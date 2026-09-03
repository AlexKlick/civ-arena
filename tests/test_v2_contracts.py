from __future__ import annotations

import copy
import datetime
import json
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from civ_arena.v2 import (
    PLAYER_ACTION_KINDS_V2,
    SYSTEM_ACTION_KINDS_V2,
    ActionGraphEdgeV2,
    ActionGraphMetricsV2,
    ActionGraphNodeV2,
    ActionGraphV2,
    ActionIntentV2,
    ActionKindV2,
    ActionResultV2,
    ActionStatusV2,
    AdapterKindV2,
    ArtifactRefV2,
    AuthorizationDecisionV2,
    AuthorizationReasonV2,
    AuthorizationV2,
    ChaosEventConfigV2,
    ChaosHookV2,
    ChaosSpecV2,
    ComputeConfigV2,
    ContractError,
    EdgeAuthorityV2,
    EdgeKindV2,
    EdgeReasonV2,
    EffectClaimV2,
    EffectKindV2,
    EntityRefV2,
    EntityTypeV2,
    EnvironmentCapabilityV2,
    EnvironmentDescriptorV2,
    EpisodeConfigV2,
    EpisodeReceiptV2,
    EpisodeTerminationV2,
    EventTypeV2,
    EventV2,
    ExecutionModeV2,
    FactSourceV2,
    FactSubjectScopeV2,
    KnowledgeValueV2,
    LegalActionSetV2,
    LegalActionV2,
    ObservableFactV2,
    ObservationPhaseV2,
    ObservationV2,
    PolicyDescriptorV2,
    PolicyKindV2,
    PolicyOperationV2,
    PolicyRuntimeKindV2,
    PolicySpendV2,
    PolicyStateV2,
    PreconditionClaimV2,
    PreconditionOperatorV2,
    RandomStateV2,
    ResourceClaimV2,
    ResourceKindV2,
    ResourceModeV2,
    TurnReceiptV2,
    TurnTerminationV2,
    VerificationStatusV2,
    WatchdogModeV2,
)
from civ_arena.v2.contracts import EDGE_KINDS_IN_ORDER, ZERO_DIGEST
from civ_arena.v2.schemas import schema_documents, validate_all_schemas

PLAYER = EntityRefV2(EntityTypeV2.PLAYER, "p0")
RULESET = "a" * 64


def _observation(*, gold: int = 0, seq: int = 0, mandatory: bool = False) -> ObservationV2:
    fact = ObservableFactV2(
        subject=PLAYER,
        subject_scope=FactSubjectScopeV2.SELF,
        predicate="gold",
        value=KnowledgeValueV2.known(gold, observed_turn=3),
        source=FactSourceV2.DIRECT,
    )
    return ObservationV2.create(
        environment_id="fake-v2",
        game_version="fake-1",
        ruleset_digest=RULESET,
        turn=3,
        active_player=PLAYER,
        observing_player=PLAYER,
        phase=ObservationPhaseV2.TURN,
        observed_at_seq=seq,
        facts=[fact],
        mandatory_action_kinds=[ActionKindV2.SET_RESEARCH] if mandatory else [],
    )


def _action(observation: ObservationV2, tech: str = "MINING") -> LegalActionV2:
    tech_ref = EntityRefV2(EntityTypeV2.TECHNOLOGY, tech)
    precondition = PreconditionClaimV2(
        subject=tech_ref,
        predicate="available",
        operator=PreconditionOperatorV2.EQ,
        expected=KnowledgeValueV2.known(True, observed_turn=observation.turn),
    )
    resource = ResourceClaimV2(
        resource=ResourceKindV2.RESEARCH_SLOT,
        owner=PLAYER,
        amount=1,
        mode=ResourceModeV2.RESERVE,
        region="player:p0:research",
    )
    effect = EffectClaimV2(
        effect_kind=EffectKindV2.SET,
        subject=PLAYER,
        attribute="researching",
        expected=KnowledgeValueV2.known(tech, observed_turn=observation.turn),
        region="player:p0:research",
    )
    return LegalActionV2.create(
        ActionKindV2.SET_RESEARCH,
        PLAYER,
        observation.observation_id,
        parameters={"tech_id": tech},
        preconditions=[precondition],
        resource_claims=[resource],
        expected_effects=[effect],
        affected_regions=["player:p0:research"],
        mandatory=True,
    )


