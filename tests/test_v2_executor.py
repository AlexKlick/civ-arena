from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from civ_arena.v2 import (
    ActionGraphCompilerV2,
    ActionIntentV2,
    ActionKindV2,
    ActionStatusV2,
    AuthorizationDecisionV2,
    AuthorizationReasonV2,
    ComputeConfigV2,
    ContractError,
    EnvironmentExecutionV2,
    EpisodeTerminationV2,
    ExecutionModeV2,
    LegalActionV2,
    PolicyDescriptorV2,
    PolicyKindV2,
    RejectionCodeV2,
    TransactionalExecutorV2,
    TurnProposalV2,
    TurnTerminationV2,
    replay_fake_episode_v2,
    simulator_facets_v2,
)
from civ_arena.v2.enumeration import ActionEnumeratorV2
from civ_arena.v2.ledger import EpisodeRecorderV2, verify_ledger_v2

FIXED_TIME = "2026-09-02T18:00:00+00:00"


def _clock() -> str:
    return FIXED_TIME


def _policy(
    registered: list[ActionKindV2] | None = None,
) -> PolicyDescriptorV2:
    return PolicyDescriptorV2.create(
        policy_kind=PolicyKindV2.SCRIPTED,
        policy_version="executor-fixture-v2",
        registered_action_kinds=registered or list(ActionKindV2),
    )


def _intent(action: LegalActionV2, index: int) -> ActionIntentV2:
    return ActionIntentV2.create(
        action.action_kind,
        action.actor,
        target=action.target,
        parameters=dict(action.parameters),
        proposal_index=index,
    )


def _end_intent(actor: Any, index: int) -> ActionIntentV2:
    return ActionIntentV2.create(
        ActionKindV2.END_TURN,
        actor,
        proposal_index=index,
    )


async def _context(seed: int = 41):
    environment, monitor = simulator_facets_v2()
    await environment.reset({"seed": seed})
    observation = await environment.begin_turn(0, 1)
    legal = ActionEnumeratorV2().enumerate(observation)
    graph = ActionGraphCompilerV2().compile(observation, legal)
    return environment, monitor, observation, legal, graph


def _mandatory_plan(legal: Any, actor: Any) -> list[ActionIntentV2]:
    research = next(
        action
        for action in legal.actions
        if action.action_kind is ActionKindV2.SET_RESEARCH
    )
    independent = next(
        action
        for action in legal.actions
        if action.action_kind is ActionKindV2.FORTIFY
    )
    return [_intent(research, 0), _intent(independent, 1), _end_intent(actor, 2)]


@pytest.mark.asyncio
async def test_executor_revalidates_complete_graph_after_each_mutation() -> None:
    environment, _monitor, observation, legal, graph = await _context()
    policy = _policy()
    proposal = TurnProposalV2.create(
        policy_id=policy.descriptor_id,
        observation_id=observation.observation_id,
        intents=_mandatory_plan(legal, observation.observing_player),
    )
    receipt = await TransactionalExecutorV2(
        environment,
        policy,
        episode_id="executor-revalidation",
    ).execute(proposal, observation, graph)

    assert receipt.termination is TurnTerminationV2.COMPLETED
    assert [result.status for result in receipt.results] == [
        ActionStatusV2.ACCEPTED,
        ActionStatusV2.ACCEPTED,
        ActionStatusV2.ACCEPTED,
    ]
    assert len(receipt.graph_ids) == 3
    assert len(set(receipt.graph_ids)) == 3
    assert receipt.replan_count == 0
    assert receipt.results[-1].post_observation_id == receipt.post_observation_id


@pytest.mark.asyncio
async def test_executor_rejects_action_absent_from_current_graph() -> None:
    environment, monitor, observation, legal, graph = await _context()
    unit_action = next(
        action for action in legal.actions if action.action_kind is ActionKindV2.MOVE_UNIT
    )
    forged = ActionIntentV2.create(
        ActionKindV2.MOVE_UNIT,
        unit_action.actor,
        parameters={"unit_id": unit_action.actor.entity_id, "dest": "99,99"},
        proposal_index=0,
    )
    policy = _policy()
    proposal = TurnProposalV2.create(
        policy_id=policy.descriptor_id,
        observation_id=observation.observation_id,
        intents=[forged],
    )
    before = monitor.state_hash()
    receipt = await TransactionalExecutorV2(
        environment,
        policy,
        episode_id="executor-absent",
    ).execute(proposal, observation, graph)

    assert monitor.state_hash() == before
    assert receipt.results == ()
    assert len(receipt.authorizations) == 1
    assert receipt.authorizations[0].decision is AuthorizationDecisionV2.REFUSED
    assert receipt.authorizations[0].reason_code is AuthorizationReasonV2.ACTION_ABSENT


