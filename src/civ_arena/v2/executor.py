"""Graph-authoritative transactional execution for the V2 turn boundary.

Policies supply untrusted :class:`TurnProposalV2` values.  This module is the
only production V2 module that calls ``ObservableExecutionFacetV2``'s mutating
method.  Every accepted mutation invalidates its observation, so the complete
legal set and graph are rebuilt before another intent can be authorized.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from civ_arena.v2.contracts import (
    ActionGraphV2,
    ActionIntentV2,
    ActionKindV2,
    ActionResultV2,
    ActionStatusV2,
    AuthorizationDecisionV2,
    AuthorizationReasonV2,
    AuthorizationV2,
    EdgeKindV2,
    EffectKindV2,
    EventTypeV2,
    KnowledgeStateV2,
    LegalActionSetV2,
    LegalActionV2,
    ObservationPhaseV2,
    ObservationV2,
    PolicyDescriptorV2,
    RejectionCodeV2,
    TurnProposalV2,
    TurnReceiptV2,
    TurnTerminationV2,
    VerificationStatusV2,
)
from civ_arena.v2.enumeration import ActionEnumeratorV2, ObservationIndexV2
from civ_arena.v2.environment import EnvironmentExecutionV2, ObservableExecutionFacetV2
from civ_arena.v2.graph import (
    PROHIBITIVE_EDGE_KINDS,
    ActionGraphCompilerV2,
    GraphSelectionError,
    canonical_action_order,
)
from civ_arena.v2.ledger import EpisodeRecorderV2
from civ_arena.v2.schemas import ContractError


@dataclass(frozen=True)
class _MappedIntent:
    intent: ActionIntentV2
    action: LegalActionV2


def _intent_matches(intent: ActionIntentV2, action: LegalActionV2) -> bool:
    return (
        intent.action_kind is action.action_kind
        and intent.actor == action.actor
        and intent.target == action.target
        and intent.parameters == action.parameters
    )


def _player_id(observation: ObservationV2) -> int:
    entity_id = observation.observing_player.entity_id
    if entity_id.startswith("p") and entity_id[1:].isdigit():
        return int(entity_id[1:])
    raise ContractError("executor observation has no canonical player id")


def _bounded_error(message: str) -> str:
    cleaned = " ".join(message.split())
    return cleaned[:160] or "turn execution failed"


def verify_observable_postconditions(
    action: LegalActionV2,
    post_observation: ObservationV2,
) -> VerificationStatusV2:
    """Verify observable direct-effect claims without consulting private state."""

    if action.action_kind is ActionKindV2.END_TURN:
        return (
            VerificationStatusV2.MATCHED
            if post_observation.phase is not ObservationPhaseV2.TURN
            else VerificationStatusV2.MISMATCHED
        )

    index = ObservationIndexV2(post_observation)
    not_verifiable = False
    for effect in action.expected_effects:
        if effect.effect_kind is EffectKindV2.TERMINAL:
            not_verifiable = True
            continue
        if effect.effect_kind is EffectKindV2.DELETE:
            if index.has_entity(effect.subject):
                return VerificationStatusV2.MISMATCHED
            continue
        if effect.expected.state is not KnowledgeStateV2.KNOWN:
            not_verifiable = True
            continue
        entry = index.entry(effect.subject, effect.attribute)
        if entry is None or entry.value != effect.expected.value:
            return VerificationStatusV2.MISMATCHED
    return (
        VerificationStatusV2.NOT_VERIFIABLE
        if not_verifiable
        else VerificationStatusV2.MATCHED
    )


class TransactionalExecutorV2:
    """Map, authorize, dispatch, verify, and fully recompile one turn proposal."""

    def __init__(
        self,
        environment: ObservableExecutionFacetV2,
        policy: PolicyDescriptorV2,
        *,
        episode_id: str,
        max_graph_actions: int = 1024,
        max_replans_per_turn: int = 2,
        recorder: EpisodeRecorderV2 | None = None,
    ) -> None:
        if max_replans_per_turn < 0:
            raise ValueError("max_replans_per_turn must be non-negative")
        self._environment = environment
        self.policy = policy
        self.episode_id = episode_id
        self.max_replans_per_turn = max_replans_per_turn
        self.recorder = recorder
        self.enumerator = ActionEnumeratorV2(max_graph_actions)
        self.compiler = ActionGraphCompilerV2(max_graph_actions)

    async def replay_dispatch(
        self,
        authorization: AuthorizationV2,
        action: LegalActionV2,
    ) -> EnvironmentExecutionV2:
        """Dispatch one already-verified ledger action during exact replay.

        Replay deliberately enters through this executor-owned method so no
        policy or replay runtime becomes a second engine-mutation authority.
        """

        return await self._environment.execute_authorized(authorization, action)

    def _append(
        self,
        event_type: EventTypeV2,
        payload: Any,
        *,
        schema_ref: str,
        turn: int,
        correlation_id: str,
    ) -> None:
        if self.recorder is None:
            return
        self.recorder.ledger.append(
            event_type,
            schema_ref=schema_ref,
            payload_value=payload,
            turn_id=turn,
            correlation_id=correlation_id,
        )

    def _record_reference(
        self,
        event_type: EventTypeV2,
        model: Any,
        *,
        turn: int,
        correlation_id: str,
    ) -> None:
        if self.recorder is None:
            return
        self.recorder.record_reference(
            event_type,
            model,
            turn_id=turn,
            correlation_id=correlation_id,
        )

    @staticmethod
    def _authorization(
        intent: ActionIntentV2,
        observation: ObservationV2,
        graph: ActionGraphV2,
        *,
        action: LegalActionV2 | None,
        reason: AuthorizationReasonV2,
    ) -> AuthorizationV2:
        decision = (
            AuthorizationDecisionV2.AUTHORIZED
            if action is not None
            else AuthorizationDecisionV2.REFUSED
        )
        return AuthorizationV2.create(
            decision=decision,
            intent_id=intent.intent_id,
            action_id=action.action_id if action is not None else None,
            observation_id=observation.observation_id,
            graph_id=graph.graph_id,
            reason_code=reason,
        )

    def _record_authorization(
        self,
        authorization: AuthorizationV2,
        *,
        turn: int,
        correlation_id: str,
    ) -> None:
        self._append(
            EventTypeV2.ACTION_AUTHORIZED,
            authorization,
            schema_ref=AuthorizationV2.SCHEMA_REF,
            turn=turn,
            correlation_id=correlation_id,
        )

    def _refuse(
        self,
        intent: ActionIntentV2,
        observation: ObservationV2,
        graph: ActionGraphV2,
        reason: AuthorizationReasonV2,
        authorizations: list[AuthorizationV2],
        *,
        correlation_id: str,
    ) -> None:
        authorization = self._authorization(
            intent,
            observation,
            graph,
            action=None,
            reason=reason,
        )
        authorizations.append(authorization)
        self._record_authorization(
            authorization,
            turn=observation.turn,
            correlation_id=correlation_id,
        )

    @staticmethod
    def _matches(
        intent: ActionIntentV2,
        graph: ActionGraphV2,
    ) -> tuple[LegalActionV2, ...]:
        return tuple(
            node.action for node in graph.nodes if _intent_matches(intent, node.action)
        )

    def _select_current(
        self,
        remaining: list[ActionIntentV2],
        observation: ObservationV2,
        graph: ActionGraphV2,
        authorizations: list[AuthorizationV2],
        *,
        correlation_id: str,
    ) -> tuple[list[_MappedIntent], list[ActionIntentV2]]:
        """Return one graph-valid stable selection and still-absent intents."""

        registered = set(self.policy.registered_action_kinds)
        mapped: list[_MappedIntent] = []
        absent: list[ActionIntentV2] = []
        for intent in remaining:
            if intent.action_kind not in registered:
                self._refuse(
                    intent,
                    observation,
                    graph,
                    AuthorizationReasonV2.UNREGISTERED_ACTION,
                    authorizations,
                    correlation_id=correlation_id,
                )
                continue
            matches = self._matches(intent, graph)
            if not matches:
                absent.append(intent)
            elif len(matches) > 1:
                self._refuse(
                    intent,
                    observation,
                    graph,
                    AuthorizationReasonV2.AMBIGUOUS_INTENT,
                    authorizations,
                    correlation_id=correlation_id,
                )
            else:
                mapped.append(_MappedIntent(intent, matches[0]))

        # Two proposal intents for the same legal action are not two mutation
        # authorities. Choose by stable intent identity, never author position.
        by_action: dict[str, list[_MappedIntent]] = {}
        for item in mapped:
            by_action.setdefault(item.action.action_id, []).append(item)
        unique: list[_MappedIntent] = []
        for action_id in sorted(by_action):
            group = sorted(by_action[action_id], key=lambda item: item.intent.intent_id)
            unique.append(group[0])
            for duplicate in group[1:]:
                self._refuse(
                    duplicate.intent,
                    observation,
                    graph,
                    AuthorizationReasonV2.AMBIGUOUS_INTENT,
                    authorizations,
                    correlation_id=correlation_id,
                )

        item_by_action = {item.action.action_id: item for item in unique}
        prohibited: set[frozenset[str]] = {
            frozenset((edge.source_action_id, edge.target_action_id))
            for edge in graph.edges
            if edge.kind in PROHIBITIVE_EDGE_KINDS
        }
        selected: list[str] = []
        for action_id in sorted(item_by_action):
            if any(frozenset((action_id, prior)) in prohibited for prior in selected):
                self._refuse(
                    item_by_action[action_id].intent,
                    observation,
                    graph,
                    AuthorizationReasonV2.CONFLICT,
                    authorizations,
                    correlation_id=correlation_id,
                )
            else:
                selected.append(action_id)

        # A REQUIRES target cannot manufacture its missing source. Remove it
        # deterministically; repeat because one removal may orphan another.
        selected_set = set(selected)
        while True:
            orphaned = {
                edge.target_action_id
                for edge in graph.edges
                if edge.kind is EdgeKindV2.REQUIRES
                and edge.target_action_id in selected_set
                and edge.source_action_id not in selected_set
            }
            if not orphaned:
                break
            for action_id in sorted(orphaned):
                selected_set.remove(action_id)
                self._refuse(
                    item_by_action[action_id].intent,
                    observation,
                    graph,
                    AuthorizationReasonV2.CONFLICT,
                    authorizations,
                    correlation_id=correlation_id,
                )

        try:
            ordered_ids = canonical_action_order(graph, selected_set)
        except GraphSelectionError as exc:
            raise ContractError("executor could not derive a graph-valid selection") from exc
        return [item_by_action[action_id] for action_id in ordered_ids], absent

    def _initial_context(
        self,
        proposal: TurnProposalV2,
        observation: ObservationV2,
        graph: ActionGraphV2,
    ) -> tuple[LegalActionSetV2, ActionGraphV2]:
        if proposal.policy_id != self.policy.descriptor_id:
            raise ContractError("proposal policy identity does not match executor policy")
        if proposal.observation_id != observation.observation_id:
            raise ContractError("proposal is not bound to the supplied observation")
        legal = self.enumerator.enumerate(observation)
        rebuilt = self.compiler.compile(observation, legal)
        if graph != rebuilt:
            raise ContractError("supplied graph is not the complete current legal graph")
        return legal, rebuilt

    def _turn_receipt(
        self,
        *,
        observation: ObservationV2,
        current_observation: ObservationV2,
        proposal: TurnProposalV2,
        graph_ids: Iterable[str],
        authorizations: Iterable[AuthorizationV2],
        results: Iterable[ActionResultV2],
        replan_count: int,
        termination: TurnTerminationV2,
        safe_error: str | None,
        correlation_id: str,
    ) -> TurnReceiptV2:
        receipt = TurnReceiptV2.create(
            episode_id=self.episode_id,
            turn=observation.turn,
            player_id=_player_id(observation),
            pre_observation_id=observation.observation_id,
            post_observation_id=current_observation.observation_id,
            proposal_id=proposal.proposal_id,
            graph_ids=tuple(graph_ids),
            authorizations=tuple(authorizations),
            results=tuple(results),
            replan_count=replan_count,
            termination=termination,
            safe_error=_bounded_error(safe_error) if safe_error is not None else None,
        )
        self._append(
            EventTypeV2.TURN_COMPLETED,
            receipt,
            schema_ref=TurnReceiptV2.SCHEMA_REF,
            turn=observation.turn,
            correlation_id=correlation_id,
        )
        return receipt

    async def execute(
        self,
        proposal: TurnProposalV2,
        observation: ObservationV2,
        graph: ActionGraphV2,
    ) -> TurnReceiptV2:
        """Execute a proposal through observation/graph-bound authorizations."""

        legal, current_graph = self._initial_context(proposal, observation, graph)
        current_observation = observation
        player_id = _player_id(observation)
        correlation_id = f"turn-{observation.turn}-p{player_id}"
        self._record_reference(
            EventTypeV2.OBSERVATION_RECORDED,
            observation,
            turn=observation.turn,
            correlation_id=correlation_id,
        )
        self._record_reference(
            EventTypeV2.LEGAL_ACTIONS_RECORDED,
            legal,
            turn=observation.turn,
            correlation_id=correlation_id,
        )
        self._record_reference(
            EventTypeV2.ACTION_GRAPH_COMPILED,
            current_graph,
            turn=observation.turn,
            correlation_id=correlation_id,
        )
        self._record_reference(
            EventTypeV2.POLICY_PROPOSAL_RECORDED,
            proposal,
            turn=observation.turn,
            correlation_id=correlation_id,
        )

        graph_ids = [current_graph.graph_id]
        authorizations: list[AuthorizationV2] = []
        results: list[ActionResultV2] = []
        remaining = list(proposal.intents)
        replan_count = 0

        while current_observation.phase is ObservationPhaseV2.TURN:
            selected, absent = self._select_current(
                remaining,
                current_observation,
                current_graph,
                authorizations,
                correlation_id=correlation_id,
            )
            selected_ids = {item.intent.intent_id for item in selected}
            absent_ids = {item.intent_id for item in absent}
            remaining = [
                intent
                for intent in remaining
                if intent.intent_id in selected_ids or intent.intent_id in absent_ids
            ]

            if not selected:
                mandatory = bool(current_observation.mandatory_action_kinds)
                for intent in absent:
                    reason = (
                        AuthorizationReasonV2.MANDATORY_UNRESOLVED
                        if mandatory and intent.action_kind is ActionKindV2.END_TURN
                        else AuthorizationReasonV2.ACTION_ABSENT
                    )
                    self._refuse(
                        intent,
                        current_observation,
                        current_graph,
                        reason,
                        authorizations,
                        correlation_id=correlation_id,
                    )
                termination = (
                    TurnTerminationV2.MANDATORY_UNRESOLVED
                    if mandatory
                    else TurnTerminationV2.REPLANS_EXHAUSTED
                    if replan_count >= self.max_replans_per_turn and replan_count > 0
                    else TurnTerminationV2.FAILED
                )
                message = (
                    "mandatory decisions remain unresolved"
                    if mandatory
                    else "proposal contains no current executable intent"
                )
                return self._turn_receipt(
                    observation=observation,
                    current_observation=current_observation,
                    proposal=proposal,
                    graph_ids=graph_ids,
                    authorizations=authorizations,
                    results=results,
                    replan_count=replan_count,
                    termination=termination,
                    safe_error=message,
                    correlation_id=correlation_id,
                )

            item = selected[0]
            action = item.action
            before_mapped = {
                candidate.intent.intent_id for candidate in selected[1:]
            }
            if action.action_kind is ActionKindV2.END_TURN:
                # Terminal execution is the last opportunity to disposition
                # every submitted intent. An action absent from every graph
                # must not silently disappear merely because end_turn closes
                # the phase before the loop can revisit it.
                for pending in remaining:
                    if pending.intent_id == item.intent.intent_id:
                        continue
                    matches = self._matches(pending, current_graph)
                    self._refuse(
                        pending,
                        current_observation,
                        current_graph,
                        (
                            AuthorizationReasonV2.ACTION_ABSENT
                            if not matches
                            else AuthorizationReasonV2.CONFLICT
                        ),
                        authorizations,
                        correlation_id=correlation_id,
                    )
                remaining = [item.intent]
            authorization = self._authorization(
                item.intent,
                current_observation,
                current_graph,
                action=action,
                reason=AuthorizationReasonV2.AUTHORIZED,
            )
            authorizations.append(authorization)
            self._record_authorization(
                authorization,
                turn=observation.turn,
                correlation_id=correlation_id,
            )
            self._append(
                EventTypeV2.ACTION_EXECUTION_STARTED,
                action.action_id,
                schema_ref=LegalActionV2.SCHEMA_REF,
                turn=observation.turn,
                correlation_id=correlation_id,
            )
            try:
                execution = await self.replay_dispatch(authorization, action)
            except ContractError:
                # The observable environment performs a second, last-moment
                # observation binding check. A race/stale cache therefore
                # becomes a typed non-execution receipt, never an adapter call.
                execution = EnvironmentExecutionV2(
                    ActionStatusV2.REJECTED,
                    rejection_code="stale_authorization",
                    safe_message="authorization became stale before dispatch",
                )
            remaining = [
                intent for intent in remaining if intent.intent_id != item.intent.intent_id
            ]

            if execution.status is ActionStatusV2.REJECTED:
                result = ActionResultV2.create(
                    status=ActionStatusV2.REJECTED,
                    action_id=action.action_id,
                    authorization_id=authorization.authorization_id,
                    pre_observation_id=current_observation.observation_id,
                    post_observation_id=None,
                    verification=VerificationStatusV2.NOT_EXECUTED,
                    rejection_code=(
                        RejectionCodeV2.STALE_AUTHORIZATION
                        if execution.rejection_code == "stale_authorization"
                        else RejectionCodeV2.ADAPTER_REJECTED
                    ),
                    safe_message=execution.safe_message,
                )
                results.append(result)
                self._append(
                    EventTypeV2.ACTION_EXECUTION_COMPLETED,
                    result,
                    schema_ref=ActionResultV2.SCHEMA_REF,
                    turn=observation.turn,
                    correlation_id=correlation_id,
                )
                self._append(
                    EventTypeV2.POSTCONDITION_VERIFIED,
                    result,
                    schema_ref=ActionResultV2.SCHEMA_REF,
                    turn=observation.turn,
                    correlation_id=correlation_id,
                )
                continue

            try:
                post_observation = await self._environment.observe(player_id)
            except Exception as exc:
                result = ActionResultV2.create(
                    status=ActionStatusV2.DIVERGED,
                    action_id=action.action_id,
                    authorization_id=authorization.authorization_id,
                    pre_observation_id=current_observation.observation_id,
                    post_observation_id=None,
                    verification=VerificationStatusV2.NOT_VERIFIABLE,
                    rejection_code=RejectionCodeV2.UNSAFE_RETRY_REFUSED,
                    safe_message="post-state observation unavailable; mutation not retried",
                )
                results.append(result)
                self._append(
                    EventTypeV2.ACTION_EXECUTION_COMPLETED,
                    result,
                    schema_ref=ActionResultV2.SCHEMA_REF,
                    turn=observation.turn,
                    correlation_id=correlation_id,
                )
                self._append(
                    EventTypeV2.POSTCONDITION_VERIFIED,
                    result,
                    schema_ref=ActionResultV2.SCHEMA_REF,
                    turn=observation.turn,
                    correlation_id=correlation_id,
                )
                return self._turn_receipt(
                    observation=observation,
                    current_observation=current_observation,
                    proposal=proposal,
                    graph_ids=graph_ids,
                    authorizations=authorizations,
                    results=results,
                    replan_count=replan_count,
                    termination=TurnTerminationV2.FAILED,
                    safe_error=f"post-state observation failed: {type(exc).__name__}",
                    correlation_id=correlation_id,
                )

            self._record_reference(
                EventTypeV2.OBSERVATION_RECORDED,
                post_observation,
                turn=observation.turn,
                correlation_id=correlation_id,
            )
            verification = verify_observable_postconditions(action, post_observation)
            status = execution.status
            rejection_code: RejectionCodeV2 | None = None
            safe_message = execution.safe_message
            if status is ActionStatusV2.DIVERGED:
                rejection_code = RejectionCodeV2.UNSAFE_RETRY_REFUSED
                safe_message = safe_message or "action outcome diverged; mutation not retried"
            elif verification is VerificationStatusV2.MISMATCHED:
                status = ActionStatusV2.DIVERGED
                rejection_code = RejectionCodeV2.POSTCONDITION_DIVERGED
                safe_message = "observable postcondition diverged"
            result = ActionResultV2.create(
                status=status,
                action_id=action.action_id,
                authorization_id=authorization.authorization_id,
                pre_observation_id=current_observation.observation_id,
                post_observation_id=post_observation.observation_id,
                verification=verification,
                observable_effects=action.expected_effects,
                rejection_code=rejection_code,
                safe_message=safe_message,
            )
            results.append(result)
            self._append(
                EventTypeV2.ACTION_EXECUTION_COMPLETED,
                result,
                schema_ref=ActionResultV2.SCHEMA_REF,
                turn=observation.turn,
                correlation_id=correlation_id,
            )
            self._append(
                EventTypeV2.POSTCONDITION_VERIFIED,
                result,
                schema_ref=ActionResultV2.SCHEMA_REF,
                turn=observation.turn,
                correlation_id=correlation_id,
            )
            self._append(
                EventTypeV2.GRAPH_REGION_INVALIDATED,
                action.action_id,
                schema_ref=ActionGraphV2.SCHEMA_REF,
                turn=observation.turn,
                correlation_id=correlation_id,
            )
            current_observation = post_observation

            if status is ActionStatusV2.DIVERGED:
                return self._turn_receipt(
                    observation=observation,
                    current_observation=current_observation,
                    proposal=proposal,
                    graph_ids=graph_ids,
                    authorizations=authorizations,
                    results=results,
                    replan_count=replan_count,
                    termination=TurnTerminationV2.FAILED,
                    safe_error=safe_message or "action diverged",
                    correlation_id=correlation_id,
                )

            if current_observation.phase is not ObservationPhaseV2.TURN:
                if action.action_kind is not ActionKindV2.END_TURN:
                    return self._turn_receipt(
                        observation=observation,
                        current_observation=current_observation,
                        proposal=proposal,
                        graph_ids=graph_ids,
                        authorizations=authorizations,
                        results=results,
                        replan_count=replan_count,
                        termination=TurnTerminationV2.FAILED,
                        safe_error="non-terminal action unexpectedly closed the turn",
                        correlation_id=correlation_id,
                    )
                return self._turn_receipt(
                    observation=observation,
                    current_observation=current_observation,
                    proposal=proposal,
                    graph_ids=graph_ids,
                    authorizations=authorizations,
                    results=results,
                    replan_count=replan_count,
                    termination=TurnTerminationV2.COMPLETED,
                    safe_error=None,
                    correlation_id=correlation_id,
                )

            legal = self.enumerator.enumerate(current_observation)
            self._record_reference(
                EventTypeV2.LEGAL_ACTIONS_RECORDED,
                legal,
                turn=observation.turn,
                correlation_id=correlation_id,
            )
            current_graph = self.compiler.compile(current_observation, legal)
            graph_ids.append(current_graph.graph_id)
            self._record_reference(
                EventTypeV2.ACTION_GRAPH_COMPILED,
                current_graph,
                turn=observation.turn,
                correlation_id=correlation_id,
            )
            still_mapped = {
                intent.intent_id
                for intent in remaining
                if len(self._matches(intent, current_graph)) == 1
            }
            if before_mapped - still_mapped:
                if replan_count >= self.max_replans_per_turn:
                    return self._turn_receipt(
                        observation=observation,
                        current_observation=current_observation,
                        proposal=proposal,
                        graph_ids=graph_ids,
                        authorizations=authorizations,
                        results=results,
                        replan_count=replan_count,
                        termination=TurnTerminationV2.REPLANS_EXHAUSTED,
                        safe_error="maximum stale-intent replans exhausted",
                        correlation_id=correlation_id,
                    )
                replan_count += 1

        return self._turn_receipt(
            observation=observation,
            current_observation=current_observation,
            proposal=proposal,
            graph_ids=graph_ids,
            authorizations=authorizations,
            results=results,
            replan_count=replan_count,
            termination=TurnTerminationV2.FAILED,
            safe_error="turn left active execution state",
            correlation_id=correlation_id,
        )