def _metrics(edges: list[ActionGraphEdgeV2], count: int) -> ActionGraphMetricsV2:
    counts = {kind: 0 for kind in EDGE_KINDS_IN_ORDER}
    for edge in edges:
        counts[edge.kind] += 1
    return ActionGraphMetricsV2(
        legal_action_count=count,
        edge_count_by_kind=tuple((kind, counts[kind]) for kind in EDGE_KINDS_IN_ORDER),
        independent_action_groups=count,
        raw_permutation_estimate=2 if count > 1 else 1,
        canonical_plan_count=1,
        reduction_ratio_fixed=10_000,
        compile_duration_ms=0,
        revalidation_regions=(),
        rejected_reasons=(),
    )


def test_all_v2_schemas_are_draft_2020_12_and_closed() -> None:
    ids = validate_all_schemas()
    assert ids == tuple(sorted(schema_documents()))
    assert len(ids) == 9
    for schema_id, doc in schema_documents().items():
        assert schema_id.startswith("urn:civ-arena:") and schema_id.endswith(":2")
        assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert doc["additionalProperties"] is False


def test_policy_state_and_spend_are_strict_canonical_contracts() -> None:
    operation = PolicyOperationV2.create(
        agent_id="agent-0",
        player_id=0,
        turn=3,
        sequence=4,
        tool="write_diary",
        args={"text": "hold the river"},
        result={"status": "accepted", "tool": "write_diary", "chars": 14},
    )
    source_rng = random.Random(17)
    source_rng.gauss(0, 1)
    rng_state = RandomStateV2.from_random(source_rng)
    state = PolicyStateV2.create(
        policy_id="a" * 64,
        agent_id="agent-0",
        player_id=0,
        turn=3,
        proposal_id="b" * 64,
        runtime_kind=PolicyRuntimeKindV2.SCRIPTED,
        rng_state=rng_state,
        runtime_state={"trace": []},
        operations=[operation],
        telemetry={"agent-0": {"total_calls": 1}},
        model_posts=2,
    )
    assert PolicyStateV2.from_doc(state.to_doc()) == state
    assert rng_state.to_random().getstate() == source_rng.getstate()
    assert operation.args == {"text": "hold the river"}

    spend = PolicySpendV2.create(
        policy_id=state.policy_id,
        agent_id=state.agent_id,
        player_id=state.player_id,
        turn=state.turn,
        attempt=2,
    )
    assert PolicySpendV2.from_doc(spend.to_doc()) == spend

    noncanonical = state.to_doc()
    noncanonical["runtime_state_json"] = '{"z":0, "a":1}'
    noncanonical["state_id"] = "0" * 64
    with pytest.raises(ContractError, match="canonical JSON"):
        PolicyStateV2.from_doc(noncanonical)

    hidden = state.to_doc()
    hidden["private_state"] = {"opponent_rng": 7}
    with pytest.raises(ContractError, match=r"rejected at \$"):
        PolicyStateV2.from_doc(hidden)

    with pytest.raises(ContractError, match="prohibited field 'private_state'"):
        PolicyStateV2.create(
            policy_id="a" * 64,
            agent_id="agent-0",
            player_id=0,
            turn=3,
            proposal_id="b" * 64,
            runtime_kind=PolicyRuntimeKindV2.SCRIPTED,
            rng_state=None,
            runtime_state={"nested": {"private_state": {"rival_rng": 7}}},
            operations=[],
            telemetry={},
            model_posts=0,
        )

    with pytest.raises(ContractError, match="prohibited field 'headers'"):
        PolicyOperationV2.create(
            agent_id="agent-0",
            player_id=0,
            turn=3,
            sequence=5,
            tool="record_lesson",
            args={"transport": {"headers": {"Authorization": "secret"}}},
            result={"status": "accepted"},
        )

    with pytest.raises(ContractError, match="prohibited field 'apiKey'"):
        PolicyStateV2.create(
            policy_id="a" * 64,
            agent_id="agent-0",
            player_id=0,
            turn=3,
            proposal_id=None,
            runtime_kind=PolicyRuntimeKindV2.SCRIPTED,
            rng_state=None,
            runtime_state={},
            operations=[],
            telemetry={"nested": {"apiKey": "secret"}},
            model_posts=0,
        )

    with pytest.raises(ContractError, match="tool is not registered"):
        PolicyOperationV2.create(
            agent_id="agent-0",
            player_id=0,
            turn=3,
            sequence=5,
            tool="adapter",
            args={},
            result={"status": "accepted"},
        )

    with pytest.raises(ContractError, match="not accepted by the runtime"):
        RandomStateV2(version=3, state=(1,), gauss_next=None)


