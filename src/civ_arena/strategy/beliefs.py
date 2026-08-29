"""Last-known foreign entities: persistence of what the player SAW.

The visibility projection is momentary — a foreign city vanishes from
``get_cities`` the moment its tile leaves observation. The belief store keeps
the last projected fields per entity so the renderer can surface "u8 WARRIOR
@ 2,0 (t14)" instead of nothing. No expiry, on purpose: an unobserved death
must not erase the last known position; staleness is labeled at render time.

Fields stored are EXACTLY the projected foreign allowlist fields — never
more than the player legitimately saw. Cap: LRU per entity kind per player,
evicting the least-recently-seen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

BELIEF_CAP_PER_KIND = 32


@dataclass
class BeliefStore:
    # player_id -> entity_id -> record; insertion order = last-seen order
    entries: dict[int, dict[str, dict[str, Any]]] = field(default_factory=dict)
    cap: int = BELIEF_CAP_PER_KIND

    def see(
        self,
        player_id: int,
        turn: int,
        seq: int,
        foreign_units: list[Any] | None = None,
        foreign_cities: list[Any] | None = None,
    ) -> None:
        if not isinstance(player_id, int) or not isinstance(turn, int):
            return
        for e in foreign_units or []:
            if isinstance(e, dict) and isinstance(e.get("unit_id"), str):
                self._put(player_id, e["unit_id"], "unit", e, turn, seq)
        for e in foreign_cities or []:
            if isinstance(e, dict) and isinstance(e.get("city_id"), str):
                self._put(player_id, e["city_id"], "city", e, turn, seq)

    def _put(
        self, player_id: int, entity_id: str, kind: str,
        fields: dict[str, Any], turn: int, seq: int,
    ) -> None:
        bucket = self.entries.setdefault(player_id, {})
        # re-insertion moves to the end: insertion order IS recency order
        bucket.pop(entity_id, None)
        bucket[entity_id] = {
            "kind": kind,
            "fields": dict(fields),
            "last_seen_turn": int(turn),
            "last_seen_seq": int(seq),
        }
        # evict least-recently-seen of the same kind over cap
        same_kind = [k for k, v in bucket.items() if v["kind"] == kind]
        while len(same_kind) > self.cap:
            victim = same_kind.pop(0)
            del bucket[victim]

    def view(self, player_id: int, limit: int = 32) -> list[dict[str, Any]]:
        """Most-recently-seen first, newest sighting (turn, then seq) first."""
        bucket = self.entries.get(int(player_id), {})
        ordered = sorted(
            bucket.items(),
            key=lambda kv: (kv[1]["last_seen_turn"], kv[1]["last_seen_seq"]),
            reverse=True,
        )
        return [{"entity_id": eid, **rec} for eid, rec in ordered[:max(0, limit)]]
