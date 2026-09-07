"""Run-dir wire ledgers: per-attempt costs (always on) + full wire
transcripts (opt-in via ``llm.wire_log``).

``llm_costs.jsonl`` — one compact line per counted POST attempt (tokens +
latency, no payloads): the per-decision cost record the efficiency
flywheel aggregates.

``llm-wire/<agent_id>.jsonl`` — the exact request/response docs the model
saw and returned (thinking blocks included), one line per attempt. This
is what makes a run distillable after the fact: "what context produced
what decision" is recoverable from disk. Headers are NEVER recorded (the
key rides headers, not the body); as defense in depth the serialized
record is swept through the client's redactor with the live env key, and
the file is chmod 0600 inside the (git-ignored) run dir.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from civ_arena.agents.llm.client import MiniMaxMessagesClient
from civ_arena.config import LLMSpec

_COST_FIELDS = ("ts", "request_kind", "attempt", "status_code", "latency_ms",
                "model", "payload_hash", "input_tokens", "output_tokens",
                # CAP-03 (F-07): logical identity for decision-level joins
                "decision_id", "logical_request_id", "request_set_key")


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


class CostLedger:
    """Compact per-attempt cost lines (no payloads)."""

    def __init__(self, run_dir: Path) -> None:
        self._path = Path(run_dir) / "llm_costs.jsonl"

    def note(self, agent_id: str, player_id: int, record: dict[str, Any]) -> None:
        row = {"ts": record.get("ts", _utcnow()),
               "agent_id": agent_id, "player_id": int(player_id)}
        for key in _COST_FIELDS:
            if key != "ts":
                row[key] = record.get(key)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


class WireLog:
    """Full request/response transcript for one agent (opt-in)."""

    def __init__(self, run_dir: Path, agent_id: str, player_id: int,
                 spec: LLMSpec) -> None:
        self._dir = Path(run_dir) / "llm-wire"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / f"{agent_id}.jsonl"
        self._agent_id = agent_id
        self._player_id = player_id
        self._spec = spec

    def note(self, record: dict[str, Any]) -> None:
        doc = {**record, "agent_id": self._agent_id,
               "player_id": self._player_id}
        text = json.dumps(doc, sort_keys=True)
        # defense in depth: the request body never carries the key, but a
        # reflecting proxy error can echo it — sweep the serialized record
        key = os.environ.get(self._spec.api_key_env, "")
        if key:
            text = MiniMaxMessagesClient._redact(text, key)  # noqa: SLF001
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                     0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
            fh.flush()
            os.fsync(fh.fileno())