def test_episode_config_is_strict_hash_bound_and_ordered() -> None:
    first = ChaosEventConfigV2(
        ChaosSpecV2.STEAL_GOLD,
        ChaosHookV2.ACT,
        1,
    )
    second = ChaosEventConfigV2(
        ChaosSpecV2.CHANGE_RESEARCH,
        ChaosHookV2.BEGIN_PHASE,
        0,
    )
    config = EpisodeConfigV2.create(
        watchdog_mode=WatchdogModeV2.FLAG_AND_CONTINUE,
        violation_limit=5,
        chaos=[first, second],
    )
    assert EpisodeConfigV2.from_doc(config.to_doc()) == config
    assert config.chaos == (first, second)

    reordered = EpisodeConfigV2.create(
        watchdog_mode=WatchdogModeV2.FLAG_AND_CONTINUE,
        violation_limit=5,
        chaos=[second, first],
    )
    assert reordered.config_id != config.config_id

    tampered = config.to_doc()
    tampered["violation_limit"] = 6
    with pytest.raises(ContractError, match="config_id"):
        EpisodeConfigV2.from_doc(tampered)

    unknown = first.to_doc()
    unknown["private_state"] = {"rival_rng": 7}
    with pytest.raises(ContractError, match=r"rejected at \$"):
        ChaosEventConfigV2.from_doc(unknown)

@settings(derandomize=True, database=None)
@given(st.integers(min_value=-10_000, max_value=10_000))
def test_observation_distinguishes_unknown_from_zero(value: int) -> None:
    known = KnowledgeValueV2.known(value, observed_turn=0)
    unknown = KnowledgeValueV2.unknown("not supplied")
    not_seen = KnowledgeValueV2.not_observed("under fog")
    not_applicable = KnowledgeValueV2.not_applicable("no such property")
    assert known.to_doc()["value"] == value
    assert known.to_doc()["state"] == "known"
    assert "value" not in unknown.to_doc()
    assert {unknown.state.value, not_seen.state.value, not_applicable.state.value} == {
        "unknown",
        "not_observed",
        "not_applicable",
    }


def test_observation_rejects_privileged_fields() -> None:
    doc = _observation().to_doc()
    doc["rng_state"] = [1, 2, 3]
    with pytest.raises(ContractError, match=r"V2 document rejected at \$"):
        ObservationV2.from_doc(doc)

    fact = copy.deepcopy(_observation().to_doc())
    fact["facts"][0]["predicate"] = "opponent_intent_flags"
    with pytest.raises(ContractError, match=r"facts\[0\].predicate"):
        ObservationV2.from_doc(fact)

    foreign = copy.deepcopy(_observation().to_doc())
    foreign["facts"][0]["subject_scope"] = "visible_foreign"
    foreign["facts"][0]["subject"]["entity_id"] = "p1"
    foreign["facts"][0]["predicate"] = "movement"
    with pytest.raises(ContractError, match=r"facts\[0\]"):
        ObservationV2.from_doc(foreign)


