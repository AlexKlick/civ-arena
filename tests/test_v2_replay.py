from __future__ import annotations

from pathlib import Path

import pytest

from civ_arena.canonical import canonical
from civ_arena.v1_compat import (
    V1CompatibilityError,
    load_v1_config_read_only,
    load_v1_events_read_only,
)
from civ_arena.v2 import (
    ActionGraphCompilerV2,
    ActionIntentV2,
    ActionKindV2,
    ActionResultV2,
    ActionStatusV2,
    AuthorizationDecisionV2,
    AuthorizationReasonV2,
    AuthorizationV2,
    ComputeConfigV2,
    EpisodeTerminationV2,
    EventTypeV2,
    ExecutionModeV2,
    FactSourceV2,
    LegalActionV2,
    ObservableFactV2,
    ObservationV2,
    PolicyDescriptorV2,
    PolicyKindV2,
    TurnProposalV2,
    TurnReceiptV2,
    TurnTerminationV2,
    VerificationStatusV2,
    replay_fake_episode_v2,
    simulator_facets_v2,
)
from civ_arena.v2.enumeration import ActionEnumeratorV2
from civ_arena.v2.ledger import EpisodeRecorderV2, LedgerIntegrityError, verify_ledger_v2
from civ_arena.v2.replay import ExactReplayError

FIXED_TIME = "2026-09-02T18:00:00+00:00"


def _clock() -> str:
    return FIXED_TIME


def _policy() -> PolicyDescriptorV2:
    return PolicyDescriptorV2.create(
        policy_kind=PolicyKindV2.SCRIPTED,
        policy_version="replay-fixture-v2",
        registered_action_kinds=list(ActionKindV2),
    )


def _intent(action: LegalActionV2, proposal_index: int) -> ActionIntentV2:
    return ActionIntentV2.create(
        action.action_kind,
        action.actor,
        target=action.target,
        parameters=dict(action.parameters),
        proposal_index=proposal_index,
    )


def _authorization(action: LegalActionV2, intent: ActionIntentV2, graph_id: str) -> AuthorizationV2:
    return AuthorizationV2.create(
        decision=AuthorizationDecisionV2.AUTHORIZED,
        intent_id=intent.intent_id,
        action_id=action.action_id,
        observation_id=action.observation_id,
        graph_id=graph_id,
        reason_code=AuthorizationReasonV2.AUTHORIZED,
    )


def _wrong_gold(observation: ObservationV2) -> ObservationV2:
    facts: list[ObservableFactV2] = []
    changed = False
    for fact in observation.facts:
        if fact.predicate == "gold" and fact.subject == observation.observing_player:
            assert type(fact.value.value) is int
            facts.append(
                ObservableFactV2(
                    subject=fact.subject,
                    subject_scope=fact.subject_scope,
                    predicate=fact.predicate,
                    value=type(fact.value).known(
                        fact.value.value + 1,
                        observed_turn=observation.turn,
                    ),
                    source=FactSourceV2.DIRECT,
                )
            )
            changed = True
        else:
            facts.append(fact)
    assert changed
    return ObservationV2.create(
        environment_id=observation.environment_id,
        game_version=observation.game_version,
        ruleset_digest=observation.ruleset_digest,
        turn=observation.turn,
        active_player=observation.active_player,
        observing_player=observation.observing_player,
        phase=observation.phase,
        observed_at_seq=observation.observed_at_seq,
        facts=facts,
        mandatory_action_kinds=observation.mandatory_action_kinds,
    )


