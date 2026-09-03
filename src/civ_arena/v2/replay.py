"""Strict V2 ledger semantics and deterministic fake-engine replay.

Replay rebuilds the fake environment from the terminal receipt's seed, issues
only ledger-authorized registered actions, and compares every recorded
player-scoped observation, legal set, and graph to a fresh derivation.  Private
referee state is never loaded from or persisted to the episode.  Its final hash
is returned only as an in-process diagnostic.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from civ_arena.game.sim.chaos import ChaosDirector, ChaosEvent, MutationSpec
from civ_arena.v2.contracts import (
    ActionGraphV2,
    ActionKindV2,
    ActionResultV2,
    ActionStatusV2,
    AdapterKindV2,
    ArtifactRefV2,
    AuthorizationDecisionV2,
    AuthorizationReasonV2,
    AuthorizationV2,
    EnvironmentCapabilityV2,
    EpisodeConfigV2,
    EpisodeReceiptV2,
    EventTypeV2,
    LegalActionSetV2,
    LegalActionV2,
    ObservationPhaseV2,
    ObservationV2,
    RejectionCodeV2,
    TurnProposalV2,
    TurnReceiptV2,
    TurnTerminationV2,
    VerificationStatusV2,
)
from civ_arena.v2.enumeration import ActionEnumeratorV2
from civ_arena.v2.environment import (
    EnvironmentExecutionV2,
    ObservableExecutionFacetV2,
    PrivateRefereeMonitorV2,
    simulator_facets_v2,
)
from civ_arena.v2.executor import (
    TransactionalExecutorV2,
    verify_observable_postconditions,
)
from civ_arena.v2.graph import ActionGraphCompilerV2
from civ_arena.v2.ledger import (
    LedgerIntegrityError,
    ObjectStoreV2,
    load_events_v2,
    verify_ledger_v2,
)
from civ_arena.v2.resume import resume_checkpoint_sequence_v2


class ExactReplayError(LedgerIntegrityError):
    """A structurally valid V2 episode diverged during deterministic replay."""


@dataclass(frozen=True)
class ExactFakeReplayV2:
    episode_id: str
    event_count: int
    turn_receipt_count: int
    action_count: int
    observation_count: int
    final_observation_id: str | None
    final_private_state_hash: str


def _episode_config_from_events(
    root: Path,
    events: list[Any],
) -> EpisodeConfigV2 | None:
    """Load the single early config artifact when the producer emits one.

    CAR-106's low-level replay fixtures predate this custody event and remain
    readable as the no-chaos default. ArenaV2-produced episodes always emit it;
    resume separately requires it.
    """

    config_events = [
        event
        for event in events[1:-1]
        if event.event_type is EventTypeV2.EPISODE_CONFIG_RECORDED
    ]
    if not config_events:
        return None
    if len(config_events) != 1 or config_events[0].sequence_number != 1:
        raise ExactReplayError(
            "episode config must be the unique event after EpisodeStarted"
        )
    event = config_events[0]
    if event.turn_id is not None or not isinstance(event.payload_value, ArtifactRefV2):
        raise ExactReplayError("episode config event has an invalid envelope")
    return _artifact_model(
        ObjectStoreV2(root),
        event.payload_value,
        EpisodeConfigV2,
    )


def _chaos_director(config: EpisodeConfigV2) -> ChaosDirector:
    return ChaosDirector(
        [
            ChaosEvent(
                MutationSpec(item.spec.value),
                hook=item.hook.value,
                offset=item.offset,
            )
            for item in config.chaos
        ]
    )


def _artifact_model[ModelT](
    store: ObjectStoreV2,
    ref: ArtifactRefV2,
    model: type[ModelT],
) -> ModelT:
    schema_ref = model.SCHEMA_REF  # type: ignore[attr-defined]
    if ref.schema_ref != schema_ref:
        raise ExactReplayError(
            f"artifact type mismatch: expected {schema_ref}, got {ref.schema_ref}"
        )
    parser = model.from_doc  # type: ignore[attr-defined]
    try:
        return parser(store.read_doc(ref))
    except LedgerIntegrityError:
        raise
    except Exception as exc:
        raise ExactReplayError(f"artifact failed typed construction: {schema_ref}") from exc


def _player_id(observation: ObservationV2) -> int:
    ref = observation.observing_player
    if ref.entity_id.startswith("p") and ref.entity_id[1:].isdigit():
        return int(ref.entity_id[1:])
    raise ExactReplayError("observation has no canonical numeric player identity")


def _intent_matches(action: LegalActionV2, proposal: TurnProposalV2, intent_id: str) -> bool:
    intent = next((item for item in proposal.intents if item.intent_id == intent_id), None)
    return bool(
        intent is not None
        and intent.action_kind is action.action_kind
        and intent.actor == action.actor
        and intent.target == action.target
        and intent.parameters == action.parameters
    )


def _matching_actions(
    graph: ActionGraphV2,
    proposal: TurnProposalV2,
    authorization: AuthorizationV2,
) -> list[LegalActionV2]:
    return [
        node.action
        for node in graph.nodes
        if _intent_matches(node.action, proposal, authorization.intent_id)
    ]


def _assert_authorization(
    authorization: AuthorizationV2,
    observation: ObservationV2,
    graph: ActionGraphV2,
    proposal: TurnProposalV2,
) -> LegalActionV2 | None:
    if authorization.observation_id != observation.observation_id:
        raise ExactReplayError("authorization is not bound to the current observation")
    if authorization.graph_id != graph.graph_id:
        raise ExactReplayError("authorization is not bound to the current graph")
    matches = _matching_actions(graph, proposal, authorization)
    if authorization.decision is AuthorizationDecisionV2.AUTHORIZED:
        if len(matches) != 1 or authorization.action_id != matches[0].action_id:
            raise ExactReplayError("authorized intent does not map to one current action")
        return matches[0]
    if authorization.action_id is not None:
        raise ExactReplayError("refused authorization unexpectedly names an action")
    intent = next(
        (item for item in proposal.intents if item.intent_id == authorization.intent_id),
        None,
    )
    expected_reason = (
        AuthorizationReasonV2.MANDATORY_UNRESOLVED
        if (
            not matches
            and intent is not None
            and intent.action_kind is ActionKindV2.END_TURN
            and observation.mandatory_action_kinds
        )
        else AuthorizationReasonV2.ACTION_ABSENT
        if not matches
        else AuthorizationReasonV2.AMBIGUOUS_INTENT
        if len(matches) > 1
        else None
    )
    if expected_reason is not None and authorization.reason_code is not expected_reason:
        raise ExactReplayError("refused authorization reason does not match current graph")
    return None


def _assert_recorded_result(
    result: ActionResultV2,
    authorization: AuthorizationV2,
    action: LegalActionV2,
    pre_observation: ObservationV2,
    post_observation: ObservationV2 | None,
    execution: EnvironmentExecutionV2,
) -> None:
    if result.action_id != action.action_id:
        raise ExactReplayError("action result names a different action")
    if result.authorization_id != authorization.authorization_id:
        raise ExactReplayError("action result names a different authorization")
    if result.pre_observation_id != pre_observation.observation_id:
        raise ExactReplayError("action result pre-observation mismatch")
    expected_verification = (
        verify_observable_postconditions(action, post_observation)
        if post_observation is not None
        else VerificationStatusV2.NOT_VERIFIABLE
    )
    accepted_divergence = (
        execution.status in {ActionStatusV2.ACCEPTED, ActionStatusV2.DUPLICATE}
        and result.status is ActionStatusV2.DIVERGED
        and expected_verification is VerificationStatusV2.MISMATCHED
        and result.rejection_code is RejectionCodeV2.POSTCONDITION_DIVERGED
    )
    if result.status is not execution.status and not accepted_divergence:
        raise ExactReplayError(
            f"fake execution status diverged: {result.status.value} != "
            f"{execution.status.value}"
        )
    if result.status in {
        ActionStatusV2.ACCEPTED,
        ActionStatusV2.DUPLICATE,
        ActionStatusV2.DIVERGED,
    } and post_observation is not None:
        if result.post_observation_id != post_observation.observation_id:
            raise ExactReplayError("action result post-observation mismatch")
    elif result.status in {ActionStatusV2.ACCEPTED, ActionStatusV2.DUPLICATE}:
        if post_observation is None:
            raise ExactReplayError("successful action has no recorded post observation")
    elif result.post_observation_id is not None:
        raise ExactReplayError("unsuccessful action carries a post observation")
    if post_observation is not None and result.verification is not expected_verification:
        raise ExactReplayError("action result postcondition verification mismatch")
    if result.observable_effects != action.expected_effects:
        raise ExactReplayError("action result does not carry the exact registered effects")


async def replay_fake_episode_v2(
    episode_dir: Path | str,
    *,
    expected_environment_id: str | None = None,
    environment: ObservableExecutionFacetV2 | None = None,
    private_monitor: PrivateRefereeMonitorV2 | None = None,
    through_sequence: int | None = None,
    reset_config: Mapping[str, Any] | None = None,
    parent_episode_dir: Path | str | None = None,
) -> ExactFakeReplayV2:
    """Verify and deterministically re-execute one terminal fake V2 episode.

    ``through_sequence`` is the child-resume seam: the full parent ledger is
    still verified first, but execution stops at a completed phase checkpoint.
    Callers may supply their own split facets so the reconstructed private state
    remains in process and can seed a child episode without ever being stored.
    """

    root = Path(episode_dir)
    structural = verify_ledger_v2(
        root,
        expected_environment_id=expected_environment_id,
        require_terminal=True,
    )
    events = load_events_v2(root / "events.jsonl")
    terminal = events[-1].payload_value
    if not isinstance(terminal, EpisodeReceiptV2):
        raise ExactReplayError("terminal event does not embed an episode receipt")
    if (
        terminal.environment.adapter_kind is not AdapterKindV2.FAKE
        or not terminal.environment.deterministic
        or EnvironmentCapabilityV2.DETERMINISTIC_FAKE_REPLAY
        not in terminal.environment.capabilities
    ):
        raise ExactReplayError("exact replay requires a deterministic fake environment")
    if terminal.seed is None:
        raise ExactReplayError("exact fake replay requires the recorded seed")
    episode_config = _episode_config_from_events(root, events)

    if (environment is None) != (private_monitor is None):
        raise ExactReplayError("replay environment facets must be supplied together")
    if environment is None or private_monitor is None:
        environment, private_monitor = simulator_facets_v2()
    if environment.descriptor != terminal.environment:
        raise ExactReplayError("runtime fake environment identity does not match episode")
    if terminal.parent_episode_id is not None:
        parent_root = (
            Path(parent_episode_dir)
            if parent_episode_dir is not None
            else root.parent / terminal.parent_episode_id
        )
        if parent_root.resolve() == root.resolve():
            raise ExactReplayError("child episode cannot name itself as its parent")
        parent_events = load_events_v2(parent_root / "events.jsonl")
        if not parent_events or parent_events[-1].event_hash != (
            terminal.parent_terminal_event_hash
        ):
            raise ExactReplayError("child episode parent terminal hash mismatch")
        parent_terminal = parent_events[-1].payload_value
        if not isinstance(parent_terminal, EpisodeReceiptV2):
            raise ExactReplayError("child episode parent has no terminal receipt")
        if (
            parent_terminal.environment != terminal.environment
            or parent_terminal.seed != terminal.seed
            or parent_terminal.compute != terminal.compute
            or parent_terminal.policies != terminal.policies
        ):
            raise ExactReplayError("child episode changed its parent runtime identity")
        if _episode_config_from_events(parent_root, parent_events) != episode_config:
            raise ExactReplayError("child episode changed its parent episode config")
        await replay_fake_episode_v2(
            parent_root,
            expected_environment_id=terminal.environment.descriptor_id,
            environment=environment,
            private_monitor=private_monitor,
            through_sequence=resume_checkpoint_sequence_v2(parent_root),
            reset_config=reset_config,
        )
    else:
        supplied = dict(reset_config or {})
        if set(supplied) - {"seed"}:
            raise ExactReplayError("exact replay reset accepts only the recorded seed")
        if supplied.get("seed", terminal.seed) != terminal.seed:
            raise ExactReplayError("replay reset seed does not match episode receipt")
        config: dict[str, Any] = {"seed": terminal.seed}
        if episode_config is not None:
            config["chaos_director"] = _chaos_director(episode_config)
        await environment.reset(config)

    if through_sequence is not None:
        if not 0 <= through_sequence < events[-1].sequence_number:
            raise ExactReplayError("replay checkpoint sequence is outside the parent body")
        replay_events = [
            event
            for event in events[1:-1]
            if event.sequence_number <= through_sequence
        ]
    else:
        replay_events = events[1:-1]

    store = ObjectStoreV2(root)
    enumerator = ActionEnumeratorV2(terminal.compute.max_graph_actions)
    compiler = ActionGraphCompilerV2(terminal.compute.max_graph_actions)
    dispatcher = TransactionalExecutorV2(
        environment,
        terminal.policies[0],
        episode_id=terminal.episode_id,
        max_graph_actions=terminal.compute.max_graph_actions,
        max_replans_per_turn=terminal.compute.max_replans_per_turn,
    )

    current_observation: ObservationV2 | None = None
    current_actions: LegalActionSetV2 | None = None
    current_graph: ActionGraphV2 | None = None
    current_proposal: TurnProposalV2 | None = None
    turn_initial_observation: ObservationV2 | None = None
    turn_graph_ids: list[str] = []
    turn_authorizations: list[AuthorizationV2] = []
    turn_results: list[ActionResultV2] = []
    turn_executed_actions: list[LegalActionV2] = []
    pending_authorization: AuthorizationV2 | None = None
    pending_action: LegalActionV2 | None = None
    pending_pre_observation: ObservationV2 | None = None
    pending_execution: EnvironmentExecutionV2 | None = None
    pending_post_observation: ObservationV2 | None = None
    completed_result: ActionResultV2 | None = None
    postcondition_seen = False
    invalidation_seen = False
    awaiting_turn_start = True
    continuation_expected = False
    turn_receipt_count = 0
    completed_turn_count = 0
    action_count = 0
    observation_count = 0
    final_observation_id: str | None = None

    for event in replay_events:
        event_type = event.event_type
        if event_type in {
            EventTypeV2.EPISODE_CONFIG_RECORDED,
            EventTypeV2.POLICY_SPEND_RECORDED,
            EventTypeV2.POLICY_STATE_RECORDED,
            EventTypeV2.VALIDATION_RECORDED,
        }:
            continue
        if event.turn_id is None:
            raise ExactReplayError(f"{event_type.value} is missing its turn id")

        if event_type is EventTypeV2.OBSERVATION_RECORDED:
            if not isinstance(event.payload_value, ArtifactRefV2):
                raise ExactReplayError("ObservationRecorded lacks an artifact")
            recorded = _artifact_model(store, event.payload_value, ObservationV2)
            if recorded.turn != event.turn_id:
                raise ExactReplayError("observation turn does not match event envelope")
            player_id = _player_id(recorded)
            if awaiting_turn_start:
                replayed = await environment.begin_turn(player_id, recorded.turn)
                turn_initial_observation = replayed
                turn_graph_ids = []
                turn_authorizations = []
                turn_results = []
                turn_executed_actions = []
                current_proposal = None
                awaiting_turn_start = False
            elif continuation_expected:
                replayed = await environment.observe(player_id)
                turn_initial_observation = replayed
                turn_graph_ids = []
                turn_authorizations = []
                turn_results = []
                turn_executed_actions = []
                current_proposal = None
                continuation_expected = False
            elif pending_execution is not None and pending_execution.status in {
                ActionStatusV2.ACCEPTED,
                ActionStatusV2.DUPLICATE,
            }:
                replayed = await environment.observe(player_id)
                pending_post_observation = replayed
            else:
                raise ExactReplayError("unexpected observation without a mutation or turn start")
            if replayed != recorded:
                raise ExactReplayError(
                    f"fake post-state observation diverged at event {event.sequence_number}"
                )
            current_observation = replayed
            current_actions = None
            current_graph = None
            observation_count += 1
            final_observation_id = replayed.observation_id
            continue

        if event_type is EventTypeV2.LEGAL_ACTIONS_RECORDED:
            if completed_result is not None and (
                not postcondition_seen
                or (
                    completed_result.status
                    in {
                        ActionStatusV2.ACCEPTED,
                        ActionStatusV2.DUPLICATE,
                        ActionStatusV2.DIVERGED,
                    }
                    and not invalidation_seen
                )
            ):
                raise ExactReplayError("new legal set precedes completed action closure")
            if current_observation is None or not isinstance(
                event.payload_value, ArtifactRefV2
            ):
                raise ExactReplayError("LegalActionsRecorded has no current observation")
            recorded = _artifact_model(store, event.payload_value, LegalActionSetV2)
            replayed = enumerator.enumerate(current_observation)
            if replayed != recorded:
                raise ExactReplayError("recorded legal actions differ from fresh enumeration")
            current_actions = replayed
            continue

        if event_type is EventTypeV2.ACTION_GRAPH_COMPILED:
            if (
                current_observation is None
                or current_actions is None
                or not isinstance(event.payload_value, ArtifactRefV2)
            ):
                raise ExactReplayError("ActionGraphCompiled lacks current legal actions")
            recorded = _artifact_model(store, event.payload_value, ActionGraphV2)
            replayed = compiler.compile(current_observation, current_actions)
            if replayed != recorded:
                raise ExactReplayError("recorded graph differs from fresh compilation")
            current_graph = replayed
            turn_graph_ids.append(replayed.graph_id)
            continue

        if event_type is EventTypeV2.POLICY_PROPOSAL_RECORDED:
            if current_observation is None or not isinstance(
                event.payload_value, ArtifactRefV2
            ):
                raise ExactReplayError("PolicyProposalRecorded has no current observation")
            proposal = _artifact_model(store, event.payload_value, TurnProposalV2)
            if proposal.observation_id != current_observation.observation_id:
                raise ExactReplayError("proposal is stale at its recorded policy boundary")
            if proposal.policy_id not in {
                policy.descriptor_id for policy in terminal.policies
            }:
                raise ExactReplayError("proposal policy is absent from episode receipt")
            if current_proposal is not None:
                raise ExactReplayError("turn contains more than one proposal identity")
            current_proposal = proposal
            continue

        if event_type is EventTypeV2.ACTION_AUTHORIZED:
            if (
                current_observation is None
                or current_graph is None
                or current_proposal is None
                or not isinstance(event.payload_value, AuthorizationV2)
            ):
                raise ExactReplayError("ActionAuthorized lacks current turn context")
            authorization = event.payload_value
            action = _assert_authorization(
                authorization,
                current_observation,
                current_graph,
                current_proposal,
            )
            turn_authorizations.append(authorization)
            if action is not None:
                if pending_authorization is not None:
                    raise ExactReplayError("authorization overlapped an unfinished execution")
                if completed_result is not None and (
                    not postcondition_seen
                    or (
                        completed_result.status
                        in {
                            ActionStatusV2.ACCEPTED,
                            ActionStatusV2.DUPLICATE,
                            ActionStatusV2.DIVERGED,
                        }
                        and not invalidation_seen
                    )
                ):
                    raise ExactReplayError("authorization precedes completed action closure")
                pending_authorization = authorization
                pending_action = action
                pending_pre_observation = current_observation
                pending_post_observation = None
                pending_execution = None
                completed_result = None
                postcondition_seen = False
                invalidation_seen = False
            continue

        if event_type is EventTypeV2.ACTION_EXECUTION_STARTED:
            if (
                pending_authorization is None
                or pending_action is None
                or event.payload_value != pending_action.action_id
            ):
                raise ExactReplayError("ActionExecutionStarted does not name pending action")
            if pending_execution is not None:
                raise ExactReplayError("action execution started more than once")
            pending_execution = await dispatcher.replay_dispatch(
                pending_authorization,
                pending_action,
            )
            action_count += 1
            continue

        if event_type is EventTypeV2.ACTION_EXECUTION_COMPLETED:
            if (
                pending_authorization is None
                or pending_action is None
                or pending_pre_observation is None
                or pending_execution is None
                or not isinstance(event.payload_value, ActionResultV2)
            ):
                raise ExactReplayError("ActionExecutionCompleted has no pending execution")
            result = event.payload_value
            _assert_recorded_result(
                result,
                pending_authorization,
                pending_action,
                pending_pre_observation,
                pending_post_observation,
                pending_execution,
            )
            turn_results.append(result)
            turn_executed_actions.append(pending_action)
            completed_result = result
            pending_authorization = None
            pending_action = None
            pending_pre_observation = None
            pending_execution = None
            pending_post_observation = None
            continue

        if event_type is EventTypeV2.POSTCONDITION_VERIFIED:
            if (
                completed_result is None
                or not isinstance(event.payload_value, ActionResultV2)
                or event.payload_value != completed_result
            ):
                raise ExactReplayError("PostconditionVerified does not repeat completed result")
            if postcondition_seen:
                raise ExactReplayError("PostconditionVerified is duplicated")
            postcondition_seen = True
            continue

        if event_type is EventTypeV2.GRAPH_REGION_INVALIDATED:
            if completed_result is None or not postcondition_seen:
                raise ExactReplayError("graph invalidation precedes action completion")
            if completed_result.status is ActionStatusV2.REJECTED:
                raise ExactReplayError("rejected action cannot invalidate the graph")
            if event.payload_value != completed_result.action_id:
                raise ExactReplayError("graph invalidation does not name completed action")
            if invalidation_seen:
                raise ExactReplayError("graph invalidation is duplicated")
            invalidation_seen = True
            continue

        if event_type is EventTypeV2.TURN_COMPLETED:
            if not isinstance(event.payload_value, TurnReceiptV2):
                raise ExactReplayError("TurnCompleted lacks its turn receipt")
            if (
                pending_authorization is not None
                or turn_initial_observation is None
                or current_observation is None
                or current_proposal is None
            ):
                raise ExactReplayError("turn receipt closes an incomplete turn")
            if completed_result is not None and (
                not postcondition_seen
                or (
                    completed_result.status
                    in {
                        ActionStatusV2.ACCEPTED,
                        ActionStatusV2.DUPLICATE,
                        ActionStatusV2.DIVERGED,
                    }
                    and not invalidation_seen
                )
            ):
                raise ExactReplayError("turn receipt precedes completed action closure")
            receipt = event.payload_value
            if receipt.episode_id != terminal.episode_id or receipt.turn != event.turn_id:
                raise ExactReplayError("turn receipt identity does not match its event")
            if receipt.player_id != _player_id(turn_initial_observation):
                raise ExactReplayError("turn receipt player mismatch")
            if receipt.pre_observation_id != turn_initial_observation.observation_id:
                raise ExactReplayError("turn receipt pre-observation mismatch")
            if receipt.post_observation_id != current_observation.observation_id:
                raise ExactReplayError("turn receipt post-observation mismatch")
            if receipt.proposal_id != current_proposal.proposal_id:
                raise ExactReplayError("turn receipt proposal mismatch")
            if receipt.graph_ids != tuple(turn_graph_ids):
                raise ExactReplayError("turn receipt graph history mismatch")
            if receipt.authorizations != tuple(turn_authorizations):
                raise ExactReplayError("turn receipt authorization history mismatch")
            if receipt.results != tuple(turn_results):
                raise ExactReplayError("turn receipt result history mismatch")
            if not 0 <= receipt.replan_count <= terminal.compute.max_replans_per_turn:
                raise ExactReplayError("turn receipt replan count exceeds episode limit")
            if receipt.replan_count > max(0, len(turn_graph_ids) - 1):
                raise ExactReplayError("turn receipt replan count exceeds revalidations")
            if receipt.termination is TurnTerminationV2.COMPLETED:
                if (
                    not turn_executed_actions
                    or turn_executed_actions[-1].action_kind is not ActionKindV2.END_TURN
                    or turn_results[-1].status
                    not in {ActionStatusV2.ACCEPTED, ActionStatusV2.DUPLICATE}
                    or current_observation.phase is ObservationPhaseV2.TURN
                ):
                    raise ExactReplayError(
                        "completed turn lacks an accepted terminal end_turn"
                    )
                completed_turn_count += 1
                awaiting_turn_start = True
            else:
                if current_observation.phase is not ObservationPhaseV2.TURN:
                    raise ExactReplayError(
                        "non-completed turn receipt closed the environment phase"
                    )
                continuation_expected = True
            turn_receipt_count += 1
            current_actions = None
            current_graph = None
            current_proposal = None
            turn_initial_observation = None
            completed_result = None
            postcondition_seen = False
            invalidation_seen = False
            continue

        raise ExactReplayError(f"unexpected V2 event in fake replay: {event_type.value}")

    if pending_authorization is not None:
        raise ExactReplayError("episode terminates with an incomplete turn")
    if (
        through_sequence is None
        and terminal.termination_reason.value == "success"
        and not awaiting_turn_start
    ):
        raise ExactReplayError("successful episode terminates with an incomplete turn")
    if not awaiting_turn_start and not continuation_expected:
        raise ExactReplayError("episode terminates within an incomplete proposal")
    if through_sequence is not None and (
        not awaiting_turn_start or continuation_expected
    ):
        raise ExactReplayError("resume checkpoint is not a completed phase boundary")
    if through_sequence is None and terminal.turns_completed != completed_turn_count:
        raise ExactReplayError(
            "episode receipt turn count does not match completed turn receipts"
        )
    return ExactFakeReplayV2(
        episode_id=structural.episode_id,
        event_count=(
            structural.event_count
            if through_sequence is None
            else through_sequence + 1
        ),
        turn_receipt_count=turn_receipt_count,
        action_count=action_count,
        observation_count=observation_count,
        final_observation_id=final_observation_id,
        final_private_state_hash=private_monitor.state_hash(),
    )


async def _main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="civ-arena-v2-replay")
    parser.add_argument("episode_dir", type=Path)
    parser.add_argument("--environment-id", default=None)
    options = parser.parse_args(argv)
    result = await replay_fake_episode_v2(
        options.episode_dir,
        expected_environment_id=options.environment_id,
    )
    print(
        f"V2 REPLAY OK: {result.action_count} actions, "
        f"{result.observation_count} observations, "
        f"terminal event count {result.event_count}"
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_main_async()))


if __name__ == "__main__":
    main()