@pytest.mark.parametrize("bad_gold", [True, "100", [100]])
def test_observation_fact_predicates_enforce_machine_types(bad_gold: object) -> None:
    doc = _observation(gold=100).to_doc()
    doc["facts"][0]["value"]["value"] = bad_gold
    with pytest.raises(ContractError, match=r"facts\[0\].value"):
        ObservationV2.from_doc(doc)


def test_observation_coordinate_requires_canonical_spelling() -> None:
    tile = EntityRefV2(EntityTypeV2.TILE, "tile:1,2")
    with pytest.raises(ContractError, match=r"value"):
        ObservableFactV2(
            subject=tile,
            subject_scope=FactSubjectScopeV2.PUBLIC,
            predicate="coord",
            value=KnowledgeValueV2.known("01,2", observed_turn=0),
            source=FactSourceV2.DIRECT,
        )


def test_unknown_schema_version_fails_closed() -> None:
    doc = _observation().to_doc()
    doc["schema"] = 3
    with pytest.raises(ContractError, match=r"schema"):
        ObservationV2.from_doc(doc)


def test_action_id_is_deterministic_for_canonical_input() -> None:
    observation = _observation()
    left = _action(observation)
    right = _action(observation)
    assert left == right
    assert left.action_id == right.action_id
    assert LegalActionV2.from_doc(json.loads(json.dumps(left.to_doc()))) == left


def test_action_precondition_binds_to_observation_hash() -> None:
    before = _observation(seq=0)
    after = _observation(seq=1)
    assert before.observation_id != after.observation_id
    assert _action(before).action_id != _action(after).action_id
    assert _action(before).observation_id == before.observation_id


def test_receipt_rejects_unregistered_tool_action() -> None:
    intent = ActionIntentV2.create(
        ActionKindV2.SET_RESEARCH,
        PLAYER,
        parameters={"tech_id": "MINING"},
        proposal_index=0,
    ).to_doc()
    intent["action_kind"] = "raw_mcp_call"
    with pytest.raises(ContractError, match="action_kind"):
        ActionIntentV2.from_doc(intent)


def test_graph_rejects_unknown_edge_kind() -> None:
    observation = _observation()
    left, right = _action(observation, "MINING"), _action(observation, "POTTERY")
    edge = ActionGraphEdgeV2.create(
        left.action_id,
        right.action_id,
        EdgeKindV2.MUTEX,
        EdgeAuthorityV2.AUTHORITATIVE,
        EdgeReasonV2.EXCLUSIVE_CHOICE,
        region="player:p0:research",
    ).to_doc()
    edge["kind"] = "MAYBE_BEFORE"
    with pytest.raises(ContractError, match="kind"):
        ActionGraphEdgeV2.from_doc(edge)


def test_commutes_cannot_override_conflict() -> None:
    observation = _observation()
    left, right = _action(observation, "MINING"), _action(observation, "POTTERY")
    conflict = ActionGraphEdgeV2.create(
        left.action_id,
        right.action_id,
        EdgeKindV2.MUTEX,
        EdgeAuthorityV2.AUTHORITATIVE,
        EdgeReasonV2.EXCLUSIVE_CHOICE,
    )
    commute = ActionGraphEdgeV2.create(
        left.action_id,
        right.action_id,
        EdgeKindV2.COMMUTES_WITH,
        EdgeAuthorityV2.DERIVED,
        EdgeReasonV2.PROVEN_DISJOINT,
    )
    actions = LegalActionSetV2.create(observation.observation_id, [left, right])
    with pytest.raises(ContractError, match="COMMUTES_WITH cannot override"):
        ActionGraphV2.create(
            observation_id=observation.observation_id,
            legal_action_set_id=actions.legal_action_set_id,
            nodes=[ActionGraphNodeV2.from_action(left), ActionGraphNodeV2.from_action(right)],
            edges=[conflict, commute],
            metrics=_metrics([conflict, commute], 2),
        )