async def _record_fake_episode(
    root: Path,
    *,
    wrong_first_post: bool = False,
    omit_first_postcondition: bool = False,
    complete_without_end_turn: bool = False,
) -> str:
    environment, monitor = simulator_facets_v2()
    await environment.reset({"seed": 41})
    policy = _policy()
    recorder = EpisodeRecorderV2(
        root,
        episode_id="episode-replay",
        environment=environment.descriptor,
        policies=[policy],
        seed=41,
        compute=ComputeConfigV2(ExecutionModeV2.DAG_TX, 1024, 2),
        scored=False,
        clock=_clock,
    )
    enumerator = ActionEnumeratorV2()
    compiler = ActionGraphCompilerV2()
    correlation = "turn-1-p0"

    first = await environment.begin_turn(0, 1)
    recorder.record_reference(
        EventTypeV2.OBSERVATION_RECORDED,
        first,
        turn_id=1,
        correlation_id=correlation,
    )
    legal = enumerator.enumerate(first)
    recorder.record_reference(
        EventTypeV2.LEGAL_ACTIONS_RECORDED,
        legal,
        turn_id=1,
        correlation_id=correlation,
    )
    graph = compiler.compile(first, legal)
    recorder.record_reference(
        EventTypeV2.ACTION_GRAPH_COMPILED,
        graph,
        turn_id=1,
        correlation_id=correlation,
    )
    research = next(
        action
        for action in legal.actions
        if action.action_kind is ActionKindV2.SET_RESEARCH
    )
    end_intent = ActionIntentV2.create(
        ActionKindV2.END_TURN,
        first.observing_player,
        proposal_index=1,
    )
    research_intent = _intent(research, 0)
    proposal = TurnProposalV2.create(
        policy_id=policy.descriptor_id,
        observation_id=first.observation_id,
        intents=[research_intent, end_intent],
    )
    recorder.record_reference(
        EventTypeV2.POLICY_PROPOSAL_RECORDED,
        proposal,
        turn_id=1,
        correlation_id=correlation,
    )

    authorizations: list[AuthorizationV2] = []
    results: list[ActionResultV2] = []
    graph_ids = [graph.graph_id]
    research_auth = _authorization(research, research_intent, graph.graph_id)
    authorizations.append(research_auth)
    recorder.ledger.append(
        EventTypeV2.ACTION_AUTHORIZED,
        schema_ref=AuthorizationV2.SCHEMA_REF,
        payload_value=research_auth,
        turn_id=1,
        correlation_id=correlation,
    )
    recorder.ledger.append(
        EventTypeV2.ACTION_EXECUTION_STARTED,
        schema_ref=LegalActionV2.SCHEMA_REF,
        payload_value=research.action_id,
        turn_id=1,
        correlation_id=correlation,
    )
    research_execution = await environment.execute_authorized(research_auth, research)
    assert research_execution.status is ActionStatusV2.ACCEPTED
    second = await environment.observe(0)
    recorder.record_reference(
        EventTypeV2.OBSERVATION_RECORDED,
        _wrong_gold(second) if wrong_first_post else second,
        turn_id=1,
        correlation_id=correlation,
    )
    research_result = ActionResultV2.create(
        status=ActionStatusV2.ACCEPTED,
        action_id=research.action_id,
        authorization_id=research_auth.authorization_id,
        pre_observation_id=first.observation_id,
        post_observation_id=second.observation_id,
        verification=VerificationStatusV2.MATCHED,
        observable_effects=research.expected_effects,
    )
    results.append(research_result)
    first_result_events = [EventTypeV2.ACTION_EXECUTION_COMPLETED]
    if not omit_first_postcondition:
        first_result_events.append(EventTypeV2.POSTCONDITION_VERIFIED)
    for event_type in first_result_events:
        recorder.ledger.append(
            event_type,
            schema_ref=ActionResultV2.SCHEMA_REF,
            payload_value=research_result,
            turn_id=1,
            correlation_id=correlation,
        )
    recorder.ledger.append(
        EventTypeV2.GRAPH_REGION_INVALIDATED,
        schema_ref=graph.SCHEMA_REF,
        payload_value=research.action_id,
        turn_id=1,
        correlation_id=correlation,
    )

    second_legal = enumerator.enumerate(second)
    recorder.record_reference(
        EventTypeV2.LEGAL_ACTIONS_RECORDED,
        second_legal,
        turn_id=1,
        correlation_id=correlation,
    )
    second_graph = compiler.compile(second, second_legal)
    graph_ids.append(second_graph.graph_id)
    recorder.record_reference(
        EventTypeV2.ACTION_GRAPH_COMPILED,
        second_graph,
        turn_id=1,
        correlation_id=correlation,
    )
    if complete_without_end_turn:
        turn_receipt = TurnReceiptV2.create(
            episode_id="episode-replay",
            turn=1,
            player_id=0,
            pre_observation_id=first.observation_id,
            post_observation_id=second.observation_id,
            proposal_id=proposal.proposal_id,
            graph_ids=graph_ids,
            authorizations=authorizations,
            results=results,
            replan_count=0,
            termination=TurnTerminationV2.COMPLETED,
        )
        recorder.ledger.append(
            EventTypeV2.TURN_COMPLETED,
            schema_ref=TurnReceiptV2.SCHEMA_REF,
            payload_value=turn_receipt,
            turn_id=1,
            correlation_id=correlation,
        )
        recorder.terminate(EpisodeTerminationV2.SUCCESS, turns_completed=1)
        recorder.close()
        return monitor.state_hash()

    end_turn = next(
        action
        for action in second_legal.actions
        if action.action_kind is ActionKindV2.END_TURN
    )
    end_auth = _authorization(end_turn, end_intent, second_graph.graph_id)
    authorizations.append(end_auth)
    recorder.ledger.append(
        EventTypeV2.ACTION_AUTHORIZED,
        schema_ref=AuthorizationV2.SCHEMA_REF,
        payload_value=end_auth,
        turn_id=1,
        correlation_id=correlation,
    )
    recorder.ledger.append(
        EventTypeV2.ACTION_EXECUTION_STARTED,
        schema_ref=LegalActionV2.SCHEMA_REF,
        payload_value=end_turn.action_id,
        turn_id=1,
        correlation_id=correlation,
    )
    end_execution = await environment.execute_authorized(end_auth, end_turn)
    assert end_execution.status is ActionStatusV2.ACCEPTED
    final_observation = await environment.observe(0)
    recorder.record_reference(
        EventTypeV2.OBSERVATION_RECORDED,
        final_observation,
        turn_id=1,
        correlation_id=correlation,
    )
    end_result = ActionResultV2.create(
        status=ActionStatusV2.ACCEPTED,
        action_id=end_turn.action_id,
        authorization_id=end_auth.authorization_id,
        pre_observation_id=second.observation_id,
        post_observation_id=final_observation.observation_id,
        verification=VerificationStatusV2.MATCHED,
        observable_effects=end_turn.expected_effects,
    )
    results.append(end_result)
    for event_type in (
        EventTypeV2.ACTION_EXECUTION_COMPLETED,
        EventTypeV2.POSTCONDITION_VERIFIED,
    ):
        recorder.ledger.append(
            event_type,
            schema_ref=ActionResultV2.SCHEMA_REF,
            payload_value=end_result,
            turn_id=1,
            correlation_id=correlation,
        )
    recorder.ledger.append(
        EventTypeV2.GRAPH_REGION_INVALIDATED,
        schema_ref=second_graph.SCHEMA_REF,
        payload_value=end_turn.action_id,
        turn_id=1,
        correlation_id=correlation,
    )
    turn_receipt = TurnReceiptV2.create(
        episode_id="episode-replay",
        turn=1,
        player_id=0,
        pre_observation_id=first.observation_id,
        post_observation_id=final_observation.observation_id,
        proposal_id=proposal.proposal_id,
        graph_ids=graph_ids,
        authorizations=authorizations,
        results=results,
        replan_count=0,
        termination=TurnTerminationV2.COMPLETED,
    )
    recorder.ledger.append(
        EventTypeV2.TURN_COMPLETED,
        schema_ref=TurnReceiptV2.SCHEMA_REF,
        payload_value=turn_receipt,
        turn_id=1,
        correlation_id=correlation,
    )
    recorder.terminate(EpisodeTerminationV2.SUCCESS, turns_completed=1)
    recorder.close()
    return monitor.state_hash()


