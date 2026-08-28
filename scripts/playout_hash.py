"""Ad-hoc random playout hasher: run N random-legal turns, print final state hash.

Run twice with the same seed; identical output proves end-to-end determinism
of the rules layer (no hidden rng, no wall clock, no dict-order leaks).

    uv run python scripts/playout_hash.py --seed 7 --turns 60
"""

from __future__ import annotations

import argparse
import random

from civ_arena.canonical import state_hash
from civ_arena.game.sim.layouts import duel_start
from civ_arena.game.sim.rules import apply_action, check_action
from civ_arena.game.sim.state import SimState, neighbors, tile_key

ACTION_TOOLS = ["move_unit", "attack", "fortify", "found_city", "set_research",
                "set_city_production", "purchase"]


def random_action(rng: random.Random, state: SimState, player_id: int) -> dict | None:
    my_units = [u for u in state.units.values() if u["owner"] == player_id]
    other_units = [u for u in state.units.values() if u["owner"] != player_id]
    my_cities = [c for c in state.cities.values() if c["owner"] == player_id]
    for _ in range(60):  # rejection-sample a legal action
        tool = rng.choice(ACTION_TOOLS)
        args: dict = {}
        if tool == "move_unit" and my_units:
            u = rng.choice(my_units)
            q, r = rng.choice(sorted(neighbors(u["q"], u["r"])))
            args = {"unit_id": u["unit_id"], "dest": tile_key(q, r)}
        elif tool == "attack" and my_units and other_units:
            args = {"unit_id": rng.choice(my_units)["unit_id"],
                    "target_id": rng.choice(other_units)["unit_id"]}
        elif tool == "fortify" and my_units or tool == "found_city" and my_units:
            args = {"unit_id": rng.choice(my_units)["unit_id"]}
        elif tool == "set_research":
            args = {"tech_id": rng.choice(["POTTERY", "MINING", "ANIMAL_HUSBANDRY",
                                           "ARCHERY", "MASONRY"])}
        elif tool == "set_city_production" and my_cities:
            args = {"city_id": rng.choice(my_cities)["city_id"],
                    "item_id": rng.choice(["WARRIOR", "SCOUT", "SETTLER", "MONUMENT"])}
        elif tool == "purchase" and my_cities:
            args = {"city_id": rng.choice(my_cities)["city_id"],
                    "item_id": rng.choice(["WARRIOR", "SCOUT", "MONUMENT", "GRANARY"])}
        else:
            continue
        if not args:
            continue
        if check_action(state, player_id, tool, args) is None:
            return {"tool": tool, "args": args}
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--turns", type=int, default=60)
    opts = ap.parse_args()

    state = SimState.from_doc(duel_start(opts.seed))
    rng = random.Random(opts.seed * 31 + 5)
    acted = 0
    for _turn in range(1, opts.turns + 1):
        for player_id in (0, 1):
            for _ in range(4):  # a few random acts per phase
                act = random_action(rng, state, player_id)
                if act is None:
                    continue
                apply_action(state, player_id, act["tool"], act["args"])
                acted += 1
    print(f"seed={opts.seed} turns={opts.turns} actions={acted} "
          f"final_hash={state_hash(state.to_doc())}")


if __name__ == "__main__":
    main()