def test_cycle_is_rejected_with_deterministic_witness() -> None:
    observation = _observation()
    left, right = _action(observation, "MINING"), _action(observation, "POTTERY")
    forward = ActionGraphEdgeV2.create(
        left.action_id,
        right.action_id,
        EdgeKindV2.MUST_PRECEDE,
        EdgeAuthorityV2.AUTHORITATIVE,
        EdgeReasonV2.SAME_ACTOR_SEQUENCE,
    )
    backward = ActionGraphEdgeV2.create(
        right.action_id,
        left.action_id,
        EdgeKindV2.REQUIRES,
        EdgeAuthorityV2.AUTHORITATIVE,
        EdgeReasonV2.PRECONDITION_DEPENDENCY,
    )
    actions = LegalActionSetV2.create(observation.observation_id, [left, right])
    with pytest.raises(ContractError, match=r"directed action-graph cycle: .* -> .* ->"):
        ActionGraphV2.create(
            observation_id=observation.observation_id,
            legal_action_set_id=actions.legal_action_set_id,
            nodes=[ActionGraphNodeV2.from_action(left), ActionGraphNodeV2.from_action(right)],
            edges=[forward, backward],
            metrics=_metrics([forward, backward], 2),
        )


def test_turn_receipt_links_pre_and_post_state_hashes() -> None:
    before, after = _observation(gold=10, seq=0), _observation(gold=0, seq=1)
    action = _action(before)
    authorization = AuthorizationV2.create(
        decision=AuthorizationDecisionV2.AUTHORIZED,
        intent_id="b" * 64,
        action_id=action.action_id,
        observation_id=before.observation_id,
        graph_id="c" * 64,
        reason_code=AuthorizationReasonV2.AUTHORIZED,
    )
    result = ActionResultV2.create(
        status=ActionStatusV2.ACCEPTED,
        action_id=action.action_id,
        authorization_id=authorization.authorization_id,
        pre_observation_id=before.observation_id,
        post_observation_id=after.observation_id,
        verification=VerificationStatusV2.MATCHED,
    )
    receipt = TurnReceiptV2.create(
        episode_id="episode-1",
        turn=3,
        player_id=0,
        pre_observation_id=before.observation_id,
        post_observation_id=after.observation_id,
        proposal_id="d" * 64,
        graph_ids=["c" * 64],
        authorizations=[authorization],
        results=[result],
        replan_count=1,
        termination=TurnTerminationV2.COMPLETED,
    )
    assert receipt.pre_observation_id == result.pre_observation_id
    assert receipt.post_observation_id == result.post_observation_id