@pytest.mark.asyncio
async def test_exact_v2_fake_replay_checks_every_post_observation_without_writes(
    tmp_path: Path,
) -> None:
    private_hash = await _record_fake_episode(tmp_path)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    result = await replay_fake_episode_v2(tmp_path)
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert result.turn_receipt_count == 1
    assert result.action_count == 2
    assert result.observation_count == 3
    assert result.final_private_state_hash == private_hash
    assert after == before
    persisted = b"".join(before.values())
    assert private_hash.encode() not in persisted
    assert b'"state_hash"' not in persisted


@pytest.mark.asyncio
async def test_exact_v2_fake_replay_detects_forged_post_state_artifact(
    tmp_path: Path,
) -> None:
    await _record_fake_episode(tmp_path, wrong_first_post=True)
    assert verify_ledger_v2(tmp_path).termination_reason == "success"
    with pytest.raises(ExactReplayError, match="post-state observation diverged"):
        await replay_fake_episode_v2(tmp_path)


@pytest.mark.asyncio
async def test_exact_v2_fake_replay_refuses_environment_mismatch(tmp_path: Path) -> None:
    await _record_fake_episode(tmp_path)
    with pytest.raises(LedgerIntegrityError, match="environment descriptor mismatch"):
        await replay_fake_episode_v2(
            tmp_path,
            expected_environment_id="f" * 64,
        )


