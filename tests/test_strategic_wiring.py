"""Explicit policy opt-in through config, factory and audited driver boundary."""
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from civ_arena.agents.llm.runtime import LLMAgentRuntime
from civ_arena.agents.runtime import AgentProfile, build_runtime
from civ_arena.arena.coordinator import Arena
from civ_arena.config import ConfigError, load_config, parse_config
from civ_arena.game.civ6.live_driver import _agent_profile, _strategic_audit
from fakes import FakeModel, use
from test_config_llm import VALID, llm_doc
from test_strategic_controller import Facade

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('value', ['auto', None, False, [], {}])
def test_invalid_decision_mode_fails_config(value):
    doc = llm_doc(VALID)
    doc['agents'][0]['decision_mode'] = value
    with pytest.raises(ConfigError, match='decision_mode'):
        parse_config(doc)


def test_new_config_changes_policy_only_and_preserves_provider_caps():
    old = load_config(ROOT / 'configs/live-hotseat-llm-minimax2-060.yaml')
    new = load_config(ROOT / 'configs/live-hotseat-strategic-minimax2-060.yaml')
    assert new.max_turns == old.max_turns == 60
    assert new.declare_own_endpath_drift == old.declare_own_endpath_drift
    for before, after in zip(old.agents, new.agents, strict=True):
        assert asdict(before.llm) == asdict(after.llm)
        assert after.decision_mode == 'strategic_autopilot'
        assert before.decision_mode == 'legacy'
        assert _agent_profile(after).decision_mode == 'strategic_autopilot'


async def test_real_factory_retains_controller_and_caps_across_quiet_turn(monkeypatch):
    doc = llm_doc(VALID)
    doc['agents'][0]['decision_mode'] = 'strategic_autopilot'
    agent = parse_config(doc).agents[0]
    fake = FakeModel([[use('submit_directive', {'version': 1})]])
    forwarded = []
    def client(spec, on_post=None):
        forwarded.append((spec, on_post))
        return fake
    monkeypatch.setattr('civ_arena.agents.llm.client.MiniMaxMessagesClient', client)
    monkeypatch.setattr('civ_arena.agents.llm.strategic_controller.run_scouting',
                        AsyncMock(return_value={'nodes': [], 'seed': 'fixture'}))
    audit = []
    runtime = build_runtime(_agent_profile(agent), match_id='trusted-match',
                            audit=audit.append, opening_units_frozen=False)
    controller = runtime.strategic_controller
    facade = Facade()
    for turn in (1, 2):
        runtime.begin_turn(turn)
        await runtime.take_turn(facade)
    assert runtime.strategic_controller is controller
    assert fake.posts_sent == 1
    assert runtime.client is fake and runtime.llm == agent.llm
    assert forwarded[0][0] is agent.llm
    assert controller.opening_units_frozen is False
    assert any(row['audit'] == 'strategy_decision' for row in audit)
    assert any(row['audit'] == 'strategy_execution' and row['source'] == 'autopilot'
               for row in audit)


def test_factory_refuses_missing_match_context_before_client_creation(monkeypatch):
    monkeypatch.setattr(LLMAgentRuntime, 'build', lambda *a, **k: pytest.fail('client built'))
    profile = AgentProfile('a', 0, 'llm', 1, llm=object(), decision_mode='strategic_autopilot')
    with pytest.raises(ValueError, match='trusted match_id'):
        build_runtime(profile)


async def test_coordinator_refuses_resume_before_any_engine_setup():
    arena = object.__new__(Arena)
    arena.spec = SimpleNamespace(agents=[SimpleNamespace(decision_mode='strategic_autopilot')])
    with pytest.raises(ValueError, match='resume is unsupported'):
        await arena.run(resume_state=object())


def test_live_audit_rebinds_match_without_duplicate_keyword_and_keeps_turn():
    calls = []
    driver = SimpleNamespace(_write=lambda kind, **kwargs: calls.append((kind, kwargs)))
    _strategic_audit(driver, {'audit': 'strategy_decision', 'match_id': 'from-controller',
                             'turn': 1, 'player_id': 0, 'agent_id': 'a'})
    assert calls == [('HEARTBEAT', {'audit': 'strategy_decision', 'turn': 1,
                                   'player_id': 0, 'agent_id': 'a',
                                   'strategy_payload_json': '{}'})]