def test_system_turn_contracts_are_strict_roundtrippable_and_separated() -> None:
    blockers = ObservableFactV2(
        subject=PLAYER,
        subject_scope=FactSubjectScopeV2.SELF,
        predicate="turn_blockers",
        value=KnowledgeValueV2.known(
            ("civic_choice", "policy_slots"), observed_turn=3
        ),
        source=FactSourceV2.DIRECT,
    )
    observation = ObservationV2.create(
        environment_id="firetuner-v2",
        game_version="civ6-fixture-1",
        ruleset_digest=RULESET,
        turn=3,
        active_player=PLAYER,
        observing_player=PLAYER,
        phase=ObservationPhaseV2.TURN,
        observed_at_seq=0,
        facts=[blockers],
        mandatory_action_kinds=SYSTEM_ACTION_KINDS_V2,
    )
    assert ObservationV2.from_doc(observation.to_doc()) == observation
    for index, kind in enumerate(SYSTEM_ACTION_KINDS_V2):
        intent = ActionIntentV2.create(kind, PLAYER, proposal_index=index)
        assert ActionIntentV2.from_doc(intent.to_doc()) == intent

    system = PolicyDescriptorV2.create(
        policy_kind=PolicyKindV2.SYSTEM,
        policy_version="system-fixture-v2",
        registered_action_kinds=SYSTEM_ACTION_KINDS_V2,
    )
    assert PolicyDescriptorV2.from_doc(system.to_doc()) == system
    with pytest.raises(ContractError, match="player policies cannot register"):
        PolicyDescriptorV2.create(
            policy_kind=PolicyKindV2.SCRIPTED,
            policy_version="forged-player-v2",
            registered_action_kinds=[ActionKindV2.RESOLVE_CIVIC],
        )
    with pytest.raises(ContractError, match="only system action kinds"):
        PolicyDescriptorV2.create(
            policy_kind=PolicyKindV2.SYSTEM,
            policy_version="forged-system-v2",
            registered_action_kinds=[ActionKindV2.END_TURN],
        )

    action = LegalActionV2.create(
        ActionKindV2.RESOLVE_CIVIC,
        PLAYER,
        observation.observation_id,
        mandatory=True,
    )
    authorization = AuthorizationV2.create(
        decision=AuthorizationDecisionV2.AUTHORIZED,
        intent_id="b" * 64,
        action_id=action.action_id,
        observation_id=observation.observation_id,
        graph_id="c" * 64,
        reason_code=AuthorizationReasonV2.AUTHORIZED,
    )
    result = ActionResultV2.create(
        status=ActionStatusV2.ACCEPTED,
        action_id=action.action_id,
        authorization_id=authorization.authorization_id,
        pre_observation_id=observation.observation_id,
        post_observation_id=observation.observation_id,
        verification=VerificationStatusV2.NOT_VERIFIABLE,
    )
    receipt = TurnReceiptV2.create(
        episode_id="system-episode",
        turn=3,
        player_id=0,
        pre_observation_id=observation.observation_id,
        post_observation_id=observation.observation_id,
        proposal_id="d" * 64,
        graph_ids=["c" * 64],
        authorizations=[authorization],
        results=[result],
        replan_count=0,
        termination=TurnTerminationV2.SYSTEM_HANDOFF,
    )
    assert TurnReceiptV2.from_doc(receipt.to_doc()) == receipt


def test_receipt_semantic_hash_is_canonical() -> None:
    before = _observation()
    action = _action(before)
    refused = AuthorizationV2.create(
        decision=AuthorizationDecisionV2.REFUSED,
        intent_id="b" * 64,
        action_id=None,
        observation_id=before.observation_id,
        graph_id="c" * 64,
        reason_code=AuthorizationReasonV2.ACTION_ABSENT,
    )
    receipt = TurnReceiptV2.create(
        episode_id="episode-1",
        turn=3,
        player_id=0,
        pre_observation_id=before.observation_id,
        post_observation_id=before.observation_id,
        proposal_id="d" * 64,
        graph_ids=["c" * 64],
        authorizations=[refused],
        results=[],
        replan_count=0,
        termination=TurnTerminationV2.FAILED,
        safe_error="proposal refused",
    )
    reordered = json.loads(json.dumps(receipt.to_doc(), sort_keys=False))
    assert TurnReceiptV2.from_doc(reordered).receipt_id == receipt.receipt_id
    assert action.action_id  # keep the fixture's registered action construction live


