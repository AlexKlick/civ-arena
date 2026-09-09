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


@pytest.mark.parametrize("popup", [name for name in ui.POPUPS if name != "AdvisorPopup"])
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


def advisor_context(*, tutorial="false", label="OK", hidden="false"):
    return f"""
ContextPtr={{GetID=function() return 'AdvisorPopup' end,
 IsHidden=function() return false end, IsVisible=function() return true end}}
Controls={{AdvisorBase={{IsHidden=function() return false end}},
 MetaBase={{IsHidden=function() return true end}},
 ButtonStack={{GetChildren=function() return {{{{GetID=function() return 'DialogButton' end,
 IsHidden=function() return {hidden} end, GetText=function() return '{label}' end,
 GetScreenOffset=function() return 299,168 end,
 GetSizeX=function() return 200 end, GetSizeY=function() return 41 end}}}} end}}}}
Locale={{Lookup=function(key) assert(key=='LOC_OK_BUTTON'); return 'OK' end}}
UIManager={{GetScreenSizeVal=function() return 1024,768 end}}
function IsTutorialRunning() return {tutorial} end
function IsBlockingInput() return true end
function Close() error('generic close forbidden') end
function OnInputActionTriggered() error('unbound hotkey forbidden') end
"""


def test_advisor_target_reads_real_button_without_hiding_or_invoking_hotkey(run_lua):
    rows = run_lua(advisor_context() + ui._advisor_target_lua("abc"))
    assert 'ADVISOR_TARGET|abc|299|168|200|41|1024|768' in rows
    assert 'ADVISOR_TARGET_END|abc' in rows


@pytest.mark.parametrize("context", [
    advisor_context(tutorial="true"), advisor_context(label="Tell me more"),
    advisor_context(hidden="true"),
])
def test_advisor_target_refuses_tutorial_choice_or_hidden_button(run_lua, context):
    rows = run_lua(context + ui._advisor_target_lua("abc"))
    assert 'ADVISOR_TARGET|abc|failed' in rows


@pytest.fixture
def desktop_advisor(monkeypatch):
    from civ_arena.game.civ6 import ui_control
    window = ui_control.Window(':1', 10, (1209, 360, 1024, 768))
    monkeypatch.setattr(ui_control, 'select_window', lambda _: window)
    game = adapter('AdvisorPopup')
    game.write_raw.side_effect = [scan_reply(ui._scan_lua('abc'), 'AdvisorPopup'),
                                 scan_reply(ui._scan_lua('abc'), None)]
    monkeypatch.setattr(ui.uuid, 'uuid4', lambda: SimpleNamespace(hex='abc'))
    game._conn._locked_execute.side_effect = lambda _i, _lua, _t: [
        'ADVISOR_TARGET|abc|299|168|200|41|1024|768', 'ADVISOR_TARGET_END|abc']
    controller = SimpleNamespace(display=':1', action=AsyncMock(
        return_value=ui_control.Outcome('sent', window=vars(window), capture_sha256='capture')))
    return game, controller, window


async def test_advisor_click_pins_frame_and_observes_hidden(desktop_advisor):
    game, controller, window = desktop_advisor
    result = await ui.dismiss_one(game, controller=controller)
    assert result['observed_dismissal'] and result['status']=='sent'
    assert result['nonce']=='abc' and result['input']['capture_sha256']=='capture'
    controller.action.assert_awaited_once_with(
        at=(399/1024,188.5/768), expected_window=window, timeout=5.0)
    game._conn._locked_execute.assert_awaited_once()
    assert game.write_raw.await_count==2


async def test_advisor_visible_next_notice_is_not_reported_hidden(desktop_advisor):
    game, controller, _ = desktop_advisor
    game.write_raw.side_effect = lambda lua: scan_reply(lua, 'AdvisorPopup')
    result = await ui.dismiss_one(game, controller=controller)
    assert result['status']=='sent' and result['queue_may_have_advanced']
    assert not result['observed_dismissal']
    controller.action.assert_awaited_once()  # one input, never auto-drain the queue


