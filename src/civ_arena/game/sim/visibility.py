"""Ground-truth visibility for the simulator — the test oracle.

The projection layer (arena/visibility.py) is validated against THIS, which
is computed independently from raw state, so a leaky projection cannot pass
by construction.
"""

from __future__ import annotations

from dataclasses import dataclass

from civ_arena.game.sim.state import SimState


@dataclass(frozen=True)
class VisibilitySet:
    observable: frozenset[str]
    remembered: frozenset[str]

    def sees(self, key: str) -> bool:
        return key in self.observable

    def knows(self, key: str) -> bool:
        return key in self.remembered or key in self.observable


def ground_truth(state: SimState, player_id: int) -> VisibilitySet:
    observable = state.sight_tiles(player_id)
    remembered = state.revealed_keys(player_id) | observable
    return VisibilitySet(observable=frozenset(observable), remembered=frozenset(remembered))