@pytest.mark.asyncio
async def test_end_turn_requires_freshly_resolved_mandatory_actions() -> None:
    environment, monitor, observation, _legal, graph = await _context()
    policy = _policy()
    proposal = TurnProposalV2.create(
        policy_id=policy.descriptor_id,
        observation_id=observation.observation_id,
        intents=[_end_intent(observation.observing_player, 0)],
    )
    before = monitor.state_hash()
    receipt = await TransactionalExecutorV2(
        environment,
        policy,
        episode_id="executor-mandatory",
    ).execute(proposal, observation, graph)

    assert monitor.state_hash() == before
    assert receipt.termination is TurnTerminationV2.MANDATORY_UNRESOLVED
    assert receipt.authorizations[0].reason_code is (
        AuthorizationReasonV2.MANDATORY_UNRESOLVED
    )
    assert receipt.results == ()


@pytest.mark.asyncio
async def test_unregistered_action_never_reaches_environment() -> None:
    environment, monitor, observation, legal, graph = await _context()
    research = next(
        action
        for action in legal.actions
        if action.action_kind is ActionKindV2.SET_RESEARCH
    )
    policy = _policy([ActionKindV2.END_TURN])
    proposal = TurnProposalV2.create(
        policy_id=policy.descriptor_id,
        observation_id=observation.observation_id,
        intents=[_intent(research, 0)],
    )
    before = monitor.state_hash()
    receipt = await TransactionalExecutorV2(
        environment,
        policy,
        episode_id="executor-unregistered",
    ).execute(proposal, observation, graph)

    assert monitor.state_hash() == before
    assert receipt.results == ()
    assert receipt.authorizations[0].reason_code is (
        AuthorizationReasonV2.UNREGISTERED_ACTION
    )


class _ExecutionWrapper:
    def __init__(self, delegate: Any, outcome: ActionStatusV2) -> None:
        self.delegate = delegate
        self.descriptor = delegate.descriptor
        self.outcome = outcome
        self.dispatches = 0

    async def reset(self, config: Any) -> None:
        await self.delegate.reset(config)

    async def begin_turn(self, player_id: int, turn: int):
        return await self.delegate.begin_turn(player_id, turn)

    async def observe(self, player_id: int):
        return await self.delegate.observe(player_id)

    async def execute_authorized(self, authorization: Any, action: Any):
        self.dispatches += 1
        if self.outcome is ActionStatusV2.ACCEPTED:
            # Deliberately report success without applying the mutation.
            return EnvironmentExecutionV2(ActionStatusV2.ACCEPTED)
        return EnvironmentExecutionV2(
            ActionStatusV2.DIVERGED,
            rejection_code="adapter_failure",
            safe_message="fixture outcome unknown",
        )


@pytest.mark.asyncio
async def test_unexpected_effect_emits_divergence_and_is_not_retried() -> None:
    delegate, _monitor, observation, legal, graph = await _context()
    environment = _ExecutionWrapper(delegate, ActionStatusV2.ACCEPTED)
    action = next(
        item for item in legal.actions if item.action_kind is ActionKindV2.SET_RESEARCH
    )
    policy = _policy()
    proposal = TurnProposalV2.create(
        policy_id=policy.descriptor_id,
        observation_id=observation.observation_id,
        intents=[_intent(action, 0)],
    )
    receipt = await TransactionalExecutorV2(
        environment,
        policy,
        episode_id="executor-postcondition",
    ).execute(proposal, observation, graph)

    assert environment.dispatches == 1
    assert receipt.termination is TurnTerminationV2.FAILED
    assert receipt.results[0].status is ActionStatusV2.DIVERGED
    assert receipt.results[0].rejection_code is (
        RejectionCodeV2.POSTCONDITION_DIVERGED
    )


