"""Growth briefings and execution use the same admitted adaptive request."""
import json
from dataclasses import replace
from pathlib import Path

from civ_arena.agents.llm.strategic_controller import GROWTH_SYSTEM, SYSTEM
from civ_arena.config import load_config
from test_adaptive_context import harness
from test_growth_integration import GrowthFacade
from test_strategic_controller import advance


async def test_growth_system_is_counted_then_sent_and_quiet_turn_has_no_posts(monkeypatch):
    runtime, controller, requests, records, ledger, kinds = harness(monkeypatch)
    runtime.profile = replace(runtime.profile, player_id=0)
    controller.growth_autopilot = True
    facade = GrowthFacade()
    try:
        await advance(controller, runtime, facade, 1)
        assert [kind for kind, _ in requests] == ['count_tokens', 'generation']
        counted, generated = [body for _, body in requests]
        assert counted['system'] == generated['system'] == SYSTEM + GROWTH_SYSTEM
        assert counted == {key: value for key, value in generated.items() if key != 'max_tokens'}
        metadata = json.loads(counted['messages'][0]['content'].split('\n', 1)[0])
        assert metadata['growth']['enabled'] is True
        assert metadata['growth']['mission']['unit_id'] == 'settler'
        assert controller._growth._completed == 1
        assert controller._settlement.last_observation['outcome'] == 'requested_destination_observed'
        await advance(controller, runtime, facade, 2)
        assert len(requests) == len(ledger) == 2
        assert facade.calls.count('end_turn') == 2
    finally:
        await runtime.client.aclose()


def test_live_growth_config_preserves_existing_provider_and_allowance():
    root = Path(__file__).resolve().parents[1]
    baseline = load_config(root / 'configs/live-hotseat-strategic-minimax2-100.yaml')
    candidate = load_config(root / 'configs/live-hotseat-adaptive-growth-minimax2-100.yaml')
    assert candidate.max_turns == 100
    assert candidate.declare_own_endpath_drift is candidate.completeness_gate is True
    for new, old in zip(candidate.agents, baseline.agents, strict=True):
        assert new.growth_autopilot is True
        assert new.decision_mode == 'strategic_autopilot'
        assert replace(new.llm, adaptive_context=None) == old.llm
        assert new.llm.adaptive_context.provider_context_tokens == 1000000
        assert new.llm.adaptive_context.strategy_target_chars is None
        assert new.llm.adaptive_context.economy_target_chars is None
        assert new.llm.adaptive_context.contact_target_chars is None
        assert (new.agent_id, new.player_id, new.seed) == (old.agent_id, old.player_id, old.seed)
