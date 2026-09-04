"""Dismiss one observed informational popup through its own UI callback.

The installed Civ VI UI defines these contexts in InGame.xml. Each callback
is the normal continue/close handler: in particular TechCivic OnClose advances
one notice, whereas its generic Close discards the queue. No gameplay choice,
desktop input, second tuner connection, or arbitrary Lua state is authorized.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

# Source: steamassets/base/assets/ui/ingame.xml and the corresponding Lua files.
# EraCompletePopup's expansion replacements retain the same OnClose callback.
POPUPS = {
    "TechCivicCompletedPopup": ("/InGame/WorldPopups/TechCivicCompletedPopup", "OnClose"),
    "BoostUnlockedPopup": ("/InGame/WorldPopups/BoostUnlockedPopup", "OnClose"),
    "EraCompletePopup": ("/InGame/WorldPopups/EraCompletePopup", "OnClose"),
    "NaturalWonderPopup": ("/InGame/WorldPopups/NaturalWonderPopup", "OnClose"),
    "WonderBuiltPopup": ("/InGame/WorldPopups/WonderBuiltPopup", "OnClose"),
    "GreatWorkShowcase": ("/InGame/Screens/GreatWorkShowcase", "HideScreen"),
}
CHECK_TIMEOUT = 10.0
_OBSERVATIONS = {"missing", "hidden", "visible", "failed"}
_DETAILS = {"identity_mismatch", "observation_failed", "already_hidden", "handler_missing",
            "handler_in_progress", "handler_returned", "handler_failed"}


def _scan_lua(token: str) -> str:
    checks = []
    for name, (path, _) in POPUPS.items():
        checks.append(f"""
do
  local ok, state = pcall(function()
    local c = ContextPtr:LookUpControl('{path}')
    if c == nil then return 'missing' end
    if c:GetID() ~= '{name}' then return 'failed' end
    return (c:IsHidden() or not c:IsVisible()) and 'hidden' or 'visible'
  end)
  print('POPUP|{token}|{name}|' .. (ok and state or 'failed'))
end
""")
    return "\n".join(checks) + f"print('POPUP_SCAN_END|{token}')\nprint('---END---')"


def _close_lua(token: str, name: str) -> str:
    # name and handler come only from the fixed allowlist, token is uuid.hex.
    handler = POPUPS[name][1]
    return f"""
do
  local function emit(r, replayed)
    print('POPUP_CLOSE|{token}|{name}|' .. r.status .. '|' .. r.before ..
      '|' .. r.after .. '|' .. r.detail .. '|' .. replayed)
  end
  local function visible()
    return (ContextPtr:IsHidden() or not ContextPtr:IsVisible()) and 'hidden' or 'visible'
  end
  local ok, id = pcall(function() return ContextPtr:GetID() end)
  if not ok or id ~= '{name}' then
    emit({{status='failed', before='unavailable', after='unavailable',
      detail='identity_mismatch'}}, '0')
  else
    local cached = _CivArenaPopupDismissReceipt
    if cached and cached.token == '{token}' then
      emit(cached, '1')
    else
      local observed, before = pcall(visible)
      local r = {{token='{token}', status='failed', before='unavailable',
        after='unavailable', detail='observation_failed'}}
      -- A bounded, context-local receipt is installed BEFORE invoking the
      -- callback. A transport reconnect replay must not close the next notice.
      _CivArenaPopupDismissReceipt = r
      if observed then
        r.before = before
        r.after = before
        if before == 'hidden' then
          r.status = 'no_target'
          r.detail = 'already_hidden'
        elseif type({handler}) ~= 'function' then
          r.detail = 'handler_missing'
        else
          r.detail = 'handler_in_progress'
          local sent = pcall({handler})
          r.status = sent and 'sent' or 'failed'
          r.detail = sent and 'handler_returned' or 'handler_failed'
          local after_ok, after = pcall(visible)
          r.after = after_ok and after or 'unavailable'
        end
      end
      emit(r, '0')
    end
  end
