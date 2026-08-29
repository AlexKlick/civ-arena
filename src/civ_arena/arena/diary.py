"""The diary: a per-player cross-turn note, the smallest memory rung.

NOT game state. A diary write makes no adapter call, produces no
MutationRecords, and does not touch any state hash — a tampered diary cannot
move a game outcome, only what a model reads next turn. For that reason the
diary is deliberately OUTSIDE the checkpoint content hash: the event log is
its only durable store, and ``from_log`` rebuilds it on resume exactly the
way the dedupe index is rebuilt (same trust root, no schema bump).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_DIARY_CHARS = 2000


@dataclass
class DiaryStore:
    """In-memory view of the per-player diary; last write wins."""

    _notes: dict[int, str] = field(default_factory=dict)

    def get(self, player_id: int) -> str:
        return self._notes.get(int(player_id), "")

    def write(self, player_id: int, text: str) -> None:
        self._notes[int(player_id)] = text

    @classmethod
    def from_log(cls, records: list[dict[str, Any]]) -> DiaryStore:
        """Rebuild from the event log: every ACCEPTED write_diary call, in
        seq order. The TOOL_RESULT for an accepted write_diary is always the
        record immediately after its TOOL_CALL (``_emit_pair`` writes the two
        back-to-back); pairing by adjacency ignores dangling calls torn off
        by a crash — a checkpoint-resume truncates those anyway."""
        store = cls()
        for i, rec in enumerate(records):
            if rec.get("kind") != "TOOL_CALL" or rec.get("tool") != "write_diary":
                continue
            if i + 1 >= len(records):
                continue
            nxt = records[i + 1]
            if (nxt.get("kind") != "TOOL_RESULT"
                    or nxt.get("tool") != "write_diary"
                    or nxt.get("status") != "accepted"
                    # namespace identity: the result must belong to the same
                    # player/agent/turn/match as the call (adjacency alone
                    # would let a foreign record authorize the preceding
                    # text — e.g. in a concatenated or tampered log)
                    or nxt.get("player_id") != rec.get("player_id")
                    or nxt.get("agent_id") != rec.get("agent_id")
                    or nxt.get("turn") != rec.get("turn")
                    or nxt.get("match_id") != rec.get("match_id")):
                continue
            text = (rec.get("args") or {}).get("text")
            pid = rec.get("player_id")
            if isinstance(text, str) and isinstance(pid, int):
                store.write(pid, text)
        return store
