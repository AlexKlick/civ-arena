"""CAR-110 live-gate policy and immutable configuration pins."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from scripts.live_newgame import CONFIG_HOTSEAT_LUA

from civ_arena.agents.runtime import AgentProfile
from civ_arena.config import load_config
from civ_arena.v2.contracts import ActionKindV2, TurnContextV2, TurnTerminationV2
from civ_arena.v2.enumeration import ActionEnumeratorV2
from civ_arena.v2.environment import simulator_facets_v2
from civ_arena.v2.executor import TransactionalExecutorV2
from civ_arena.v2.graph import ActionGraphCompilerV2
from civ_arena.v2.policy import PolicyServicesV2, build_policy_runtime_v2

CONFIGS = (
    Path("configs/car-m1-live-canonical-1.yaml"),
    Path("configs/car-m1-live-random-frontier.yaml"),
    Path("configs/car-m1-live-expansionist.yaml"),
    Path("configs/car-m1-live-canonical-repeat.yaml"),
)


async def _runtime_context(
    policy: str,
    seed: int,
) -> tuple[Any, Any, Any, TurnContextV2]:
    environment, monitor = simulator_facets_v2()
    await environment.reset({"seed": 271828})
    observation = await environment.begin_turn(0, 1)
    legal = ActionEnumeratorV2().enumerate(observation)
    graph = ActionGraphCompilerV2().compile(observation, legal)
    profile = AgentProfile(f"gate-{policy}", 0, policy, seed)
    services = PolicyServicesV2(profile.agent_id, profile.player_id)
    runtime = build_policy_runtime_v2(profile, services=services)
    context = TurnContextV2(observation, legal, graph, runtime.descriptor)
    return environment, monitor, runtime, context


def test_live_gate_configs_are_dag_tx_and_repeat_is_identical() -> None:
    specs = [load_config(path) for path in CONFIGS]
    for spec in specs:
        assert spec.schema == 2
        assert spec.execution_mode == "dag_tx"
        assert spec.scored is False
        assert spec.adapter == "firetuner"
        assert spec.max_turns == 20
        assert spec.violation_limit == 0
        assert [agent.player_id for agent in spec.agents] == [0, 1]

    first = asdict(specs[0])
    repeat = asdict(specs[3])
    first.pop("match_id")
    repeat.pop("match_id")
    assert first == repeat


def test_hotseat_setup_pins_and_reads_back_both_engine_seeds() -> None:
    assert 'GameConfiguration.SetValue("GAME_SYNC_RANDOM_SEED", 271828)' in (
        CONFIG_HOTSEAT_LUA
    )
    assert 'MapConfiguration.SetValue("RANDOM_SEED", 271828)' in CONFIG_HOTSEAT_LUA
    assert 'GameConfiguration.GetValue("GAME_SYNC_RANDOM_SEED")' in (
        CONFIG_HOTSEAT_LUA
    )
    assert 'MapConfiguration.GetValue("RANDOM_SEED")' in CONFIG_HOTSEAT_LUA


@pytest.mark.asyncio
async def test_canonical_frontier_proposes_without_mutating_then_completes() -> None:
    environment, monitor, runtime, context = await _runtime_context(
        "canonical-first", 7
    )
    before = monitor.state_hash()

    proposal = await runtime.propose_turn(context)

    assert monitor.state_hash() == before
    assert proposal.intents[-1].action_kind is ActionKindV2.END_TURN
    mandatory_groups = {
        (action.action_kind, action.actor)
        for action in context.legal_actions.actions
        if action.mandatory
    }
    proposed_groups = {
        (intent.action_kind, intent.actor)
        for intent in proposal.intents[:-1]
    }
    assert proposed_groups == mandatory_groups

    receipt = await TransactionalExecutorV2(
        environment,
        runtime.descriptor,
        episode_id="canonical-frontier-test",
    ).execute(proposal, context.observation, context.graph)
    assert receipt.termination is TurnTerminationV2.COMPLETED


@pytest.mark.asyncio
async def test_random_frontier_is_fixed_seed_and_observation_deterministic() -> None:
    left = await _runtime_context("random-frontier", 424242)
    right = await _runtime_context("random-frontier", 424242)

    left_proposal = await left[2].propose_turn(left[3])
    right_proposal = await right[2].propose_turn(right[3])

    assert left_proposal.to_doc() == right_proposal.to_doc()
    assert left[1].state_hash() == right[1].state_hash()
    assert left_proposal.intents[-1].action_kind is ActionKindV2.END_TURN
