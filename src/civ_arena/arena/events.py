"""Append-only JSONL event log — the single source of truth.

The log is the only durable store: the dedupe index and the replay engine
both REBUILD from it, so live / resume / replay cannot disagree. Every record
carries the 8-tuple namespacing and a strictly increasing ``seq`` (the log's
only ordering authority). ``ts``/``duration_ms`` are envelope fields excluded
from every replay-relevant hash.
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any

SCHEMA = 1

EVENT_KINDS = frozenset({
    "MATCH_START", "LEASE_GRANT", "LEASE_RELEASE", "LEASE_EXPIRED", "AMBIENT",
    "TOOL_CALL", "TOOL_RESULT", "VIOLATION", "CHECKPOINT", "TURN_END",
    "MATCH_END", "HEARTBEAT", "UNAUTHORIZED_TOOL_CALL",
    # Spectator-capture lane: a human plays the seat at the keyboard; the
    # harness only observes. Ambient rows ride INSIDE these payloads (never
    # as top-level AMBIENT events — replay compares those against a sim
    # replay) and the kinds are outside the comparable strip by construction.
    "SPECTATOR_SNAPSHOT", "HUMAN_TURN_START", "HUMAN_TURN_END",
})

NAMESPACE_FIELDS = (
    "match_id", "game_instance_id", "turn", "phase_player_id", "player_id",
    "agent_id", "seq", "visibility_scope",
)


def _utcnow() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Continue seq from whatever survived on disk (resume path). A torn
        # trailing line is PHYSICALLY dropped here — otherwise the next append
        # would concatenate onto it and corrupt the log mid-file.
        existing, torn = self._load()
        self._seq = len(existing)
        if torn:
            self._rewrite(existing)
        # long-lived append handle; closed explicitly via close()/truncate_to()
        self._file = open(self.path, "a", encoding="utf-8")  # noqa: SIM115

    # -- writing -----------------------------------------------------------
    def write(
        self,
        kind: str,
        *,
        match_id: str,
        game_instance_id: str,
        turn: int,
        phase_player_id: int,
        player_id: int | None,
        agent_id: str | None,
        visibility_scope: str,
        duration_ms: int | None = None,
        **fields: Any,
    ) -> int:
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind: {kind}")
        rec: dict[str, Any] = {
            "schema": SCHEMA,
            "seq": self._seq,
            "kind": kind,
            "ts": _utcnow(),
            "match_id": match_id,
            "game_instance_id": game_instance_id,
            "turn": turn,
            "phase_player_id": phase_player_id,
            "player_id": player_id,
            "agent_id": agent_id,
            "visibility_scope": visibility_scope,
            **fields,
        }
        if duration_ms is not None:
            rec["duration_ms"] = duration_ms
        line = json.dumps(rec, sort_keys=True, separators=(",", ":"))
        self._file.write(line + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())
        self._seq += 1
        return rec["seq"]

    # -- reading -------------------------------------------------------------
    def records(self) -> list[dict[str, Any]]:
        """Parsed records; a torn trailing line is dropped (kill -9 mid-write)."""
        return self._load()[0]

    def __len__(self) -> int:
        return self._seq

    def _load(self) -> tuple[list[dict[str, Any]], bool]:
        """Returns (records, torn_tail_detected). Fails CLOSED on a stored
        seq that does not equal its position — seq is the log's only
        ordering authority and must not be trusted from disk."""
        if not self.path.exists():
            return [], False
        raw = self.path.read_text(encoding="utf-8").splitlines()
        out: list[dict[str, Any]] = []
        for i, line in enumerate(raw):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                if i == len(raw) - 1:
                    return out, True  # torn tail from a crash mid-write
                raise ValueError(f"corrupt event log line {i}: {line[:80]!r}") from None
            if not isinstance(rec, dict) or rec.get("seq") != len(out):
                raise ValueError(
                    f"event log seq broken at line {i}: expected {len(out)}, "
                    f"got {rec.get('seq') if isinstance(rec, dict) else type(rec)}"
                )
            out.append(rec)
        return out, False

    def _rewrite(self, recs: list[dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for rec in recs:
                fh.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    # -- resume support ----------------------------------------------------------
    def truncate_to(self, keep: int) -> None:
        """Keep the first ``keep`` records; rewrite atomically; continue seq there."""
        if keep < 0:
            raise ValueError(f"cannot truncate to a negative count: {keep}")
        recs, _ = self._load()
        if keep > len(recs):
            raise ValueError(f"cannot truncate to {keep}: only {len(recs)} records")
        self._file.close()
        self._rewrite(recs[:keep])
        self._file = open(self.path, "a", encoding="utf-8")  # noqa: SIM115
        self._seq = keep

    def close(self) -> None:
        self._file.close()
