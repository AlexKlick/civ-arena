"""Content-addressed V2 objects and the hash-chained episode trust root."""

from __future__ import annotations

import datetime
import errno
import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from civ_arena.canonical import canonical
from civ_arena.v2.contracts import (
    ZERO_DIGEST,
    ArtifactRefV2,
    ComputeConfigV2,
    EnvironmentDescriptorV2,
    EpisodeReceiptV2,
    EpisodeTerminationV2,
    EventPayloadValue,
    EventTypeV2,
    EventV2,
    ExecutionModeV2,
    PolicyDescriptorV2,
)
from civ_arena.v2.schemas import ContractError, validate_doc


class LedgerIntegrityError(RuntimeError):
    """Persisted V2 evidence failed structural or cryptographic verification."""


class ConcurrentWriterError(LedgerIntegrityError):
    """Another writer already owns the episode ledger."""


SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "headers",
        "password",
        "proxy_authorization",
        "raw_http_body",
        "secret",
        "set_cookie",
    }
)


def _normalized_key(key: str) -> str:
    return key.lower().replace("-", "_")


def redact_document(value: Any) -> Any:
    """Return a detached document with transport credentials removed."""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                raise ContractError("persisted object keys must be strings")
            if _normalized_key(raw_key) in SENSITIVE_KEYS:
                out[raw_key] = "[REDACTED]"
            else:
                out[raw_key] = redact_document(child)
        return out
    if isinstance(value, list | tuple):
        return [redact_document(child) for child in value]
    return value


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ObjectStoreV2:
    """Atomic canonical JSON objects under objects/sha256/xx/digest.json."""

    def __init__(self, episode_dir: Path | str) -> None:
        self.episode_dir = Path(episode_dir)
        self.root = self.episode_dir / "objects" / "sha256"
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for_digest(self, digest: str) -> Path:
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise LedgerIntegrityError("invalid artifact digest")
        return self.root / digest[:2] / f"{digest}.json"

    def put_doc(self, doc: Mapping[str, Any], schema_ref: str) -> ArtifactRefV2:
        redacted = redact_document(dict(doc))
        validate_doc(schema_ref, redacted)
        raw = canonical(redacted).encode("utf-8")
        digest = _sha256_bytes(raw)
        dest = self.path_for_digest(digest)
        dest.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_name = tempfile.mkstemp(
            dir=dest.parent,
            prefix=f".{digest}.",
            suffix=".tmp",
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp, dest)
                _fsync_directory(dest.parent)
            except FileExistsError:
                if dest.is_symlink() or dest.read_bytes() != raw:
                    raise LedgerIntegrityError(
                        "content-addressed object path exists with different bytes"
                    ) from None
        finally:
            tmp.unlink(missing_ok=True)

        return ArtifactRefV2(
            digest=digest,
            schema_ref=schema_ref,
            byte_length=len(raw),
        )

    def put_model(self, model: Any) -> ArtifactRefV2:
        schema_ref = getattr(model, "SCHEMA_REF", None)
        to_doc = getattr(model, "to_doc", None)
        if not isinstance(schema_ref, str) or not callable(to_doc):
            raise TypeError("V2 object must expose SCHEMA_REF and to_doc()")
        return self.put_doc(to_doc(), schema_ref)

    def read_doc(self, ref: ArtifactRefV2) -> dict[str, Any]:
        path = self.path_for_digest(ref.digest)
        if path.is_symlink() or not path.is_file():
            raise LedgerIntegrityError(f"artifact missing: {ref.digest}")
        raw = path.read_bytes()
        if len(raw) != ref.byte_length:
            raise LedgerIntegrityError(f"artifact length mismatch: {ref.digest}")
        if _sha256_bytes(raw) != ref.digest:
            raise LedgerIntegrityError(f"artifact digest mismatch: {ref.digest}")
        try:
            doc = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LedgerIntegrityError(f"artifact is not canonical JSON: {ref.digest}") from None
        if not isinstance(doc, dict) or canonical(doc).encode("utf-8") != raw:
            raise LedgerIntegrityError(f"artifact bytes are not canonical: {ref.digest}")
        try:
            validate_doc(ref.schema_ref, doc)
        except ContractError as exc:
            raise LedgerIntegrityError(
                f"artifact schema mismatch: {ref.digest}: {exc}"
            ) from None
        return doc


def _utcnow() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _open_nofollow(path: Path, flags: int, mode: int = 0o600) -> int:
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)


