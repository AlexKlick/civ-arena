"""Informational UI callbacks, including executable Lua transport-retry probes."""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from civ_arena.game.civ6 import ui_popups as ui


def token_from(lua):
    return re.search(r"POPUP(?:_CLOSE)?\|([a-f0-9]+)\|", lua)[1]


def scan_reply(lua, visible="TechCivicCompletedPopup"):
    token = token_from(lua)
    return [f"POPUP|{token}|{name}|{'visible' if name == visible else 'hidden'}"
            for name in ui.POPUPS] + [f"POPUP_SCAN_END|{token}"]


def close_reply(lua, status="sent", before="visible", after="hidden",
                detail="handler_returned", replayed="0"):
    token = token_from(lua)
    name = re.search(r"POPUP_CLOSE\|[a-f0-9]+\|([^|]+)\|", lua)[1]
    return [f"POPUP_CLOSE|{token}|{name}|{status}|{before}|{after}|{detail}|{replayed}",
            f"POPUP_CLOSE_END|{token}"]


def adapter(visible="TechCivicCompletedPopup"):
    return SimpleNamespace(
        _simulate=None, write_raw=AsyncMock(side_effect=lambda lua: scan_reply(lua, visible)),
        _conn=SimpleNamespace(_lock=asyncio.Lock(), is_connected=True,
                              _reader=object(), _writer=object(),
                              lua_states={i: n for i, n in enumerate(ui.POPUPS)},
                              _locked_execute=AsyncMock(
                                  side_effect=lambda _i, lua, _timeout: close_reply(lua))))


async def test_fake_never_accesses_wire():
    fake = SimpleNamespace(_simulate=object())
    assert (await ui.dismiss_one(fake))["status"] == "skipped_fake"


@pytest.mark.parametrize("popup", ui.POPUPS)
async def test_one_exact_informational_handler(popup):
    game = adapter(popup)
    result = await ui.dismiss_one(game)
    assert result["status"] == "sent"
    assert result["popup"] == popup
    assert result["observed_dismissal"] is True
    assert result["queue_may_have_advanced"] is False
    game._conn._locked_execute.assert_awaited_once()
    state, lua, _timeout = game._conn._locked_execute.await_args.args
    assert game._conn.lua_states[state] == popup
    assert f"pcall({ui.POPUPS[popup][1]})" in lua
    assert "pcall(Close)" not in lua
    assert ui.POPUPS[popup][0] in game.write_raw.await_args.args[0]


async def test_no_visible_target_sends_no_close():
    game = adapter(None)
    result = await ui.dismiss_one(game)
    assert result["status"] == "no_target"
    game._conn._locked_execute.assert_not_awaited()


async def test_all_missing_contexts_fail_instead_of_claiming_clear_ui():
    game = adapter()
    game.write_raw.side_effect = lambda lua: [s.replace("hidden", "missing").replace(
        "visible", "missing") for s in scan_reply(lua)]
    result = await ui.dismiss_one(game)
    assert result["status"] == "failed"
    assert result["diagnostics"] == "informational_contexts_unavailable"
    game._conn._locked_execute.assert_not_awaited()


async def test_partial_missing_contexts_allow_observed_notice():
    game = adapter()
    game.write_raw.side_effect = lambda lua: [s.replace("hidden", "missing")
                                            for s in scan_reply(lua)]
    assert (await ui.dismiss_one(game))["status"] == "sent"
    game._conn._locked_execute.assert_awaited_once()


@pytest.mark.parametrize("mutation", [
    lambda rows: rows[:-1],
    lambda rows: rows[1:],
    lambda rows: [rows[0], *rows],
    lambda rows: [rows[0].replace("visible", "failed"), *rows[1:]],
    lambda rows: [rows[0].replace("visible", "unknown"), *rows[1:]],
])
async def test_partial_duplicate_and_failed_scans_fail_without_input(mutation):
    game = adapter()
    game.write_raw.side_effect = lambda lua: mutation(scan_reply(lua))
    assert (await ui.dismiss_one(game))["status"] == "failed"
    game._conn._locked_execute.assert_not_awaited()


@pytest.mark.parametrize("states", [
    {}, {1: "TechCivicCompletedPopup", 2: "TechCivicCompletedPopup"},
])
async def test_missing_or_ambiguous_state_fails(states):
    game = adapter()
    game._conn.lua_states = states
    assert (await ui.dismiss_one(game))["diagnostics"] == "missing_or_ambiguous_lua_state"
    game._conn._locked_execute.assert_not_awaited()


async def test_queued_popup_is_not_reported_dismissed():
    game = adapter()
    game._conn._locked_execute.side_effect = lambda _i, lua, _timeout: close_reply(
        lua, after="visible")
    result = await ui.dismiss_one(game)
    assert result["status"] == "sent"
    assert result["observed_dismissal"] is False
    assert result["queue_may_have_advanced"] is True


@pytest.mark.parametrize("mutation", [lambda rows: rows[:-1], lambda rows: rows + rows,
                                       lambda rows: [rows[0].replace("handler_returned", "secret"),
                                                     rows[1]]])
