"""Phase-boundary engine: the DECLARED ambient batch each player's phase opens with.

Everything here is deterministic (no rng draws) and returns an explicit
manifest of MutationRecords. The referee requests this batch at begin_phase;
the manifest is the authorization source for the watchdog — ambient effects
are declared at phase boundaries, never inferred from mutation shape.
"""

from __future__ import annotations

from typing import Any

from civ_arena.game.adapter import MutationRecord
from civ_arena.game.sim.rules import _m
from civ_arena.game.sim.state import (
    BUILDINGS,
    GROWTH_BASE,
    GROWTH_PER_POP,
    HEAL_PER_TURN,
    TECHS,
    TERRAIN,
    UNIT_TYPES,
    SimState,
)

POP_CAP_BASE = 7
POP_CAP_GRANARY = 2


def _production_per_turn(state: SimState, city: dict[str, Any]) -> int:
    tile = state.tile(city["q"], city["r"])
    hill = 3 if tile is not None and tile["terrain"] == "HILL" else 0
    return 3 + hill + 2 * city["population"]


def _pop_cap(city: dict[str, Any]) -> int:
    return POP_CAP_BASE + (POP_CAP_GRANARY if "GRANARY" in city["buildings"] else 0)


def run_ambient(state: SimState, player_id: int) -> list[MutationRecord]:
    muts: list[MutationRecord] = []
    own_units = sorted(
        (u for u in state.units.values() if u["owner"] == player_id),
        key=lambda u: u["unit_id"],
    )
    own_cities = sorted(
        (c for c in state.cities.values() if c["owner"] == player_id),
        key=lambda c: c["city_id"],
    )

    # 1. healing — units that were idle last turn (full movement) heal
    for unit in own_units:
        if unit["movement"] == unit["max_movement"] and unit["hp"] < 100:
            old = unit["hp"]
            unit["hp"] = min(100, old + HEAL_PER_TURN)
            muts.append(_m("unit.healed", "unit", unit["unit_id"], "hp", old,
                           unit["hp"], "ambient"))

    # 2. movement refresh
    for unit in own_units:
        if unit["movement"] != unit["max_movement"]:
            old = unit["movement"]
            unit["movement"] = unit["max_movement"]
            muts.append(_m("unit.refreshed", "unit", unit["unit_id"], "movement", old,
                           unit["max_movement"], "ambient"))

    # 3. per-city food / growth / production
    science_total = 0
    gold_total = 0
    for city in own_cities:
        territory_food = sum(
            TERRAIN[t["terrain"]]["food"]
            for t in state.tiles.values()
            if t["city"] == city["city_id"]
        )
        net_food = 2 + territory_food - 2 * city["population"]
        if city["population"] >= _pop_cap(city):
            net_food = min(net_food, 0)
        old_bucket = city["food_bucket"]
        city["food_bucket"] = max(0, old_bucket + net_food)
        muts.append(_m("city.food", "city", city["city_id"], "food_bucket", old_bucket,
                       city["food_bucket"], "ambient"))
        threshold = GROWTH_BASE + GROWTH_PER_POP * city["population"]
        if city["food_bucket"] >= threshold:
            old_pop = city["population"]
            city["population"] += 1
            city["food_bucket"] -= threshold
            muts.append(_m("city.grew", "city", city["city_id"], "population", old_pop,
                           city["population"], "ambient"))
            muts.append(_m("city.grew", "city", city["city_id"], "food_bucket",
                           city["food_bucket"] + threshold, city["food_bucket"], "ambient"))

        prod = _production_per_turn(state, city)
        science_total += 2 * city["population"]
        gold_total += city["population"] + sum(
            BUILDINGS[b].get("gold", 0) for b in city["buildings"]
        )

        if city["production_queue"]:
            old_pb = city["production_bucket"]
            city["production_bucket"] = old_pb + prod
            muts.append(_m("city.produced", "city", city["city_id"], "production_bucket",
                           old_pb, city["production_bucket"], "ambient"))
            while city["production_queue"]:
                item = city["production_queue"][0]
                cost = BUILDINGS[item]["cost"] if item in BUILDINGS else UNIT_TYPES[item]["cost"]
                if city["production_bucket"] < cost:
                    break
                city["production_queue"] = city["production_queue"][1:]
                city["production_bucket"] -= cost
                if item in BUILDINGS:
                    old_b = list(city["buildings"])
                    city["buildings"] = sorted([*old_b, item])
                    muts.append(_m("city.building_built", "city", city["city_id"],
                                   "buildings", old_b, list(city["buildings"]), "ambient"))
                    muts.append(_m("city.produced", "city", city["city_id"],
                                   "production_bucket",
                                   city["production_bucket"] + cost,
                                   city["production_bucket"], "ambient"))
                else:
                    _, unit = state.spawn_unit(player_id, item, city["q"], city["r"])
                    muts.append(_m("unit.built", "unit", unit["unit_id"], "created", 0, 1,
                                   "ambient"))
                    muts.append(_m("city.produced", "city", city["city_id"],
                                   "production_bucket",
                                   city["production_bucket"] + cost,
                                   city["production_bucket"], "ambient"))
                muts.append(_m("city.queue_advanced", "city", city["city_id"],
                               "production_queue",
                               [item, *city["production_queue"]],
                               list(city["production_queue"]), "ambient"))

    # 4. empire-level yields
    player = state.player(player_id)
    if science_total:
        old = player["science_bucket"]
        player["science_bucket"] = old + science_total
        muts.append(_m("player.science", "player", str(player_id), "science_bucket", old,
                       player["science_bucket"], "ambient"))
    if gold_total:
        old = player["gold"]
        player["gold"] = old + gold_total
        muts.append(_m("player.income", "player", str(player_id), "gold", old,
                       player["gold"], "ambient"))

    # 5. research completion
    researching = player["researching"]
    if researching:
        cost = TECHS[researching]["cost"]
        if player["science_bucket"] >= cost:
            old_sb = player["science_bucket"]
            old_res = list(player["researched"])
            player["science_bucket"] -= cost
            player["researched"] = sorted([*old_res, researching])
            player["researching"] = ""
            muts.append(_m("player.tech_researched", "player", str(player_id), "researched",
                           old_res, list(player["researched"]), "ambient"))
            muts.append(_m("player.tech_researched", "player", str(player_id),
                           "science_bucket", old_sb, player["science_bucket"], "ambient"))
            muts.append(_m("player.tech_researched", "player", str(player_id),
                           "researching", researching, "", "ambient"))

    return muts