class EventLedgerV2:
    """Single-writer, append-only V2 events.jsonl.

    Existing bytes are verified before the writer opens. A V1 file, corrupt
    middle line, or torn V2 tail is refused. Recovery uses a new child episode
    instead of truncating or rewriting the original trust root.
    """

    def __init__(
        self,
        episode_dir: Path | str,
        episode_id: str,
        *,
        clock: Callable[[], str] = _utcnow,
    ) -> None:
        self.episode_dir = Path(episode_dir)
        self.episode_dir.mkdir(parents=True, exist_ok=True)
        self.episode_id = episode_id
        self.path = self.episode_dir / "events.jsonl"
        self.object_store = ObjectStoreV2(self.episode_dir)
        self._clock = clock
        self._closed = False

        lock_path = self.episode_dir / ".events.writer.lock"
        try:
            self._lock_fd = _open_nofollow(lock_path, os.O_RDWR | os.O_CREAT)
        except OSError as exc:
            raise LedgerIntegrityError("cannot open episode writer lock") from exc
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self._lock_fd)
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise ConcurrentWriterError("episode already has an active writer") from None
            raise

        try:
            existing = load_events_v2(self.path) if self.path.exists() else []
            if existing:
                raise LedgerIntegrityError(
                    "existing V2 episode cannot resume in place; create a child episode"
                )
            self._sequence = 0
            self._previous_hash = ZERO_DIGEST
            self._fd = _open_nofollow(
                self.path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            )
        except BaseException:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            raise

    def write_artifact(self, model: Any) -> ArtifactRefV2:
        return self.object_store.put_model(model)

    def append(
        self,
        event_type: EventTypeV2,
        *,
        schema_ref: str,
        payload_value: EventPayloadValue,
        turn_id: int | None,
        correlation_id: str,
        causation_id: str | None = None,
    ) -> EventV2:
        if self._closed:
            raise LedgerIntegrityError("cannot append to a closed ledger")
        event = EventV2.create(
            event_type=event_type,
            schema_ref=schema_ref,
            episode_id=self.episode_id,
            turn_id=turn_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            sequence_number=self._sequence,
            occurred_at=self._clock(),
            payload_value=payload_value,
            previous_event_hash=self._previous_hash,
        )
        raw = (canonical(event.to_doc()) + "\n").encode("utf-8")
        offset = 0
        while offset < len(raw):
            written = os.write(self._fd, raw[offset:])
            if written <= 0:
                raise LedgerIntegrityError("short write to episode ledger")
            offset += written
        os.fsync(self._fd)
        self._sequence += 1
        self._previous_hash = event.event_hash
        return event

    @property
    def terminal_hash(self) -> str:
        return self._previous_hash

    @property
    def sequence(self) -> int:
        return self._sequence

    def close(self) -> None:
        if self._closed:
            return
        os.close(self._fd)
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        os.close(self._lock_fd)
        self._closed = True

    def __enter__(self) -> EventLedgerV2:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def load_events_v2(path: Path | str) -> list[EventV2]:
    source = Path(path)
    if not source.exists():
        return []
    if source.is_symlink():
        raise LedgerIntegrityError("event ledger path must not be a symlink")
    raw = source.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise LedgerIntegrityError("torn V2 event-log tail detected")
    events: list[EventV2] = []
    previous_hash = ZERO_DIGEST
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line:
            raise LedgerIntegrityError(f"blank event-log line {line_number}")
        try:
            doc = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LedgerIntegrityError(f"corrupt event-log line {line_number}") from None
        if not isinstance(doc, dict):
            raise LedgerIntegrityError(f"event-log line {line_number} is not an object")
        if doc.get("schema") != 2:
            raise LedgerIntegrityError(
                f"event-log line {line_number} is not schema 2; V1 is read-only"
            )
        if canonical(doc).encode("utf-8") != line:
            raise LedgerIntegrityError(f"event-log line {line_number} is not canonical")
        try:
            event = EventV2.from_doc(doc)
        except ContractError as exc:
            raise LedgerIntegrityError(
                f"invalid event at line {line_number}: {exc}"
            ) from None
        if event.sequence_number != len(events):
            raise LedgerIntegrityError(
                f"event sequence mismatch at line {line_number}: expected {len(events)}"
            )
        if event.previous_event_hash != previous_hash:
            raise LedgerIntegrityError(f"event chain break at line {line_number}")
        if events and event.episode_id != events[0].episode_id:
            raise LedgerIntegrityError(f"episode identity changed at line {line_number}")
        events.append(event)
        previous_hash = event.event_hash
    return events


@dataclass(frozen=True)
class LedgerVerificationV2:
    episode_id: str
    event_count: int
    artifact_count: int
    terminal_event_hash: str
    termination_reason: str