async def test_incomplete_or_unrecognized_close_response_fails(mutation):
    game = adapter()
    game._conn._locked_execute.side_effect = lambda _i, lua, _timeout: mutation(close_reply(lua))
    result = await ui.dismiss_one(game)
    assert result["status"] == "failed"
    assert "secret" not in str(result)


async def test_timeout_bounds_entire_scan_and_close(monkeypatch):
    game = adapter()
    monkeypatch.setattr(ui, "CHECK_TIMEOUT", 0.02)

    async def slow_scan(lua):
        await asyncio.sleep(0.012)
        return scan_reply(lua)

    async def slow_close(*args):
        await asyncio.Event().wait()

    game.write_raw.side_effect = slow_scan
    game._conn._locked_execute.side_effect = slow_close
    result = await ui.dismiss_one(game)
    assert result["status"] == "failed"
    assert result["diagnostics"] == "timeout"
    game._conn._locked_execute.assert_awaited_once()


async def test_parent_cancellation_propagates():
    game = adapter()
    game.write_raw.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await ui.dismiss_one(game)
    game._conn._locked_execute.assert_not_awaited()


async def test_transport_error_diagnostics_are_redacted():
    game = adapter()
    game.write_raw.side_effect = RuntimeError("secret authorization transport body")
    result = await ui.dismiss_one(game)
    assert result["diagnostics"] == "RuntimeError"
    assert "secret" not in str(result)


async def test_close_transport_error_is_never_reissued():
    game = adapter()
    game._conn._locked_execute.side_effect = ConnectionError("reply lost after callback")
    game._conn.execute_in_state = AsyncMock()
    result = await ui.dismiss_one(game)
    assert result["status"] == "failed"
    assert result["diagnostics"] == "ConnectionError"
    game._conn._locked_execute.assert_awaited_once()
    game._conn.execute_in_state.assert_not_awaited()


async def test_disconnected_after_scan_sends_no_callback():
    game = adapter()
    game._conn.is_connected = False
    result = await ui.dismiss_one(game)
    assert result["diagnostics"] == "connection_changed_since_scan"
    game._conn._locked_execute.assert_not_awaited()


@pytest.fixture
def run_lua(tmp_path):
    executable = shutil.which("texlua")
    if executable is None:
        pytest.skip("texlua unavailable for executable callback fixture")

    def run(source):
        path = tmp_path / "popup.lua"
        path.write_text(source)
        result = subprocess.run([executable, str(path)], capture_output=True, text=True,
                                timeout=5, check=True)
        return result.stdout.splitlines()
    return run


def lua_context(name="TechCivicCompletedPopup", hidden="false", handler_body="calls = calls + 1"):
    return f"""
calls = 0
hidden = {hidden}
ContextPtr = {{GetID=function() return '{name}' end,
              IsHidden=function() return hidden end,
              IsVisible=function() return not hidden end}}
function OnClose() {handler_body} end
function HideScreen() {handler_body} end
"""


def test_lua_transport_replay_does_not_advance_second_queued_notice(run_lua):
    close = ui._close_lua("abc", "TechCivicCompletedPopup")
    rows = run_lua(lua_context() + close + close + "print('CALLS|' .. calls)")
    reply = "POPUP_CLOSE|abc|TechCivicCompletedPopup|sent|visible|visible|handler_returned|"
    assert rows.count(reply + "0") == 1
    assert rows.count(reply + "1") == 1
    assert rows[-1] == "CALLS|1"


@pytest.mark.parametrize("context, status, detail", [
    (lua_context(hidden="true"), "no_target", "already_hidden"),
    (lua_context(name="GreatPeoplePopup"), "failed", "identity_mismatch"),
    (lua_context() + "OnClose=nil\n", "failed", "handler_missing"),
])
def test_lua_atomic_rechecks_refuse_changed_context(run_lua, context, status, detail):
    rows = run_lua(context + ui._close_lua("abc", "TechCivicCompletedPopup") +
                   "print('CALLS|' .. calls)")
    assert any(f"|{status}|" in row and detail in row for row in rows)
    assert rows[-1] == "CALLS|0"


def test_lua_partial_handler_failure_is_not_retried(run_lua):
    close = ui._close_lua("abc", "TechCivicCompletedPopup")
    rows = run_lua(lua_context(handler_body="calls=calls+1; error('private error')") +
                   close + close + "print('CALLS|' .. calls)")
    assert sum("handler_failed" in row for row in rows) == 2
    assert rows[-1] == "CALLS|1"
    assert "private error" not in str(rows)


def test_lua_next_invocation_can_close_next_notice(run_lua):
    rows = run_lua(lua_context() + ui._close_lua("abc", "TechCivicCompletedPopup") +
                   ui._close_lua("def", "TechCivicCompletedPopup") + "print('CALLS|' .. calls)")
    assert rows[-1] == "CALLS|2"


def test_lua_scan_uses_canonical_paths_and_reports_missing(run_lua):
    path, _ = ui.POPUPS["TechCivicCompletedPopup"]
    source = lua_context() + f"""
local target=ContextPtr
ContextPtr={{LookUpControl=function(_, path)
  if path=='{path}' then return target end
  return nil
end}}
"""
    observations = ui._scan(run_lua(source + ui._scan_lua("abc")), "abc")
    assert observations["TechCivicCompletedPopup"] == "visible"
    assert list(observations.values()).count("missing") == 5
