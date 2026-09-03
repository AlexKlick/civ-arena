from __future__ import annotations

import json
from pathlib import Path

import pytest

from civ_arena.canonical import canonical
from civ_arena.v2 import (
    ActionKindV2,
    AdapterKindV2,
    ArtifactRefV2,
    ComputeConfigV2,
    ContractError,
    EntityRefV2,
    EntityTypeV2,
    EnvironmentCapabilityV2,
    EnvironmentDescriptorV2,
    EpisodeReceiptV2,
    EpisodeTerminationV2,
    EventTypeV2,
    ExecutionModeV2,
    FactSourceV2,
    FactSubjectScopeV2,
    KnowledgeValueV2,
    ObservableFactV2,
    ObservationPhaseV2,
    ObservationV2,
    PolicyDescriptorV2,
    PolicyKindV2,
    ValidationCommandV2,
    ValidationReceiptV2,
)
from civ_arena.v2.ledger import (
    ConcurrentWriterError,
    EpisodeRecorderV2,
    EventLedgerV2,
    LedgerIntegrityError,
    ObjectStoreV2,
    load_events_v2,
    redact_document,
    verify_ledger_v2,
)

RULESET = "a" * 64
FIXED_TIME = "2026-09-02T18:00:00+00:00"


def _clock() -> str:
    return FIXED_TIME


def _environment() -> EnvironmentDescriptorV2:
    return EnvironmentDescriptorV2.create(
        adapter_kind=AdapterKindV2.FAKE,
        adapter_version="2.0",
        game_version="fake-1",
        ruleset_digest=RULESET,
        mod_digest="b" * 64,
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
        policy_version="fixture-v2",
        registered_action_kinds=list(ActionKindV2),
    )


def _observation(seq: int = 0) -> ObservationV2:
    player = EntityRefV2(EntityTypeV2.PLAYER, "p0")
    fact = ObservableFactV2(
        subject=player,
        subject_scope=FactSubjectScopeV2.SELF,
        predicate="gold",
        value=KnowledgeValueV2.known(20, observed_turn=0),
        source=FactSourceV2.DIRECT,
    )
    return ObservationV2.create(
        environment_id="fake-v2",
        game_version="fake-1",
        ruleset_digest=RULESET,
        turn=0,
        active_player=player,
        observing_player=player,
        phase=ObservationPhaseV2.TURN,
        observed_at_seq=seq,
        facts=[fact],
    )


def _recorder(path: Path) -> EpisodeRecorderV2:
    return EpisodeRecorderV2(
        path,
        episode_id="episode-1",
        environment=_environment(),
        policies=[_policy()],
        seed=41,
        compute=ComputeConfigV2(ExecutionModeV2.DAG_TX, 1024, 2),
        scored=False,
        clock=_clock,
    )


def _complete_episode(path: Path) -> None:
    recorder = _recorder(path)
    recorder.record_reference(
        event_type=EventTypeV2.OBSERVATION_RECORDED,
        model=_observation(),
        turn_id=0,
        correlation_id="turn-0",
    )
    recorder.terminate(EpisodeTerminationV2.SUCCESS, turns_completed=1)
    recorder.close()


def _rewrite_records(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(canonical(record) + "\n" for record in records),
        encoding="utf-8",
    )


def test_object_store_is_content_addressed_and_canonical(tmp_path: Path) -> None:
    store = ObjectStoreV2(tmp_path)
    observation = _observation()
    first = store.put_model(observation)
    second = store.put_doc(
        json.loads(json.dumps(observation.to_doc(), sort_keys=False)),
        ObservationV2.SCHEMA_REF,
    )
    assert first == second
    path = store.path_for_digest(first.digest)
    assert path.relative_to(tmp_path).parts[:3] == (
        "objects",
        "sha256",
        first.digest[:2],
    )
    assert store.read_doc(first) == observation.to_doc()
    assert path.read_bytes() == canonical(observation.to_doc()).encode()


def test_event_chain_detects_payload_mutation(tmp_path: Path) -> None:
    _complete_episode(tmp_path)
    path = tmp_path / "events.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[1]["payload"]["artifact"]["byte_length"] += 1
    _rewrite_records(path, records)
    with pytest.raises(LedgerIntegrityError, match="event semantic identity mismatch"):
        load_events_v2(path)


@pytest.mark.parametrize("mode", ["delete", "reorder"])
def test_event_chain_detects_deletion_or_reordering(tmp_path: Path, mode: str) -> None:
    _complete_episode(tmp_path)
    path = tmp_path / "events.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    if mode == "delete":
        records.pop(1)
    else:
        records[0], records[1] = records[1], records[0]
    _rewrite_records(path, records)
    with pytest.raises(LedgerIntegrityError, match="sequence mismatch|chain break"):
        load_events_v2(path)