def verify_ledger_v2(
    episode_dir: Path | str,
    *,
    expected_environment_id: str | None = None,
    require_terminal: bool = True,
) -> LedgerVerificationV2:
    root = Path(episode_dir)
    events = load_events_v2(root / "events.jsonl")
    if not events:
        raise LedgerIntegrityError("episode ledger is empty")
    if require_terminal and events[-1].event_type is not EventTypeV2.EPISODE_TERMINATED:
        raise LedgerIntegrityError("episode ledger has no terminal receipt")
    store = ObjectStoreV2(root)
    refs: dict[str, ArtifactRefV2] = {}
    event_ref_digests: set[str] = set()
    for event in events:
        if isinstance(event.payload_value, ArtifactRefV2):
            refs[event.payload_value.digest] = event.payload_value
            event_ref_digests.add(event.payload_value.digest)
        if isinstance(event.payload_value, EpisodeReceiptV2):
            for ref in event.payload_value.artifacts:
                refs[ref.digest] = ref
    for ref in refs.values():
        store.read_doc(ref)

    termination_reason = "open"
    if events[-1].event_type is EventTypeV2.EPISODE_TERMINATED:
        receipt = events[-1].payload_value
        assert isinstance(receipt, EpisodeReceiptV2)
        if (
            expected_environment_id is not None
            and receipt.environment.descriptor_id != expected_environment_id
        ):
            raise LedgerIntegrityError("episode environment descriptor mismatch")
        receipt_digests = {ref.digest for ref in receipt.artifacts}
        if receipt_digests != event_ref_digests:
            raise LedgerIntegrityError("terminal receipt artifact manifest mismatch")
        start = events[0]
        if start.event_type is not EventTypeV2.EPISODE_STARTED or not isinstance(
            start.payload_value, ArtifactRefV2
        ):
            raise LedgerIntegrityError("ledger does not start with environment custody")
        if store.read_doc(start.payload_value) != receipt.environment.to_doc():
            raise LedgerIntegrityError("started environment does not match terminal receipt")
        termination_reason = receipt.termination_reason.value
    return LedgerVerificationV2(
        episode_id=events[0].episode_id,
        event_count=len(events),
        artifact_count=len(refs),
        terminal_event_hash=events[-1].event_hash,
        termination_reason=termination_reason,
    )


class EpisodeRecorderV2:
    """Own terminal-receipt closure for success, failure, and interruption."""

    def __init__(
        self,
        episode_dir: Path | str,
        *,
        episode_id: str,
        environment: EnvironmentDescriptorV2,
        policies: tuple[PolicyDescriptorV2, ...] | list[PolicyDescriptorV2],
        seed: int | None,
        compute: ComputeConfigV2,
        scored: bool,
        parent_episode_id: str | None = None,
        parent_terminal_event_hash: str | None = None,
        clock: Callable[[], str] = _utcnow,
    ) -> None:
        self.environment = environment
        self.policies = tuple(policies)
        self.seed = seed
        self.compute = compute
        self.scored = scored
        self.parent_episode_id = parent_episode_id
        self.parent_terminal_event_hash = parent_terminal_event_hash
        if (parent_episode_id is None) != (parent_terminal_event_hash is None):
            raise ContractError("resume parent id and terminal hash must appear together")
        if scored and parent_episode_id is not None:
            raise ContractError("scored V2 episodes cannot resume")
        if scored and compute.execution_mode is not ExecutionModeV2.DAG_TX:
            raise ContractError("scored V2 episodes require dag_tx")
        self.ledger = EventLedgerV2(episode_dir, episode_id, clock=clock)
        self._artifacts: dict[str, ArtifactRefV2] = {}
        self._terminated = False
        env_ref = self.record_artifact(environment)
        self.ledger.append(
            EventTypeV2.EPISODE_STARTED,
            schema_ref=environment.SCHEMA_REF,
            payload_value=env_ref,
            turn_id=None,
            correlation_id=episode_id,
        )

    def record_artifact(self, model: Any) -> ArtifactRefV2:
        ref = self.ledger.write_artifact(model)
        self._artifacts[ref.digest] = ref
        return ref

    def record_reference(
        self,
        event_type: EventTypeV2,
        model: Any,
        *,
        turn_id: int,
        correlation_id: str,
        causation_id: str | None = None,
    ) -> ArtifactRefV2:
        ref = self.record_artifact(model)
        self.ledger.append(
            event_type,
            schema_ref=ref.schema_ref,
            payload_value=ref,
            turn_id=turn_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        return ref

    def terminate(
        self,
        reason: EpisodeTerminationV2,
        *,
        turns_completed: int,
    ) -> EpisodeReceiptV2:
        if self._terminated:
            raise LedgerIntegrityError("episode already terminated")
        receipt = EpisodeReceiptV2.create(
            episode_id=self.ledger.episode_id,
            parent_episode_id=self.parent_episode_id,
            parent_terminal_event_hash=self.parent_terminal_event_hash,
            environment=self.environment,
            policies=self.policies,
            seed=self.seed,
            compute=self.compute,
            scored=self.scored,
            termination_reason=reason,
            turns_completed=turns_completed,
            artifacts=tuple(sorted(self._artifacts.values(), key=lambda item: item.digest)),
        )
        event = self.ledger.append(
            EventTypeV2.EPISODE_TERMINATED,
            schema_ref=receipt.SCHEMA_REF,
            payload_value=receipt,
            turn_id=None,
            correlation_id=self.ledger.episode_id,
        )
        assert isinstance(event.payload_value, EpisodeReceiptV2)
        self._terminated = True
        return event.payload_value

    def close(self) -> None:
        self.ledger.close()

    def __enter__(self) -> EpisodeRecorderV2:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            if not self._terminated:
                reason = (
                    EpisodeTerminationV2.CANCELLED
                    if exc_type in {KeyboardInterrupt, SystemExit}
                    else EpisodeTerminationV2.FAILURE
                )
                self.terminate(reason, turns_completed=0)
        finally:
            self.close()