def stock_ai_turn(state: SimState, player_id: int) -> list[MutationRecord]:
    """A trivial built-in-AI stand-in, used ONLY by the stock_ai/broken_freeze path.

    It moves each unit one passable step and keeps production on WARRIORs —
    exactly the kind of uncoordinated engine behavior the freeze must suppress.
    """
    from civ_arena.game.sim.rules import apply_action, check_action
    from civ_arena.game.sim.state import neighbors, tile_key

    muts: list[MutationRecord] = []
    for unit in sorted(
        (u for u in state.units.values() if u["owner"] == player_id),
        key=lambda u: u["unit_id"],
    ):
        live = state.unit(unit["unit_id"])
        if live is None:
            continue
        for nq, nr in sorted(neighbors(live["q"], live["r"])):
            args = {"unit_id": live["unit_id"], "dest": tile_key(nq, nr)}
            if check_action(state, player_id, "move_unit", args) is None:
                muts.extend(apply_action(state, player_id, "move_unit", args))
                break
    for city in sorted(
        (c for c in state.cities.values() if c["owner"] == player_id),
        key=lambda c: c["city_id"],
    ):
        args = {"city_id": city["city_id"], "item_id": "WARRIOR"}
        if check_action(state, player_id, "set_city_production", args) is None:
            muts.extend(apply_action(state, player_id, "set_city_production", args))
    return muts
