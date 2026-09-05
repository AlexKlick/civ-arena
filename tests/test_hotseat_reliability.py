import asyncio
import json
from types import SimpleNamespace

import pytest

from civ_arena.agents.llm.runtime import LLMAgentRuntime
from civ_arena.agents.runtime import AgentProfile
from civ_arena.arena.referee import MatchAborted
from civ_arena.config import load_config
from civ_arena.game.civ6 import live_driver as ld
from civ_arena.game.civ6.fake_tuner_server import FakeMod, FakeTunerServer
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from civ_arena.game.civ6.ui_control import FakeController
from civ_arena.replay import RecordedCall, ReplayRuntime
from fakes import FakeModel, text, use
from test_llm_runtime import FAKE_LLM


async def test_recovery_attempts_persist_and_reset_only_on_engagement(tmp_path):
    episode = ld.RecoveryEpisode(ld.HotseatLimits(), FakeController(), lambda *a, **kw: None,
                                 tmp_path)
    async def guard_failure():
        return False
    for _ in range(8):
        assert not await episode.sweep(guard_failure)
    with pytest.raises(RuntimeError, match='sweep limit'):
        await episode.sweep(guard_failure)
    assert episode.attempts == 8
    episode.engaged()
    assert episode.attempts == 0
    assert not await episode.sweep(guard_failure)


async def test_recovery_poll_included_in_deadline(tmp_path):
    episode = ld.RecoveryEpisode(ld.HotseatLimits(recovery=0.02), FakeController(),
                                 lambda *a, **kw: None, tmp_path)
    async def stalled_poll():
        await asyncio.Event().wait()
    with pytest.raises(TimeoutError):
        await episode.sweep(stalled_poll)
    assert episode.attempts == 1


async def test_recovery_stops_input_on_first_observed_progress(tmp_path):
    actions = []
    class Controller(FakeController):
        async def action(self, **kw):
            actions.append(kw)
            return await super().action(**kw)
    episode = ld.RecoveryEpisode(ld.HotseatLimits(), Controller(), lambda *a, **kw: None,
                                 tmp_path)
    async def engaged():
        return len(actions) == 1
    assert await episode.sweep(engaged)
    assert len(actions) == 1
    assert episode.attempts == 1  # only caller's successful begin can reset


@pytest.mark.parametrize('rows', [ [(1, 1)], [(1, 0), (1, 0)],
                                   [(1, 0), (2, 1)], [(1, 0), (1, 1), (3, 0)] ])
def test_missing_duplicate_or_out_of_order_seats_cannot_count(rows):
    ledger = ld.CompletedTurns([0, 1])
    with pytest.raises(RuntimeError, match='out-of-order'):
        for turn, player in rows:
            ledger.append(dict(turn=turn, player=player), SimpleNamespace(released=True))
    assert ledger.rounds < 30


def test_open_lease_never_counts():
    ledger = ld.CompletedTurns([0, 1])
    with pytest.raises(RuntimeError, match='open lease'):
        ledger.append(dict(turn=1, player=0), SimpleNamespace(released=False))
    assert ledger.rows == []


@pytest.mark.parametrize('prose', [True, False])
@pytest.mark.parametrize('closed', [True, False])
async def test_forced_llm_closure_repairs_once(prose, closed):
    from dataclasses import replace
    llm = replace(FAKE_LLM, max_tool_rounds=1)
    model = FakeModel(script=[[text('Done')] if prose else [use('get_overview')]])
    runtime = LLMAgentRuntime.build(AgentProfile('a', 0, 'llm', 1, llm=llm), client=model)
    runtime.begin_turn(1)
    class Facade:
        calls = 0
        fortified = []
        async def get_overview(self):
            return {}
        async def end_turn(self):
            self.calls += 1
            if self.calls == 2 and closed:
                return {'status': 'accepted'}
            return {'status': 'rejected', 'rejection': 'unmoved_units', 'unmoved_units': ['u1']}
        async def fortify(self, uid):
            self.fortified.append(uid)
            return {'status': 'accepted'}
    facade = Facade()
    if closed:
        await runtime.take_turn(facade)
    else:
        with pytest.raises(MatchAborted, match='did not release'):
            await runtime.take_turn(facade)
    assert facade.calls == 2
    assert facade.fortified == ['u1']


async def test_replay_keeps_rejected_end_turn_in_same_turn():
    calls = [RecordedCall('end_turn', {}, None), RecordedCall('fortify', {'unit_id': 'u1'}, None),
             RecordedCall('end_turn', {}, None), RecordedCall('get_overview', {}, None)]
    runtime = ReplayRuntime(calls)
    seen = []
    class Facade:
        async def end_turn(self):
            seen.append('end')
            return {'status': 'accepted' if len(seen) == 3 else 'rejected'}
        async def fortify(self, uid):
            seen.append(uid)
    await runtime.take_turn(Facade())
    assert seen == ['end', 'u1', 'end']
    assert runtime.issued == 3


