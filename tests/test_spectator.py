import asyncio
from types import SimpleNamespace

import pytest

from civ_arena.game.civ6 import spectator
from civ_arena.game.civ6.live_driver import HotseatLimits, RecoveryEpisode
from civ_arena.game.civ6.ui_control import FakeController


def monitor(monkeypatch, result, **kwargs):
    calls = []
    async def dismiss(adapter, *, controller):
        assert isinstance(controller, FakeController)
        calls.append(adapter)
        return result
    monkeypatch.setattr(spectator.ui_popups, 'dismiss_one', dismiss)
    return spectator.PopupMonitor(SimpleNamespace(_simulate=None), FakeController(),
                                  lambda *a, **kw: None, **kwargs), calls


def sent(hidden=False):
    return {'status': 'sent', 'popup': 'TechCivicCompletedPopup', 'diagnostics': '',
            'observed_dismissal': hidden}


async def test_no_checks_when_inactive_or_fake(monkeypatch):
    m, calls = monitor(monkeypatch, sent())
    assert not await m.check(lambda: False)
    m.adapter._simulate = object()
    await m.watch(lambda: True)
    assert calls == []


async def test_visible_queue_is_bounded_and_hidden_observation_resets(monkeypatch):
    result = sent()
    m, calls = monitor(monkeypatch, result, attempts=2)
    assert await m.check()
    assert await m.check()
    with pytest.raises(RuntimeError, match='exhausted'):
        await m.check()
    assert len(calls) == 2 and m.observed_dismissals == 0
    fresh, _ = monitor(monkeypatch, result, attempts=2)
    await fresh.check()
    result['observed_dismissal'] = True
    await fresh.check()
    assert fresh.pending is None and fresh.attempts == 0
    assert fresh.observed_dismissals == 1
    await fresh.check()


async def test_helper_failure_and_deadline_abort(monkeypatch):
    m, _ = monitor(monkeypatch, {'status': 'failed', 'diagnostics': 'bad UI response'})
    with pytest.raises(RuntimeError, match='bad UI response'):
        await m.check()
    entered = asyncio.Event()
    async def stalled(adapter, *, controller):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(spectator.ui_popups, 'dismiss_one', stalled)
    m.timeout = 0.01
    with pytest.raises(TimeoutError):
        await m.check()
    assert entered.is_set()


async def test_monitor_closes_during_agent_wait_and_stops_when_cancelled(monkeypatch):
    m, calls = monitor(monkeypatch, sent(hidden=True), interval=0.001)
    task = asyncio.create_task(m.watch(lambda: True))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    count = len(calls)
    assert count > 0
    await asyncio.sleep(0.01)
    assert len(calls) == count


async def test_recovery_input_and_popup_close_are_serialized(monkeypatch):
    m, _ = monitor(monkeypatch, sent())
    started, release = asyncio.Event(), asyncio.Event()
    async def dismiss(adapter, *, controller):
        started.set()
        await release.wait()
        return sent(hidden=True)
    monkeypatch.setattr(spectator.ui_popups, 'dismiss_one', dismiss)
    popup = asyncio.create_task(m.check())
    await started.wait()
    action = asyncio.create_task(m.action(key='Escape'))
    await asyncio.sleep(0)
    assert not action.done()
    release.set()
    await popup
    assert (await action).status == 'skipped_fake'


async def test_popup_recovery_uses_sweep_budget_and_repolls_without_keys(tmp_path):
    class Controller:
        async def action(self, **kw):
            pytest.fail('must not send blind keys after semantic popup close')
    calls = []
    async def dismiss():
        calls.append('close')
        return True
    async def poll():
        calls.append('poll')
        return 'close' in calls
    recovery = RecoveryEpisode(HotseatLimits(), Controller(), lambda *a, **kw: None,
                               tmp_path, popup_check=dismiss)
    assert await recovery.sweep(poll)
    assert calls == ['poll', 'close', 'poll']
    assert recovery.attempts == 1


async def test_background_popup_failure_cancels_turn_and_records_terminal(tmp_path, monkeypatch):
    import json

    from civ_arena.config import load_config
    from civ_arena.game.civ6 import live_driver as ld
    from civ_arena.game.civ6.fake_tuner_server import FakeMod, FakeTunerServer
    from civ_arena.game.civ6.firetuner import FireTunerAdapter

    server = FakeTunerServer(mod=FakeMod(hotseat=[0, 1]))
    port = await server.start()
    adapter = FireTunerAdapter('127.0.0.1', port, simulate_hook=ld._fake_hook)
    stopped = asyncio.Event()
    async def fail_watch(self, active):
        try:
            while not active():
                await asyncio.sleep(0.001)
            raise RuntimeError('popup callback unavailable')
        finally:
            stopped.set()
    monkeypatch.setattr(ld.PopupMonitor, 'watch', fail_watch)
    try:
        spec = load_config(ld.MOD_DEFAULT.parents[2] / 'configs/live-hotseat-001.yaml')
        code = await ld.phase_dispatch_hotseat(
            spec, adapter, tmp_path, 1, 'h1', ld.MOD_DEFAULT.read_text(),
            limits=ld.HotseatLimits(match=10))
    finally:
        await server.stop()
    assert code == 2 and stopped.is_set()
    summary = json.loads((tmp_path / 'summary.json').read_text())
    events = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text().splitlines()]
    ends = [event for event in events if event['kind'] == 'MATCH_END']
    assert len(ends) == 1 and ends[0]['summary'] == summary
    assert 'popup callback unavailable' in summary['failure_reason']
    assert summary['cleanup']['status'] == 'completed'
    assert not summary['clean'] and summary['active_lease'] is not None