@pytest.mark.asyncio
async def test_exact_replay_requires_postcondition_and_invalidation_closure(
    tmp_path: Path,
) -> None:
    await _record_fake_episode(tmp_path, omit_first_postcondition=True)
    assert verify_ledger_v2(tmp_path).termination_reason == "success"
    with pytest.raises(ExactReplayError, match="graph invalidation precedes"):
        await replay_fake_episode_v2(tmp_path)


@pytest.mark.asyncio
async def test_exact_replay_refuses_completed_turn_without_end_turn(
    tmp_path: Path,
) -> None:
    await _record_fake_episode(tmp_path, complete_without_end_turn=True)
    assert verify_ledger_v2(tmp_path).termination_reason == "success"
    with pytest.raises(ExactReplayError, match="lacks an accepted terminal end_turn"):
        await replay_fake_episode_v2(tmp_path)


def test_frozen_v1_reader_is_read_only_and_reports_torn_tail(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    complete = (
        b'{"schema":1,"seq":0,"kind":"MATCH_START"}\n'
        b'{"schema":1,"seq":1,"kind":"MATCH_END"}\n'
    )
    path.write_bytes(complete + b'{"schema":1')
    before = path.read_bytes()
    loaded = load_v1_events_read_only(path)
    assert [record["kind"] for record in loaded.records] == ["MATCH_START", "MATCH_END"]
    assert loaded.torn_tail is True
    assert path.read_bytes() == before


def test_frozen_v1_reader_fails_closed_on_midfile_corruption(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(
        b'{"schema":1,"seq":0,"kind":"MATCH_START"}\n'
        b'{broken}\n'
        b'{"schema":1,"seq":1,"kind":"MATCH_END"}\n'
    )
    with pytest.raises(V1CompatibilityError, match="corrupt V1 event-log line 2"):
        load_v1_events_read_only(path)


def test_v1_reader_does_not_accept_v2_as_historical_input(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        canonical({"schema": 2, "seq": 0, "kind": "MATCH_START"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(V1CompatibilityError, match="unsupported schema"):
        load_v1_events_read_only(path)


def test_frozen_v1_config_reader_never_enters_authoritative_v2(tmp_path: Path) -> None:
    path = tmp_path / "legacy.yaml"
    path.write_text(
        "schema: 1\nmatch:\n  match_id: legacy\n  seed: 9\nagents:\n"
        "  - {agent_id: old, player_id: 0, policy: turtler}\n",
        encoding="utf-8",
    )
    before = path.read_bytes()
    spec = load_v1_config_read_only(path)
    assert spec.schema == 1
    assert path.read_bytes() == before

    v2 = tmp_path / "v2.yaml"
    v2.write_text("schema: 2\nmatch: {}\nagents: []\n", encoding="utf-8")
    with pytest.raises(V1CompatibilityError, match="not schema 1"):
        load_v1_config_read_only(v2)