def test_terminal_event_deletion_is_not_a_valid_episode(tmp_path: Path) -> None:
    _complete_episode(tmp_path)
    path = tmp_path / "events.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    _rewrite_records(path, records[:-1])
    with pytest.raises(LedgerIntegrityError, match="no terminal receipt"):
        verify_ledger_v2(tmp_path)


def test_failed_episode_emits_terminal_receipt(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="fixture failure"), _recorder(tmp_path):
        raise RuntimeError("fixture failure")
    verification = verify_ledger_v2(tmp_path)
    assert verification.termination_reason == "failure"
    events = load_events_v2(tmp_path / "events.jsonl")
    assert events[-1].event_type.value == "EpisodeTerminated"
    assert events[-1].payload_value.terminal_event_hash == events[-1].event_hash


def test_concurrent_episode_writers_do_not_interleave_events(tmp_path: Path) -> None:
    first = EventLedgerV2(tmp_path, "episode-1", clock=_clock)
    try:
        with pytest.raises(ConcurrentWriterError, match="active writer"):
            EventLedgerV2(tmp_path, "episode-1", clock=_clock)
    finally:
        first.close()


def test_nonterminal_episode_cannot_resume_in_place(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    recorder.record_reference(
        EventTypeV2.OBSERVATION_RECORDED,
        _observation(),
        turn_id=0,
        correlation_id="turn-0",
    )
    recorder.close()
    before = (tmp_path / "events.jsonl").read_bytes()
    with pytest.raises(LedgerIntegrityError, match="create a child episode"):
        EventLedgerV2(tmp_path, "episode-1", clock=_clock)
    assert (tmp_path / "events.jsonl").read_bytes() == before


def test_verifier_refuses_multiple_episode_starts(tmp_path: Path) -> None:
    ledger = EventLedgerV2(tmp_path, "episode-1", clock=_clock)
    reference = ledger.write_artifact(_environment())
    for _ in range(2):
        ledger.append(
            EventTypeV2.EPISODE_STARTED,
            schema_ref=reference.schema_ref,
            payload_value=reference,
            turn_id=None,
            correlation_id="episode-1",
        )
    ledger.close()
    with pytest.raises(LedgerIntegrityError, match="multiple EpisodeStarted"):
        verify_ledger_v2(tmp_path, require_terminal=False)


def test_verifier_refuses_conflicting_refs_for_one_digest(tmp_path: Path) -> None:
    ledger = EventLedgerV2(tmp_path, "episode-1", clock=_clock)
    reference = ledger.write_artifact(_environment())
    ledger.append(
        EventTypeV2.EPISODE_STARTED,
        schema_ref=reference.schema_ref,
        payload_value=reference,
        turn_id=None,
        correlation_id="episode-1",
    )
    conflicting = ArtifactRefV2(
        digest=reference.digest,
        schema_ref=reference.schema_ref,
        byte_length=reference.byte_length + 1,
    )
    ledger.append(
        EventTypeV2.VALIDATION_RECORDED,
        schema_ref=conflicting.schema_ref,
        payload_value=conflicting,
        turn_id=0,
        correlation_id="episode-1",
    )
    ledger.close()
    with pytest.raises(LedgerIntegrityError, match="conflicting references"):
        verify_ledger_v2(tmp_path, require_terminal=False)


def test_non_scored_resume_is_a_child_bound_to_parent_terminal_hash(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    child = tmp_path / "child"
    _complete_episode(parent)
    parent_bytes = (parent / "events.jsonl").read_bytes()
    parent_proof = verify_ledger_v2(parent)
    recorder = EpisodeRecorderV2(
        child,
        episode_id="episode-child",
        environment=_environment(),
        policies=[_policy()],
        seed=41,
        compute=ComputeConfigV2(ExecutionModeV2.DAG_TX, 1024, 2),
        scored=False,
        parent_episode_id="episode-1",
        parent_terminal_event_hash=parent_proof.terminal_event_hash,
        clock=_clock,
    )
    receipt = recorder.terminate(EpisodeTerminationV2.RECOVERED_CRASH, turns_completed=0)
    recorder.close()
    assert receipt.parent_episode_id == "episode-1"
    assert receipt.parent_terminal_event_hash == parent_proof.terminal_event_hash
    assert (parent / "events.jsonl").read_bytes() == parent_bytes


def test_torn_tail_is_detected_without_rewriting_original(tmp_path: Path) -> None:
    _complete_episode(tmp_path)
    path = tmp_path / "events.jsonl"
    original = path.read_bytes()
    path.write_bytes(original + b'{"schema":2')
    torn = path.read_bytes()
    with pytest.raises(LedgerIntegrityError, match="torn V2 event-log tail"):
        load_events_v2(path)
    assert path.read_bytes() == torn


def test_payload_artifact_tampering_and_deletion_are_detected(tmp_path: Path) -> None:
    _complete_episode(tmp_path)
    events = load_events_v2(tmp_path / "events.jsonl")
    observation_ref = events[1].payload_value
    store = ObjectStoreV2(tmp_path)
    object_path = store.path_for_digest(observation_ref.digest)
    original = object_path.read_bytes()
    object_path.write_bytes(original + b" ")
    with pytest.raises(LedgerIntegrityError, match="length mismatch"):
        verify_ledger_v2(tmp_path)
    object_path.write_bytes(original)
    object_path.unlink()
    with pytest.raises(LedgerIntegrityError, match="artifact missing"):
        verify_ledger_v2(tmp_path)


def test_terminal_manifest_must_cover_every_referenced_artifact(tmp_path: Path) -> None:
    environment = _environment()
    policy = _policy()
    ledger = EventLedgerV2(tmp_path, "episode-1", clock=_clock)
    env_ref = ledger.write_artifact(environment)
    ledger.append(
        EventTypeV2.EPISODE_STARTED,
        schema_ref=environment.SCHEMA_REF,
        payload_value=env_ref,
        turn_id=None,
        correlation_id="episode-1",
    )
    obs_ref = ledger.write_artifact(_observation())
    ledger.append(
        EventTypeV2.OBSERVATION_RECORDED,
        schema_ref=ObservationV2.SCHEMA_REF,
        payload_value=obs_ref,
        turn_id=0,
        correlation_id="turn-0",
    )
    receipt = EpisodeReceiptV2.create(
        episode_id="episode-1",
        environment=environment,
        policies=[policy],
        seed=41,
        compute=ComputeConfigV2(ExecutionModeV2.DAG_TX, 1024, 2),
        scored=False,
        termination_reason=EpisodeTerminationV2.SUCCESS,
        turns_completed=1,
        artifacts=[env_ref],
    )
    ledger.append(
        EventTypeV2.EPISODE_TERMINATED,
        schema_ref=receipt.SCHEMA_REF,
        payload_value=receipt,
        turn_id=None,
        correlation_id="episode-1",
    )
    ledger.close()
    with pytest.raises(LedgerIntegrityError, match="artifact manifest mismatch"):
        verify_ledger_v2(tmp_path)


def test_replay_rejects_environment_digest_mismatch(tmp_path: Path) -> None:
    _complete_episode(tmp_path)
    with pytest.raises(LedgerIntegrityError, match="environment descriptor mismatch"):
        verify_ledger_v2(tmp_path, expected_environment_id="f" * 64)


def test_v1_event_log_cannot_open_as_v2_writer(tmp_path: Path) -> None:
    (tmp_path / "events.jsonl").write_text(
        '{"schema":1,"seq":0,"kind":"MATCH_START"}\n',
        encoding="utf-8",
    )
    with pytest.raises(LedgerIntegrityError, match="not schema 2"):
        EventLedgerV2(tmp_path, "episode-1")


def test_receipt_redacts_credentials_and_auth_headers() -> None:
    raw = {
        "Authorization": "Bearer top-secret",
        "nested": {
            "api-key": "provider-secret",
            "max_tokens": 1024,
            "ordinary": "kept",
        },
        "headers": {"x-request-id": "host-id"},
    }
    redacted = redact_document(raw)
    assert redacted == {
        "Authorization": "[REDACTED]",
        "nested": {
            "api-key": "[REDACTED]",
            "max_tokens": 1024,
            "ordinary": "kept",
        },
        "headers": "[REDACTED]",
    }
    assert "top-secret" not in canonical(redacted)
    assert "provider-secret" not in canonical(redacted)


def test_validation_receipt_binds_commit_tree_and_test_commands() -> None:
    command = ValidationCommandV2(
        command=".venv/bin/python -m pytest -q",
        exit_code=0,
        log_digest="1" * 64,
        passed=536,
        failed=0,
        skipped=1,
    )
    receipt = ValidationReceiptV2.create(
        commit_sha="2" * 40,
        tree_sha="3" * 40,
        source_diff_digest="4" * 64,
        validator="local-codex",
        commands=[command],
    )
    doc = receipt.to_doc()
    doc["tree_sha"] = "5" * 40
    with pytest.raises(
        ContractError,
        match="receipt_id does not match canonical semantic bytes",
    ):
        ValidationReceiptV2.from_doc(doc)
