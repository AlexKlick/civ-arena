"""Dismiss one observed informational popup through its own UI callback.

The installed Civ VI UI defines these contexts in InGame.xml. Each callback
is the normal continue/close handler: in particular TechCivic OnClose advances
one notice, whereas its generic Close discards the queue. AdvisorPopup uses its
observed informational OK button because this release build has no bound continue
hotkey. No gameplay choice, second tuner connection, or arbitrary state is allowed.
"""
from __future__ import annotations

import asyncio
import math
import uuid
from dataclasses import asdict
from typing import Any

from civ_arena.game.civ6 import ui_control

# Source: steamassets/base/assets/ui/ingame.xml and the corresponding Lua files.
# EraCompletePopup's expansion replacements retain the same OnClose callback.
POPUPS = {
    "TechCivicCompletedPopup": ("/InGame/WorldPopups/TechCivicCompletedPopup", "OnClose"),
    "BoostUnlockedPopup": ("/InGame/WorldPopups/BoostUnlockedPopup", "OnClose"),
    "EraCompletePopup": ("/InGame/WorldPopups/EraCompletePopup", "OnClose"),
    "NaturalWonderPopup": ("/InGame/WorldPopups/NaturalWonderPopup", "OnClose"),
    "WonderBuiltPopup": ("/InGame/WorldPopups/WonderBuiltPopup", "OnClose"),
    "GreatWorkShowcase": ("/InGame/Screens/GreatWorkShowcase", "HideScreen"),
    "AdvisorPopup": ("/TutorialUIRoot/AdvisorPopup", "desktop_ok"),
}
CHECK_TIMEOUT = 10.0
_OBSERVATIONS = {"missing", "hidden", "visible", "failed"}
_DETAILS = {"identity_mismatch", "observation_failed", "already_hidden", "handler_missing",
            "handler_in_progress", "handler_returned", "handler_failed"}

# AdvisorPopup's first-button callback invokes BOTH OnHideAdvisorDialog (releases
# the held event) and Button1Func (clears TutorialUIRoot's active advisor). Calling
# Hide/Close alone strands that state. Refuse tutorial/choice dialogs; only the
# ordinary portrait advisor with an OK first button is informational here. Its
# geometry is read without invoking a callback; the window-bound click is separate.
def _advisor_target_lua(token: str) -> str:
    return f"""
local ok, target = pcall(function()
  if ContextPtr:GetID() ~= 'AdvisorPopup' or ContextPtr:IsHidden()
      or not ContextPtr:IsVisible()
      or type(IsTutorialRunning) ~= 'function' or IsTutorialRunning()
      or type(IsBlockingInput) ~= 'function' or not IsBlockingInput()
      or Controls.AdvisorBase:IsHidden() or not Controls.MetaBase:IsHidden() then
    error('advisor is not an ordinary informational dialog')
  end
  local buttons = Controls.ButtonStack:GetChildren()
  local button = buttons[1]
  if button == nil or button:GetID() ~= 'DialogButton' or button:IsHidden() then
    error('advisor first button unavailable')
  end
  local text = button:GetText()
  if text ~= Locale.Lookup('LOC_OK_BUTTON') and text ~= 'OK' and text ~= 'Ok' then
    error('advisor first button is not OK')
  end
  local x,y = button:GetScreenOffset()
  local w,h = button:GetSizeX(),button:GetSizeY()
  local vw,vh = UIManager:GetScreenSizeVal()
  return table.concat({{x,y,w,h,vw,vh}},'|')
end)
print('ADVISOR_TARGET|{token}|' .. (ok and target or 'failed'))
print('ADVISOR_TARGET_END|{token}')
print('---END---')
"""


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
    if name == "AdvisorPopup":
        raise ValueError("advisor requires its observed OK button; no synthetic hotkey")
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


async def _click_advisor(adapter, controller, token, state, reader, writer):
    """One real OK click, pinned to the frame used by the read-only UI query."""
    if isinstance(controller, ui_control.FakeController):
        return _result("skipped_fake", popup="AdvisorPopup", diagnostics="fake_controller")
    window = await asyncio.to_thread(ui_control.select_window, controller.display)
    conn = adapter._conn
    async with conn._lock:
        if (not conn.is_connected or conn._reader is not reader or conn._writer is not writer
                or conn.lua_states.get(state) != "AdvisorPopup"):
            return _result("failed", popup="AdvisorPopup",
                           diagnostics="connection_changed_since_scan")
        lines = await conn._locked_execute(state, _advisor_target_lua(token), 5.0)
        prefix = f"ADVISOR_TARGET|{token}|"
        targets = [line[len(prefix):] for line in lines if line.startswith(prefix)]
        if len(targets) != 1 or lines.count(f"ADVISOR_TARGET_END|{token}") != 1:
            raise ValueError("incomplete advisor target")
        values = [float(value) for value in targets[0].split("|")]
        if len(values) != 6 or not all(math.isfinite(value) for value in values):
            raise ValueError("invalid advisor geometry")
        x, y, width, height, viewport_width, viewport_height = values
        if (tuple(window.geometry[2:]) != (viewport_width, viewport_height)
                or min(x, y) < 0 or min(width, height) <= 0
                or x + width > viewport_width or y + height > viewport_height):
            raise ValueError("advisor target outside the verified viewport")
        if await asyncio.to_thread(ui_control.select_window, controller.display) != window:
            raise ui_control.FrameChanged("window changed during advisor target read")
        if not conn.is_connected or conn._reader is not reader or conn._writer is not writer:
            raise ConnectionError("connection changed during advisor target read")
        outcome = await controller.action(
            at=((x + width / 2) / viewport_width, (y + height / 2) / viewport_height),
            expected_window=window, timeout=5.0)
        if outcome.status != "sent":
            return {**_result(outcome.status, popup="AdvisorPopup", before="visible",
                              diagnostics=outcome.diagnostic), "input": asdict(outcome)}
    # No retry of a click. A fresh scan is observation only; visible may mean
    # the next queued advisor. The owning driver separately verifies seat progress.
    def observation_failure(exc):
        return {**_result("failed", popup="AdvisorPopup", before="visible",
                          diagnostics=f"post_click_observation_failed:{type(exc).__name__}"),
                "input": asdict(outcome), "nonce": token, "target": values}

    try:
        after = _scan(await adapter.write_raw(_scan_lua(token)), token)["AdvisorPopup"]
    except asyncio.CancelledError as exc:
        # Preserve both cancellation and the already-known input receipt. The
        # owning monitor audits it even when its outer deadline cancelled us.
        exc.popup_outcome = observation_failure(exc)
        raise
    except Exception as exc:
        return observation_failure(exc)
    return {**_result("sent", popup="AdvisorPopup", before="visible", after=after,
                      diagnostics="observed_ok_button_clicked"),
            "input": asdict(outcome), "nonce": token, "target": values}


async def dismiss_one(adapter: Any, *, controller=None) -> dict:
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
            if popup == "AdvisorPopup":
                return await _click_advisor(adapter, controller or ui_control.Controller(),
                                           token, states[0], reader, writer)
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
    except TimeoutError as exc:
        if getattr(exc.__cause__, "popup_outcome", None) is not None:
            return exc.__cause__.popup_outcome
        return _result("failed", popup=popup, before=before, diagnostics="timeout")
    except Exception as exc:
        # Exception text can contain raw Lua/wire output; retain only its type.
        return _result("failed", popup=popup, before=before, diagnostics=type(exc).__name__)