end
print('POPUP_CLOSE_END|{token}')
print('---END---')
"""


def _scan(lines: list[str], token: str) -> dict[str, str]:
    prefix = f"POPUP|{token}|"
    rows = [line[len(prefix):].split("|") for line in lines if line.startswith(prefix)]
    if lines.count(f"POPUP_SCAN_END|{token}") != 1 or len(rows) != len(POPUPS):
        raise ValueError("incomplete_scan")
    observations = {}
    for row in rows:
        if (len(row) != 2 or row[0] not in POPUPS or row[0] in observations
                or row[1] not in _OBSERVATIONS):
            raise ValueError("invalid_scan")
        observations[row[0]] = row[1]
    if "failed" in observations.values():
        raise ValueError("failed_scan")
    return observations


def _result(status: str, *, popup: str | None = None, before: str = "unavailable",
            after: str = "unavailable", diagnostics: str = "", replayed: bool = False) -> dict:
    return {"status": status, "popup": popup, "before": before, "after": after,
            "diagnostics": diagnostics, "replayed": replayed,
            "observed_dismissal": status == "sent" and after == "hidden",
            "queue_may_have_advanced": status == "sent" and after == "visible"}


async def dismiss_one(adapter: Any) -> dict:
    """Bound the complete scan/one-callback operation to ten seconds.

    ``sent`` means the handler returned, not that the engine progressed. A
    still-visible context may contain the next queued notice. The caller must
    serialize invocations; GameConnection serializes individual wire commands.
    Cancellation by the owning driver propagates without starting another call.
    """
    if getattr(adapter, "_simulate", None) is not None:
        return _result("skipped_fake", diagnostics="fake_adapter")
    token = uuid.uuid4().hex
    popup = None
    before = "unavailable"
    try:
        async with asyncio.timeout(CHECK_TIMEOUT):
            observations = _scan(await adapter.write_raw(_scan_lua(token)), token)
            if all(state == "missing" for state in observations.values()):
                return {**_result("failed", diagnostics="informational_contexts_unavailable"),
                        "scan": observations}
            popup = next((name for name in POPUPS if observations[name] == "visible"), None)
            if popup is None:
                return {**_result("no_target", diagnostics="no_visible_informational_popup"),
                        "scan": observations}
            before = "visible"
            conn = adapter._conn
            states = [index for index, name in conn.lua_states.items() if name == popup]
            if len(states) != 1:
                return _result("failed", popup=popup, before=before,
                               diagnostics="missing_or_ambiguous_lua_state")
            # A mutating UI callback must not use execute_in_state's automatic
            # reconnect/retry: a recreated UI VM would lose its token receipt.
            # Pin the connection observed by the scan and issue exactly once.
            reader, writer = conn._reader, conn._writer
            async with conn._lock:
                if (not conn.is_connected or conn._reader is not reader
                        or conn._writer is not writer or conn.lua_states.get(states[0]) != popup):
                    return _result("failed", popup=popup, before=before,
                                   diagnostics="connection_changed_since_scan")
                lines = await conn._locked_execute(states[0], _close_lua(token, popup), 5.0)
            prefix = f"POPUP_CLOSE|{token}|{popup}|"
            replies = [line[len(prefix):].split("|") for line in lines
                       if line.startswith(prefix)]
            if len(replies) != 1 or lines.count(f"POPUP_CLOSE_END|{token}") != 1:
                return _result("failed", popup=popup, before=before,
                               diagnostics="incomplete_close_response")
            row = replies[0]
            if (len(row) != 5 or row[0] not in {"sent", "no_target", "failed"}
                    or row[1] not in {"visible", "hidden", "unavailable"}
                    or row[2] not in {"visible", "hidden", "unavailable"}
                    or row[3] not in _DETAILS or row[4] not in {"0", "1"}):
                return _result("failed", popup=popup, before=before,
                               diagnostics="invalid_close_response")
            return _result(row[0], popup=popup, before=row[1], after=row[2],
                           diagnostics=row[3], replayed=row[4] == "1")
    except TimeoutError:
        return _result("failed", popup=popup, before=before, diagnostics="timeout")
    except Exception as exc:
        # Exception text can contain raw Lua/wire output; retain only its type.
        return _result("failed", popup=popup, before=before, diagnostics=type(exc).__name__)
