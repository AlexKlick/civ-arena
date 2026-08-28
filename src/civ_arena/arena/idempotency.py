"""Idempotency: content-derived default keys, explicit nonce opt-out.

Default key = player + tool + TURN + canonical args — retry-safe within a
turn (the failure window that matters) while allowing the same content again
next turn. Agents that intentionally repeat an effect within one turn pass an
explicit nonce key. The durable index is the event log itself; this class is
the in-memory view, rebuilt by ``from_log`` on resume and replay.

Known collision semantics (deliberate): an EXPLICIT nonce key excludes both
turn and args, so reusing one nonce for the same player+tool with different
args (or in a later turn) returns the FIRST cached result and suppresses the
new action — a caller-visible contract, not a double-execution hazard.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from civ_arena.canonical import canonical, sha256_hex

KEY_VERSION = "v1"


class DedupeIndex:
    def __init__(self) -> None:
        self._results: dict[str, dict[str, Any]] = {}

    @staticmethod
    def key_for(
        player_id: int,
        tool: str,
        args: Mapping[str, Any],
        client_key: str | None,
        turn: int,
    ) -> str:
        if client_key is not None:
            return sha256_hex(f"{KEY_VERSION}|{player_id}|{tool}|n|{client_key}")
        return sha256_hex(f"{KEY_VERSION}|{player_id}|{tool}|{turn}|{canonical(dict(args))}")

    def seen(self, key: str) -> dict[str, Any] | None:
        return self._results.get(key)

    def record(self, key: str, result_doc: dict[str, Any]) -> None:
        self._results[key] = result_doc

    def forget(self, key: str) -> None:
        """Drop a recorded key (rollback path: a retry may re-execute)."""
        self._results.pop(key, None)

    def __len__(self) -> int:
        return len(self._results)

    @classmethod
    def from_log(cls, records: list[dict[str, Any]]) -> DedupeIndex:
        """Rebuild by replaying the log IN ORDER: a rolled-back key is
        forgotten (retryable), and a LATER committed acceptance of the same
        key re-registers it — the rebuilt index ends exactly where the live
        one was, instead of blanket-excluding everything ever rolled back."""
        idx = cls()
        for rec in records:
            if rec.get("kind") != "TOOL_RESULT":
                continue
            key = rec.get("idempotency_key")
            if not key:
                continue
            if rec.get("rolled_back"):
                idx.forget(key)
            elif rec.get("status") == "accepted" and not rec.get("duplicate"):
                idx.record(key, {
                    "status": rec["status"],
                    "tool": rec.get("tool"),
                    "result": rec.get("result_doc"),
                    "after_state_hash": rec.get("after_state_hash"),
                })
        return idx
