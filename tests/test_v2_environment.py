from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

import pytest

from civ_arena.game.adapter import ActionCommand
from civ_arena.game.civ6.fake_tuner_server import FakeMod, FakeTunerServer
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from civ_arena.game.sim.rules import check_action, legal_actions
from civ_arena.game.sim.simulator import SimulatorAdapter
from civ_arena.game.sim.state import SimState
from civ_arena.v2.contracts import (
    ActionKindV2,
    ActionStatusV2,
    AdapterKindV2,
    AuthorizationDecisionV2,
    AuthorizationReasonV2,
    AuthorizationV2,
    EntityTypeV2,
    EnvironmentCapabilityV2,
    EnvironmentDescriptorV2,
    LegalActionV2,
)
from civ_arena.v2.enumeration import ActionEnumeratorV2, GraphOverflowError
from civ_arena.v2.environment import (
    firetuner_facets_v2,
    simulator_facets_v2,
    split_adapter_v2,
)
from civ_arena.v2.schemas import ContractError

GRAPH_ID = "a" * 64


def _fake_live_hook(event: str, player_id: int, turn: int = 0) -> str:
    return {
        "turn_start": f"Simulate.TurnStartAt({player_id}, {turn})",
        "turn_deactivated": f"Simulate.TurnDeactivated({player_id})",
        "advance_turn": "Simulate.AdvanceTurn()",
    }[event]


def _authorization(action: LegalActionV2) -> AuthorizationV2:
    return AuthorizationV2.create(
        decision=AuthorizationDecisionV2.AUTHORIZED,
        intent_id="b" * 64,
        action_id=action.action_id,
        observation_id=action.observation_id,
        graph_id=GRAPH_ID,
        reason_code=AuthorizationReasonV2.AUTHORIZED,
    )


def _action_key(action: LegalActionV2) -> tuple[str, tuple[tuple[str, object], ...]]:
    return action.action_kind.value, tuple(action.parameters)


def _oracle_key(tool: str, args: Mapping[str, Any]) -> tuple[str, tuple[tuple[str, object], ...]]:
    normalized = dict(args)
    if tool == "found_city":
        normalized["name"] = f"Arena-{normalized['unit_id']}"
    return tool, tuple(sorted(normalized.items()))


def _direct_visible_coords(observation: Any) -> set[str]:
    return {
        str(fact.value.value)
        for fact in observation.facts
        if fact.subject.entity_type is EntityTypeV2.TILE
        and fact.predicate == "coord"
        and fact.source.value == "direct"
    }


def _visible_foreign_ids(observation: Any) -> set[str]:
    return {
        fact.subject.entity_id
        for fact in observation.facts
        if fact.subject.entity_type is EntityTypeV2.UNIT
        and fact.subject_scope.value == "visible_foreign"
    }


def _observable_oracle_actions(
    state: SimState,
    player_id: int,
    observation: Any,
) -> set[tuple[str, tuple[tuple[str, object], ...]]]:
    direct_tiles = _direct_visible_coords(observation)
    visible_foreign = _visible_foreign_ids(observation)
    out = set()
    for tool, args in legal_actions(state, player_id):
        if tool == "move_unit" and args["dest"] not in direct_tiles:
            continue
        if tool == "attack" and args["target_id"] not in visible_foreign:
            continue
        out.add(_oracle_key(tool, args))
    return out


@pytest.mark.asyncio
async def test_fake_observation_and_legal_set_are_deterministic_and_sound() -> None:
    left, left_monitor = simulator_facets_v2()
    right, _ = simulator_facets_v2()
    await left.reset({"seed": 41})
    await right.reset({"seed": 41})

    left_observation = await left.begin_turn(0, 1)
    right_observation = await right.begin_turn(0, 1)
    left_actions = ActionEnumeratorV2().enumerate(left_observation)
    right_actions = ActionEnumeratorV2().enumerate(right_observation)

    assert left_observation == right_observation
    assert left_actions == right_actions
    state = SimState.from_doc(left_monitor.snapshot())
    for action in left_actions.actions:
        if action.action_kind is ActionKindV2.END_TURN:
            continue
        assert check_action(
            state,
            0,
            action.action_kind.value,
            dict(action.parameters),
        ) is None


@pytest.mark.asyncio
async def test_enumerator_is_complete_for_actions_proven_by_observation() -> None:
    environment, monitor = simulator_facets_v2()
    await environment.reset({"seed": 41})
    observation = await environment.begin_turn(0, 1)
    actions = ActionEnumeratorV2().enumerate(observation)
    state = SimState.from_doc(monitor.snapshot())

    got = {_action_key(action) for action in actions.actions}
    expected = _observable_oracle_actions(state, 0, observation)
    assert got == expected
    assert len(legal_actions(state, 0)) > len(expected), (
        "the fixture must contain legal moves through unseen tiles so the "
        "observable/legal distinction has teeth"
    )