@pytest.mark.asyncio
async def test_uncertain_adapter_outcome_is_never_blindly_retried() -> None:
    delegate, _monitor, observation, legal, graph = await _context()
    environment = _ExecutionWrapper(delegate, ActionStatusV2.DIVERGED)
    action = next(
        item for item in legal.actions if item.action_kind is ActionKindV2.SET_RESEARCH
    )
    policy = _policy()
    proposal = TurnProposalV2.create(
        policy_id=policy.descriptor_id,
        observation_id=observation.observation_id,
        intents=[_intent(action, 0)],
    )
    receipt = await TransactionalExecutorV2(
        environment,
        policy,
        episode_id="executor-unsafe-retry",
    ).execute(proposal, observation, graph)

    assert environment.dispatches == 1
    assert receipt.termination is TurnTerminationV2.FAILED
    assert receipt.results[0].status is ActionStatusV2.DIVERGED
    assert receipt.results[0].rejection_code is RejectionCodeV2.UNSAFE_RETRY_REFUSED


@pytest.mark.asyncio
async def test_last_moment_stale_authorization_is_receipted_without_mutation() -> None:
    environment, monitor, observation, legal, graph = await _context()
    action = next(
        item for item in legal.actions if item.action_kind is ActionKindV2.SET_RESEARCH
    )
    policy = _policy()
    proposal = TurnProposalV2.create(
        policy_id=policy.descriptor_id,
        observation_id=observation.observation_id,
        intents=[_intent(action, 0)],
    )
    await environment.observe(0)  # makes the supplied observation stale in the facet
    before = monitor.state_hash()
    receipt = await TransactionalExecutorV2(
        environment,
        policy,
        episode_id="executor-stale",
    ).execute(proposal, observation, graph)

    assert monitor.state_hash() == before
    assert receipt.results[0].status is ActionStatusV2.REJECTED
    assert receipt.results[0].rejection_code is RejectionCodeV2.STALE_AUTHORIZATION


@pytest.mark.asyncio
async def test_executor_receipt_and_ledger_carry_full_authorization_chain(
    tmp_path: Path,
) -> None:
    environment, monitor, observation, legal, graph = await _context()
    policy = _policy()
    recorder = EpisodeRecorderV2(
        tmp_path,
        episode_id="executor-ledger",
        environment=environment.descriptor,
        policies=[policy],
        seed=41,
        compute=ComputeConfigV2(ExecutionModeV2.DAG_TX, 1024, 2),
        scored=False,
        clock=_clock,
    )
    proposal = TurnProposalV2.create(
        policy_id=policy.descriptor_id,
        observation_id=observation.observation_id,
        intents=_mandatory_plan(legal, observation.observing_player),
    )
    receipt = await TransactionalExecutorV2(
        environment,
        policy,
        episode_id="executor-ledger",
        recorder=recorder,
    ).execute(proposal, observation, graph)
    recorder.terminate(EpisodeTerminationV2.SUCCESS, turns_completed=1)
    recorder.close()

    assert len(receipt.authorizations) == len(receipt.results) == 3
    assert all(
        result.authorization_id == authorization.authorization_id
        for result, authorization in zip(receipt.results, receipt.authorizations, strict=True)
    )
    assert verify_ledger_v2(tmp_path).termination_reason == "success"
    replayed = await replay_fake_episode_v2(tmp_path)
    assert replayed.action_count == 3
    assert replayed.final_private_state_hash == monitor.state_hash()


def test_only_executor_module_calls_v2_environment_mutation_method() -> None:
    root = Path(__file__).parents[1] / "src" / "civ_arena" / "v2"
    offenders: list[str] = []
    for path in sorted(root.glob("*.py")):
        if path.name in {"environment.py", "executor.py"}:
            continue
        if ".execute_authorized(" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert offenders == []


@pytest.mark.asyncio
async def test_executor_refuses_forged_or_stale_graph_before_dispatch() -> None:
    environment, monitor, observation, legal, graph = await _context()
    policy = _policy()
    proposal = TurnProposalV2.create(
        policy_id=policy.descriptor_id,
        observation_id=observation.observation_id,
        intents=[_intent(legal.actions[0], 0)],
    )
    newer = await environment.observe(0)
    newer_legal = ActionEnumeratorV2().enumerate(newer)
    newer_graph = ActionGraphCompilerV2().compile(newer, newer_legal)
    before = monitor.state_hash()
    with pytest.raises(ContractError, match="complete current legal graph"):
        await TransactionalExecutorV2(
            environment,
            policy,
            episode_id="executor-forged-graph",
        ).execute(proposal, observation, newer_graph)
    assert monitor.state_hash() == before