async def test_live_fake_dispatch_preserves_strategic_economy_choices(tmp_path, monkeypatch):
    import json

    from civ_arena.game.civ6 import live_driver as ld
    from civ_arena.game.civ6.fake_tuner_server import FakeMod, FakeTunerServer
    from civ_arena.game.civ6.firetuner import FireTunerAdapter

    spec = load_config(ROOT / 'configs/live-hotseat-strategic-minimax2-060.yaml')
    models = []
    def client(_spec, on_post=None):
        fake = FakeModel([[use('submit_directive', {'version': 1,
                         'production_preferences': ['WARRIOR'], 'unit_targets': {'WARRIOR': 2},
                         'research_preferences': ['POTTERY']})]])
        fake.on_post = on_post
        models.append(fake)
        return fake
    monkeypatch.setattr('civ_arena.agents.llm.client.MiniMaxMessagesClient', client)
    mod = FakeMod(hotseat=[0, 1])
    mod.pending_blockers = ['BLOCKING|ENDTURN_BLOCKING_RESEARCH',
                            'BLOCKING|ENDTURN_BLOCKING_PRODUCTION']
    server = FakeTunerServer(mod=mod)
    port = await server.start()
    adapter = FireTunerAdapter('127.0.0.1', port, simulate_hook=ld._fake_hook)
    try:
        result = await ld.phase_dispatch_hotseat(spec, adapter, tmp_path, 1, 'h1',
                                                ld.MOD_DEFAULT.read_text())
    finally:
        await server.stop()
    summary = json.loads((tmp_path / 'summary.json').read_text())
    assert result == 0, summary
    assert mod.cities[1]['queue'] == 'WARRIOR'  # housekeeping preference is MONUMENT
    assert mod.players[0]['researching'] == 'POTTERY'  # housekeeping preference is MINING
    assert len(summary['per_turn']) == 2 and summary['completed_rounds'] == 1
    events = [json.loads(row) for row in (tmp_path / 'events.jsonl').read_text().splitlines()]
    assert all(row['opening_units_frozen'] is False for row in events
               if row.get('audit') == 'decision_mode')
    choices = [row for row in events if row.get('tool') == 'set_city_production'
               and row['kind'] == 'TOOL_CALL']
    assert choices and choices[0]['agent_id'] == spec.agents[0].agent_id
    assert all(model.posts_sent == 1 for model in models)


async def test_strategic_deferral_retains_civic_and_policy_housekeeping(monkeypatch):
    from civ_arena.game.civ6 import live_driver as ld

    adapter = SimpleNamespace(write_raw=AsyncMock(side_effect=[
        ['BLOCKING|ENDTURN_BLOCKING_CIVIC', 'BLOCKING|FILL_CIVIC_SLOT',
         'BLOCKING|ENDTURN_BLOCKING_RESEARCH', 'BLOCKING|ENDTURN_BLOCKING_PRODUCTION'], []]),
        read_raw=AsyncMock(return_value=[]))
    fill = AsyncMock()
    research = AsyncMock()
    monkeypatch.setattr(ld, '_fill_empty_queues', fill)
    monkeypatch.setattr(ld, '_ensure_research', research)
    await ld._resolve_blockers(adapter, 0, 1, defer_economy=True)
    assert adapter.read_raw.await_count == 1
    assert adapter.write_raw.await_count == 2
    fill.assert_not_awaited()
    research.assert_not_awaited()


@pytest.mark.parametrize('lane', ['live', 'simulator'])
def test_strategy_probabilities_survive_real_event_log_prefix_hash(tmp_path, lane):
    import json

    from civ_arena.arena.events import EventLog
    from civ_arena.canonical import log_prefix_hash
    from civ_arena.game.civ6.live_driver import LiveDriver

    log = EventLog(tmp_path / 'events.jsonl')
    payload = {'audit': 'strategy_graph', 'match_id': 'm', 'agent_id': 'a', 'player_id': 0,
               'turn': 1, 'graph': {'probability': 0.125, 'weight': 0.75}}
    if lane == 'live':
        driver = object.__new__(LiveDriver)
        driver.spec = SimpleNamespace(match_id='m')
        driver.game_instance_id = 'i'
        driver.log = log
        _strategic_audit(driver, payload)
    else:
        arena = object.__new__(Arena)
        arena.game_instance_id = 'i'
        arena.log = log
        arena._strategic_audit(payload)
    log.close()
    records = log.records()
    assert len(log_prefix_hash(records)) == 64
    assert records[0]['audit'] == 'strategy_graph'
    assert json.loads(records[0]['strategy_payload_json'])['graph']['probability'] == 0.125