@pytest.mark.asyncio
async def test_hidden_rng_and_nonvisible_unit_state_are_noninterfering() -> None:
    control, control_monitor = simulator_facets_v2()
    variant, variant_monitor = simulator_facets_v2()
    await control.reset({"seed": 73})
    await variant.reset({"seed": 73})
    await control.begin_turn(0, 1)
    first_variant = await variant.begin_turn(0, 1)

    hidden = copy.deepcopy(variant_monitor.snapshot())
    visible_ids = {
        fact.subject.entity_id
        for fact in first_variant.facts
        if fact.subject.entity_type is EntityTypeV2.UNIT
    }
    hidden_enemy = next(
        unit
        for unit in hidden["units"].values()
        if unit["owner"] != 0 and unit["unit_id"] not in visible_ids
    )
    hidden_enemy["hp"] -= 7
    hidden_enemy["movement"] = max(0, hidden_enemy["movement"] - 1)
    hidden["rng"][1][1] ^= 1
    variant_monitor.restore(hidden)
    assert control_monitor.state_hash() != variant_monitor.state_hash()

    control_observation = await control.observe(0)
    variant_observation = await variant.observe(0)
    assert control_observation.to_doc() == variant_observation.to_doc()
    assert (
        ActionEnumeratorV2().enumerate(control_observation)
        == ActionEnumeratorV2().enumerate(variant_observation)
    )


@pytest.mark.asyncio
async def test_policy_facet_has_no_private_monitor_or_raw_adapter_surface() -> None:
    policy_facet, private_monitor = simulator_facets_v2()
    await policy_facet.reset({"seed": 7})
    await policy_facet.begin_turn(0, 1)

    assert dir(policy_facet) == [
        "begin_turn",
        "descriptor",
        "execute_authorized",
        "observe",
        "reset",
    ]
    for prohibited in (
        "act",
        "adapter",
        "debug",
        "drain_mutations",
        "export_state",
        "import_state",
        "restore",
        "snapshot",
        "state_hash",
    ):
        assert not hasattr(policy_facet, prohibited)
    assert private_monitor.snapshot()
    assert private_monitor.state_hash()


@pytest.mark.asyncio
async def test_stale_authorization_is_refused_before_adapter_mutation() -> None:
    policy_facet, private_monitor = simulator_facets_v2()
    await policy_facet.reset({"seed": 41})
    stale = await policy_facet.begin_turn(0, 1)
    action = next(
        item
        for item in ActionEnumeratorV2().enumerate(stale).actions
        if item.action_kind is ActionKindV2.SET_RESEARCH
    )
    authorization = _authorization(action)
    before = private_monitor.state_hash()
    await policy_facet.observe(0)

    with pytest.raises(ContractError, match="stale observation authorization"):
        await policy_facet.execute_authorized(authorization, action)
    assert private_monitor.state_hash() == before


@pytest.mark.asyncio
async def test_accepted_mutation_invalidates_its_observation_authority() -> None:
    policy_facet, private_monitor = simulator_facets_v2()
    await policy_facet.reset({"seed": 41})
    observation = await policy_facet.begin_turn(0, 1)
    action = next(
        item
        for item in ActionEnumeratorV2().enumerate(observation).actions
        if item.action_kind is ActionKindV2.SET_RESEARCH
    )
    authorization = _authorization(action)
    before = private_monitor.state_hash()

    result = await policy_facet.execute_authorized(authorization, action)
    assert result.status is ActionStatusV2.ACCEPTED
    assert private_monitor.state_hash() != before
    with pytest.raises(ContractError, match="stale observation authorization"):
        await policy_facet.execute_authorized(authorization, action)


@pytest.mark.asyncio
async def test_mandatory_choice_blocks_end_turn_even_if_action_is_forged() -> None:
    policy_facet, private_monitor = simulator_facets_v2()
    await policy_facet.reset({"seed": 41})
    observation = await policy_facet.begin_turn(0, 1)
    assert ActionKindV2.SET_RESEARCH in observation.mandatory_action_kinds
    assert all(
        action.action_kind is not ActionKindV2.END_TURN
        for action in ActionEnumeratorV2().enumerate(observation).actions
    )
    forged = LegalActionV2.create(
        ActionKindV2.END_TURN,
        observation.observing_player,
        observation.observation_id,
        terminal=True,
    )
    before = private_monitor.state_hash()

    result = await policy_facet.execute_authorized(_authorization(forged), forged)
    assert result.status is ActionStatusV2.REJECTED
    assert result.rejection_code == "mandatory_unresolved"
    assert private_monitor.state_hash() == before