@pytest.mark.parametrize('failure', ['moved','resized','different_window','viewport','bad_rect'])
async def test_advisor_refuses_stale_or_invalid_geometry(desktop_advisor, monkeypatch, failure):
    from dataclasses import replace

    from civ_arena.game.civ6 import ui_control
    game, controller, window = desktop_advisor
    if failure in ('moved','resized','different_window'):
        new = replace(window, window_id=11) if failure=='different_window' else replace(
            window, geometry=(1300,360,1024,768) if failure=='moved' else (1209,360,800,600))
        frames = iter([window,new])
        monkeypatch.setattr(ui_control,'select_window',lambda _: next(frames))
    else:
        value = '299|168|200|41|800|600' if failure=='viewport' else '999|168|200|41|1024|768'
        game._conn._locked_execute.side_effect = lambda *_: [
            'ADVISOR_TARGET|abc|'+value, 'ADVISOR_TARGET_END|abc']
    result = await ui.dismiss_one(game,controller=controller)
    assert result['status']=='failed'
    controller.action.assert_not_awaited()


async def test_fake_advisor_controller_never_inspects_desktop(desktop_advisor, monkeypatch):
    from civ_arena.game.civ6 import ui_control
    game, _, _ = desktop_advisor
    monkeypatch.setattr(ui_control,'select_window',lambda _: pytest.fail('desktop read'))
    result = await ui.dismiss_one(game,controller=ui_control.FakeController())
    assert result['status']=='skipped_fake'
    game._conn._locked_execute.assert_not_awaited()


async def test_advisor_post_click_failure_preserves_sent_input_without_retry(desktop_advisor):
    game, controller, _ = desktop_advisor
    game.write_raw.side_effect = [scan_reply(ui._scan_lua('abc'), 'AdvisorPopup'),
                                 ConnectionError('lost observation')]
    result = await ui.dismiss_one(game, controller=controller)
    assert result['status'] == 'failed' and result['input']['status'] == 'sent'
    assert result['after'] == 'unavailable' and result['nonce'] == 'abc'
    assert result['diagnostics'] == 'post_click_observation_failed:ConnectionError'
    controller.action.assert_awaited_once()


@pytest.mark.parametrize('deadline_owner', ['helper', 'monitor'])
async def test_advisor_post_click_timeout_keeps_receipt_and_cancellation(
        desktop_advisor, monkeypatch, deadline_owner):
    from civ_arena.game.civ6.spectator import PopupMonitor

    game, controller, _ = desktop_advisor
    reads = 0
    async def scan(lua):
        nonlocal reads
        reads += 1
        if reads == 1:
            return scan_reply(lua, 'AdvisorPopup')
        await asyncio.Event().wait()
    game.write_raw.side_effect = scan
    monkeypatch.setattr(ui, 'CHECK_TIMEOUT', 0.03 if deadline_owner == 'helper' else 1)
    if deadline_owner == 'helper':
        result = await ui.dismiss_one(game, controller=controller)
    else:
        audits = []
        monitor = PopupMonitor(game, controller, lambda event, **kw: audits.append(kw),
                               timeout=0.03)
        with pytest.raises(TimeoutError):
            await monitor.check()
        assert len(audits) == 1
        assert monitor.summary()['close_requests'] == 1
        result = audits[0]['result']
    assert result['status'] == 'failed' and result['input']['status'] == 'sent'
    assert result['after'] == 'unavailable' and result['nonce'] == 'abc'
    assert result['diagnostics'] == 'post_click_observation_failed:CancelledError'
    controller.action.assert_awaited_once()


async def test_advisor_input_timeout_does_not_retry_or_claim_dismissal(desktop_advisor):
    from civ_arena.game.civ6 import ui_control
    game, controller, _ = desktop_advisor
    controller.action.return_value = ui_control.Outcome('failed', 'helper timeout')
    result = await ui.dismiss_one(game, controller=controller)
    assert result['status'] == 'failed' and not result['observed_dismissal']
    controller.action.assert_awaited_once()
    assert game.write_raw.await_count == 1


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
    assert list(observations.values()).count("missing") == len(ui.POPUPS) - 1