@pytest.mark.parametrize('stage', ['startup', 'active', 'recovery', 'cleanup', 'cancel'])
async def test_termination_writes_one_honest_terminal_record(tmp_path, monkeypatch, stage):
    spec = load_config(ld.MOD_DEFAULT.parents[2] / 'configs/live-hotseat-001.yaml')
    server = FakeTunerServer(mod=FakeMod(hotseat=[0, 1]))
    port = await server.start()
    adapter = FireTunerAdapter('127.0.0.1', port, simulate_hook=ld._fake_hook, poll_timeout_s=0.1)
    class Runtime:
        async def take_turn(self, facade):
            if stage == 'cancel':
                raise asyncio.CancelledError()
            if stage == 'active':
                raise MatchAborted('provider unavailable')
            await facade.end_turn()
            await facade.end_turn()
        async def aclose(self):
            if stage == 'cleanup':
                await asyncio.Event().wait()
    monkeypatch.setattr(ld, 'build_runtime', lambda _, **_context: Runtime())
    if stage == 'startup':
        async def setup(_):
            raise RuntimeError('setup unavailable')
        monkeypatch.setattr(adapter, 'setup', setup)
    if stage == 'recovery':
        async def guard(*a):
            raise RuntimeError('cannot begin turn persistent guard')
        monkeypatch.setattr(ld.Referee, 'begin_turn', guard)
    try:
        code = await ld.phase_dispatch_hotseat(
            spec, adapter, tmp_path, 1, 'h1', ld.MOD_DEFAULT.read_text(),
            limits=ld.HotseatLimits(recovery=0.03 if stage == "recovery" else 10, cleanup=0.03))
    finally:
        await server.stop()
    assert code == 2
    summary = json.loads((tmp_path / 'summary.json').read_text())
    events = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text().splitlines()]
    assert len([e for e in events if e['kind'] == 'MATCH_START']) == 1
    ends = [e for e in events if e['kind'] == 'MATCH_END']
    assert len(ends) == 1 and ends[0]['summary'] == summary
    assert not summary['clean'] and summary['failure_reason']
    if stage == 'startup':
        assert summary['final_state_hash'] is None
    if stage in ('active', 'cancel'):
        assert summary['active_lease'] is not None
    if stage == 'cleanup':
        assert summary['cleanup']['status'] == 'failed'


async def test_movement_allowance_records_exact_rows_and_keeps_foreign_drift_strict(tmp_path):
    from civ_arena.game.adapter import MutationRecord
    from test_watchdog import harness
    adapter, referee, log, ctx, _ = await harness(tmp_path)
    own = next(u for u in adapter.state.units.values() if u['owner'] == 0)
    foreign = next(u for u in adapter.state.units.values() if u['owner'] == 1)
    admitted = MutationRecord('unit.moved', 'unit', own['unit_id'], 'movement', 2, 0)
    rows = [admitted,
            MutationRecord('unit.moved', 'unit', foreign['unit_id'], 'movement', 2, 0),
            MutationRecord('unit.damaged', 'unit', own['unit_id'], 'hp', 100, 90)]
    adapter._journal.extend(rows)
    await referee._declare_own_endpath_drift(ctx)
    audits = [e for e in log.records() if e.get('audit') == 'movement_allowance']
    assert audits[-1]['mutations'] == [admitted.to_doc()]
    violations = await referee._sweep(0, 'roman', 1, final=True)
    flagged = [m for v in violations for m in v.mutations]
    assert admitted.to_doc() not in flagged
    assert all(m.to_doc() in flagged for m in rows[1:])


async def test_persistent_phase_guard_aborts_after_eight_sweeps(tmp_path, monkeypatch):
    spec = load_config(ld.MOD_DEFAULT.parents[2] / 'configs/live-hotseat-001.yaml')
    server = FakeTunerServer(mod=FakeMod(hotseat=[0, 1]))
    port = await server.start()
    adapter = FireTunerAdapter('127.0.0.1', port, simulate_hook=ld._fake_hook)
    async def no_real_input(*args, **kwargs):
        pytest.fail("fake hotseat attempted real desktop input")
    monkeypatch.setattr(ld.ui_control.Controller, 'action', no_real_input)
    original = ld.RecoveryEpisode.start
    def no_backoff(self):
        original(self)
        self.next_sweep = 0
    async def guard(*args):
        raise RuntimeError('cannot begin turn persistent guard')
    monkeypatch.setattr(ld.RecoveryEpisode, 'start', no_backoff)
    monkeypatch.setattr(ld.Referee, 'begin_turn', guard)
    try:
        assert await ld.phase_dispatch_hotseat(
            spec, adapter, tmp_path, 30, 'h1', ld.MOD_DEFAULT.read_text()) == 2
    finally:
        await server.stop()
    summary = json.loads((tmp_path / 'summary.json').read_text())
    assert summary['recovery_attempts'] == 8
    assert summary['per_turn'] == []
    assert 'sweep limit' in summary['failure_reason']
