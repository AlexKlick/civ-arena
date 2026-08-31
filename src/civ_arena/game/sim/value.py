"""Hand-crafted value head (M15a): integer score vector + scalarization.

Mirrors the component set the arena reports at match end (cities,
population, gold, techs, units) as pure rules-layer functions the planner
can call on any SimState — including belief-state reconstructions.
Integers only; no learned component this cycle.
"""

from __future__ import annotations

from civ_arena.game.sim.state import SimState

SCORE_COMPONENTS = ("cities", "population", "gold", "techs", "units")

# Deliberately hand-set (no training in the first cycle): cities are the
# engine of everything else, population/techs compound, units are tempo,
# gold is the tiebreaker.
DEFAULT_WEIGHTS: dict[str, int] = {
    "cities": 100,
    "population": 20,
    "techs": 30,
    "units": 10,
    "gold": 1,
}


def score_vector(state: SimState, player_id: int) -> dict[str, int]:
    """The per-player score components, all integers."""
    cities = [c for c in state.cities.values() if c["owner"] == player_id]
    player = state.player(player_id)
    return {
        "cities": len(cities),
        "population": sum(c["population"] for c in cities),
        "gold": player["gold"],
        "techs": len(player["researched"]),
        "units": sum(1 for u in state.units.values() if u["owner"] == player_id),
    }


def scalarize(vec: dict[str, int], weights: dict[str, int] | None = None) -> int:
    w = DEFAULT_WEIGHTS if weights is None else weights
    return sum(w[k] * vec[k] for k in SCORE_COMPONENTS)


def value_of(state: SimState, player_id: int,
             weights: dict[str, int] | None = None) -> int:
    """Own scalar score minus the strongest rival's — the search objective."""
    own = scalarize(score_vector(state, player_id), weights)
    rivals = [
        scalarize(score_vector(state, int(pid)), weights)
        for pid in state.players
        if int(pid) != player_id
    ]
    return own - max(rivals) if rivals else own
