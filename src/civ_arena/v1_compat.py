"""Frozen, read-only schema-1 event-log compatibility reader.

Schema-1 episodes remain replayable evidence, but this module deliberately
offers no writer, truncation, resume, or migration operation.  A torn final
record is reported to the caller without changing the source bytes; corruption
anywhere else fails closed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

V1_SCHEMA = 1
V1_EVENT_KINDS = frozenset(
    {
        "MATCH_START",
        "LEASE_GRANT",
        "LEASE_RELEASE",
        "LEASE_EXPIRED",
        "AMBIENT",
        "TOOL_CALL",
        "TOOL_RESULT",
        "VIOLATION",
        "CHECKPOINT",
        "TURN_END",
        "MATCH_END",
        "HEARTBEAT",
        "UNAUTHORIZED_TOOL_CALL",
    }
)


class V1CompatibilityError(ValueError):
    """A historical schema-1 log is not safely readable."""


@dataclass(frozen=True)
class V1EventLog:
    records: tuple[dict[str, Any], ...]
    torn_tail: bool


def load_v1_events_read_only(path: Path | str) -> V1EventLog:
    """Parse a V1 log without opening it for write or repairing its tail."""

    source = Path(path)
    if source.is_symlink():
        raise V1CompatibilityError("V1 event ledger path must not be a symlink")
    raw = source.read_bytes()
    lines = raw.splitlines(keepends=True)
    records: list[dict[str, Any]] = []
    torn_tail = False
    for index, line_with_ending in enumerate(lines):
        is_last = index == len(lines) - 1
        line = line_with_ending.rstrip(b"\r\n")
        if not line:
            continue
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            if is_last and not raw.endswith((b"\n", b"\r")):
                torn_tail = True
                break
            raise V1CompatibilityError(
                f"corrupt V1 event-log line {index + 1}"
            ) from None
        if not isinstance(record, dict):
            raise V1CompatibilityError(
                f"V1 event-log line {index + 1} is not an object"
            )
        if record.get("schema") != V1_SCHEMA:
            raise V1CompatibilityError(
                f"V1 event-log line {index + 1} has unsupported schema"
            )
        if record.get("seq") != len(records):
            raise V1CompatibilityError(
                f"V1 event sequence mismatch at line {index + 1}: "
                f"expected {len(records)}"
            )
        if record.get("kind") not in V1_EVENT_KINDS:
            raise V1CompatibilityError(
                f"V1 event-log line {index + 1} has unknown event kind"
            )
        records.append(record)
    return V1EventLog(tuple(records), torn_tail)
