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


def score_differential(scores: dict, pid: int,
                       weights: dict[str, int] | None = None) -> int:
    """Own scalarized score minus the strongest rival's, over the
    summary.json ``scores`` shape (civ name -> per-civ component dict
    carrying ``player_id``) — the same objective as ``value_of``.

    NOTE: the FIRST function in this module consuming the civ-keyed
    summary shape; every other function here reads a SimState. Rival =
    any entry whose player_id differs; with no rival the own score
    stands (mirroring ``value_of``).
    """
    vals = {entry["player_id"]: scalarize(entry, weights)
            for entry in scores.values()}
    rivals = [v for p, v in vals.items() if p != pid]
    return vals[pid] - max(rivals) if rivals else vals[pid]


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