class _ExplodingSimulator(SimulatorAdapter):
    async def act(self, cmd: ActionCommand) -> Any:
        _ = cmd
        raise RuntimeError("provider-token=DO-NOT-EXPOSE hidden-coordinate=99,99")


@pytest.mark.asyncio
async def test_adapter_exception_is_collapsed_to_a_safe_bounded_result() -> None:
    reference, _ = simulator_facets_v2()
    policy_facet, _ = split_adapter_v2(_ExplodingSimulator(), reference.descriptor)
    await policy_facet.reset({"seed": 41})
    observation = await policy_facet.begin_turn(0, 1)
    action = next(
        item
        for item in ActionEnumeratorV2().enumerate(observation).actions
        if item.action_kind is ActionKindV2.SET_RESEARCH
    )

    result = await policy_facet.execute_authorized(_authorization(action), action)
    assert result.status is ActionStatusV2.DIVERGED
    assert result.rejection_code == "adapter_failure"
    assert result.safe_message == "action outcome could not be verified"
    assert "DO-NOT-EXPOSE" not in repr(result)
    assert "99,99" not in repr(result)


@pytest.mark.asyncio
async def test_graph_overflow_fails_closed_without_truncating() -> None:
    policy_facet, _ = simulator_facets_v2()
    await policy_facet.reset({"seed": 41})
    observation = await policy_facet.begin_turn(0, 1)
    with pytest.raises(GraphOverflowError, match="complete legal set has"):
        ActionEnumeratorV2(max_graph_actions=1).enumerate(observation)


def test_live_adapter_uses_same_contract_with_explicit_verified_identity() -> None:
    policy_facet, private_monitor = firetuner_facets_v2(
        SimulatorAdapter(),
        adapter_version="firetuner-fixture-1",
        game_version="civ6-fixture-1",
        ruleset_digest="c" * 64,
        mod_digest="d" * 64,
    )
    descriptor = policy_facet.descriptor
    assert descriptor.adapter_kind is AdapterKindV2.FIRETUNER
    assert descriptor.game_version == "civ6-fixture-1"
    assert descriptor.ruleset_digest == "c" * 64
    assert descriptor.mod_digest == "d" * 64
    assert descriptor.deterministic is False
    assert EnvironmentCapabilityV2.LIVE_OBSERVATIONAL_REPLAY in descriptor.capabilities
    assert private_monitor is not policy_facet


@pytest.mark.asyncio
async def test_firetuner_rehearsal_projects_and_enumerates_through_v2() -> None:
    server = FakeTunerServer(mod=FakeMod())
    port = await server.start()
    adapter = FireTunerAdapter(
        "127.0.0.1",
        port,
        simulate_hook=_fake_live_hook,
        poll_timeout_s=2.0,
    )
    policy_facet, _ = firetuner_facets_v2(
        adapter,
        adapter_version="firetuner-rehearsal-1",
        game_version="civ6-rehearsal-1",
        ruleset_digest="c" * 64,
        mod_digest="d" * 64,
    )
    try:
        await policy_facet.reset({})
        observation = await policy_facet.begin_turn(0, 1)
        action_set = ActionEnumeratorV2().enumerate(observation)
        assert observation.environment_id == policy_facet.descriptor.descriptor_id
        assert all(
            action.observation_id == observation.observation_id
            for action in action_set.actions
        )
        assert all(isinstance(action.action_kind, ActionKindV2) for action in action_set.actions)
        purchase_costs = [
            fact.value.value
            for fact in observation.facts
            if fact.predicate == "purchase_cost"
        ]
        assert purchase_costs and all(isinstance(cost, int) for cost in purchase_costs)
        assert any(action.action_kind is ActionKindV2.PURCHASE for action in action_set.actions)
    finally:
        await adapter.teardown()
        await server.stop()


def test_split_adapter_refuses_a_capability_underclaimed_descriptor() -> None:
    reference, _ = simulator_facets_v2()
    source = reference.descriptor
    descriptor = EnvironmentDescriptorV2.create(
        adapter_kind=source.adapter_kind,
        adapter_version=source.adapter_version,
        game_version=source.game_version,
        ruleset_digest=source.ruleset_digest,
        mod_digest=source.mod_digest,
        capabilities=[
            capability
            for capability in source.capabilities
            if capability is not EnvironmentCapabilityV2.ACTION_EXECUTION
        ],
        deterministic=source.deterministic,
    )

    with pytest.raises(ContractError, match="lacks required environment capabilities"):
        split_adapter_v2(SimulatorAdapter(), descriptor)