def _environment() -> EnvironmentDescriptorV2:
    return EnvironmentDescriptorV2.create(
        adapter_kind=AdapterKindV2.FAKE,
        adapter_version="2.0",
        game_version="fake-1",
        ruleset_digest=RULESET,
        mod_digest="e" * 64,
        capabilities=[
            EnvironmentCapabilityV2.ACTION_EXECUTION,
            EnvironmentCapabilityV2.DETERMINISTIC_FAKE_REPLAY,
            EnvironmentCapabilityV2.PLAYER_OBSERVATION,
            EnvironmentCapabilityV2.PRIVATE_REFEREE_MONITOR,
        ],
        deterministic=True,
    )


def _policy() -> PolicyDescriptorV2:
    return PolicyDescriptorV2.create(
        policy_kind=PolicyKindV2.SCRIPTED,
        policy_version="canonical-first-v2",
        registered_action_kinds=PLAYER_ACTION_KINDS_V2,
    )


def test_scored_episode_cannot_resume_or_use_control_treatment() -> None:
    with pytest.raises(ContractError, match="scored V2 episodes cannot resume"):
        EpisodeReceiptV2.create(
            episode_id="child-1",
            parent_episode_id="parent-1",
            parent_terminal_event_hash="f" * 64,
            environment=_environment(),
            policies=[_policy()],
            seed=4,
            compute=ComputeConfigV2(ExecutionModeV2.DAG_TX, 1024, 2),
            scored=True,
            termination_reason=EpisodeTerminationV2.FAILURE,
            turns_completed=0,
        )

    with pytest.raises(ContractError, match="require dag_tx"):
        EpisodeReceiptV2.create(
            episode_id="scored-control",
            environment=_environment(),
            policies=[_policy()],
            seed=4,
            compute=ComputeConfigV2(ExecutionModeV2.SEQUENTIAL, 1024, 2),
            scored=True,
            termination_reason=EpisodeTerminationV2.FAILURE,
            turns_completed=0,
        )


def test_terminal_event_embeds_and_binds_episode_receipt() -> None:
    receipt = EpisodeReceiptV2.create(
        episode_id="episode-1",
        environment=_environment(),
        policies=[_policy()],
        seed=4,
        compute=ComputeConfigV2(ExecutionModeV2.DAG_TX, 1024, 2),
        scored=False,
        termination_reason=EpisodeTerminationV2.SUCCESS,
        turns_completed=3,
    )
    event = EventV2.create(
        event_type=EventTypeV2.EPISODE_TERMINATED,
        schema_ref=EpisodeReceiptV2.SCHEMA_REF,
        episode_id="episode-1",
        turn_id=None,
        correlation_id="episode-1",
        causation_id=None,
        sequence_number=9,
        occurred_at=datetime.datetime(2026, 9, 2, tzinfo=datetime.UTC).isoformat(),
        payload_value=receipt,
        previous_event_hash="1" * 64,
    )
    assert isinstance(event.payload_value, EpisodeReceiptV2)
    assert event.payload_value.terminal_event_hash == event.event_hash
    assert EventV2.from_doc(event.to_doc()) == event


def test_event_timestamp_is_nonsemantic_but_chain_bound() -> None:
    artifact_doc = {
        "schema": 2,
        "digest": "2" * 64,
        "schema_ref": ObservationV2.SCHEMA_REF,
        "byte_length": 7,
        "media_type": "application/json",
    }
    artifact = ArtifactRefV2.from_doc(artifact_doc)
    kwargs = {
        "event_type": EventTypeV2.OBSERVATION_RECORDED,
        "schema_ref": ObservationV2.SCHEMA_REF,
        "episode_id": "episode-1",
        "turn_id": 3,
        "correlation_id": "turn-3",
        "causation_id": None,
        "sequence_number": 0,
        "payload_value": artifact,
        "previous_event_hash": ZERO_DIGEST,
    }
    first = EventV2.create(
        occurred_at="2026-09-02T12:00:00+00:00",
        **kwargs,
    )
    second = EventV2.create(
        occurred_at="2026-09-02T12:00:01+00:00",
        **kwargs,
    )
    assert first.semantic_hash == second.semantic_hash
    assert first.event_hash != second.event_hash
