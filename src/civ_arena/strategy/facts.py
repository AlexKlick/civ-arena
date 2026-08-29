"""Own-state samples folded from observation digests ONLY.

AMBIENT manifests are never folded here: they are referee-scope records that
contain the opponent's economy, and anything this store feeds into a prompt
must be exactly what the player saw. Correctness under fog falls out for
free — an own city captured by the enemy is absent from the next
``get_cities`` digest, so the count drops instead of overstating.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Facts:
    # player_id -> turn -> merged sample (latest observation wins per key)
    samples: dict[int, dict[int, dict[str, Any]]] = field(default_factory=dict)

    def note(self, player_id: int, turn: int, sample: dict[str, Any]) -> None:
        if not isinstance(player_id, int) or not isinstance(turn, int):
            return
        bucket = self.samples.setdefault(player_id, {}).setdefault(turn, {})
        for key, value in sample.items():
            # ints and strings only — canonical-JSON safe by construction
            if isinstance(value, (int, str)) and not isinstance(value, bool):
                bucket[key] = value

    def value(self, player_id: int, metric: str, turn: int) -> int | str | None:
        """Latest sample at or before ``turn`` that carries the metric."""
        turns = self.samples.get(int(player_id)) if isinstance(player_id, int) \
            else self.samples.get(player_id)
        if not turns:
            return None
        for t in sorted(turns, reverse=True):
            if t <= turn and metric in turns[t]:
                return turns[t][metric]
        return None
