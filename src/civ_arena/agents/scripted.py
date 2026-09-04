"""Deterministic scripted policies: expansionist + turtler.

The bots see ONLY their projected observations (the facade) — the same view
an LLM runtime will get. Decisions key on (turn, observation, rng) with no
wall clock, so matches replay and resume deterministically. Rejected tool
calls are normal policy noise and stay in the log.
"""

from __future__ import annotations

import random
from typing import Any

from civ_arena.game.sim.state import hex_dist, neighbors, parse_key

DOCTRINES: dict[str, dict[str, Any]] = {
    "expansionist": {
        "research": ["POTTERY", "ARCHERY", "WRITING", "MINING", "MASONRY",
                     "BRONZE_WORKING", "ANIMAL_HUSBANDRY", "IRRIGATION"],
        "march": True,           # military pushes toward the far corridor
        "fortify_idle": False,
        "build_order": ["SETTLER", "WARRIOR", "ARCHER", "SCOUT"],
        "purchase_pref": ["SCOUT", "WARRIOR", "MONUMENT", "GRANARY"],
        "max_cities": 4,
    },
    "turtler": {
        "research": ["MINING", "MASONRY", "BRONZE_WORKING", "ANIMAL_HUSBANDRY",
                     "POTTERY", "ARCHERY", "WRITING", "IRRIGATION"],
        "march": False,
        "fortify_idle": True,
        "build_order": ["WALLS", "MONUMENT", "WARRIOR", "SPEARMAN"],
        "purchase_pref": ["MONUMENT", "GRANARY", "WARRIOR"],
        "max_cities": 2,
    },
}

_MILITARY = ("WARRIOR", "SPEARMAN", "ARCHER")
_CORNERS = [(4, 1), (1, 4), (-4, -1), (-1, -4)]


def _coord_of(entity: dict[str, Any]) -> tuple[int, int]:
    return parse_key(entity["coord"])


def _steps_toward(unit: dict[str, Any], target: tuple[int, int]) -> list[str]:
    q, r = _coord_of(unit)
    candidates = sorted(
        neighbors(q, r), key=lambda n: (hex_dist(n, target), n)
    )
    return [f"{nq},{nr}" for nq, nr in candidates[:3]]


async def run_policy(runtime: Any, facade: Any) -> None:
    profile = runtime.profile
    doctrine = DOCTRINES[profile.policy]
    rng: random.Random = runtime.rng
    pid = profile.player_id

    overview = await facade.get_overview()
    turn = overview["turn"]
    units = await facade.get_units()
    cities = await facade.get_cities()
    if turn % 7 == 1:
        await facade.get_visible_map()

    # research: first doctrine tech still available
    options = await facade.get_available_research()
    for pick in doctrine["research"]:
        if any(o["tech_id"] == pick for o in options):
            await facade.set_research(pick)
            break

    # cities: keep production going, occasionally purchase. ALWAYS set the
    # doctrine pick (idempotent — the read-back wants cur == item.Hash):
    # lease-start housekeeping fills empty queues BEFORE the policy runs,
    # and conditioning on an empty queue stopped this tool from ever
    # rehearsing (test_dispatch_rehearsal_end_to_end, regression from the
    # B2 housekeeping order — found 2026-09-03).
    for city in cities:
        opts = await facade.get_available_production(city["city_id"])
        item = _pick_build(opts, doctrine, len(cities))
        if item:
            await facade.set_city_production(city["city_id"], item)
        if turn % 3 == 0 and overview["you"]["gold"] >= 120:
            opts = await facade.get_available_production(city["city_id"])
            item = _pick_purchase(opts, doctrine)
            if item:
                await facade.purchase(
                    city["city_id"], item,
                    idempotency_key=f"buy-{turn}-{city['city_id']}-{rng.randint(0, 9999)}",
                )

    my_units = [u for u in units if u["owner_id"] == pid]
    foreigners = [u for u in units if u["owner_id"] != pid]
    settled = await _handle_settlers(facade, my_units, cities, profile, turn)

    attacks_this_turn = 0
    for unit in my_units:
        if unit["type"] == "SETTLER":
            continue  # handled above
        if unit["type"] in _MILITARY:
            target = _in_range_target(unit, foreigners) if foreigners else None
            if target and unit["movement"] > 0 and attacks_this_turn < 2:
                await facade.attack(unit["unit_id"], target["unit_id"])
                attacks_this_turn += 1
            elif doctrine["fortify_idle"] and not unit["fortified"]:
                await facade.fortify(unit["unit_id"])
            elif doctrine["march"] and unit["movement"] > 0:
                await _march(facade, unit, (4, -3) if pid == 0 else (-4, 3))
        elif unit["type"] == "SCOUT" and unit["movement"] > 0:
            corner = _CORNERS[rng.randint(0, len(_CORNERS) - 1)]
            await _march(facade, unit, corner)

    # TURN-COMPLETENESS GATE contract: on the structured unmoved_units
    # bounce, put a standing order on each listed unit and re-end — the
    # rehearsal exercises the same loop the LLM seats run live.
    res = await facade.end_turn()
    if isinstance(res, dict) and res.get("rejection") == "unmoved_units":
        for uid in res.get("unmoved_units") or []:
            await facade.fortify(uid)
        await facade.end_turn()
    _ = settled


def _in_range_target(unit: dict[str, Any],
                     foreigners: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Nearest foreign unit this unit can actually strike this turn."""
    if not foreigners:
        return None
    rng_max = 2 if unit["type"] == "ARCHER" else 1
    pos = _coord_of(unit)
    in_range = [
        f for f in foreigners
        if hex_dist(pos, _coord_of(f)) <= rng_max
    ]
    if not in_range:
        return None
    return min(in_range, key=lambda f: (hex_dist(pos, _coord_of(f)), f["unit_id"]))


async def _handle_settlers(facade: Any, my_units: list[dict], cities: list[dict],
                           profile: Any, turn: int) -> int:
    founded = 0
    for unit in (u for u in my_units if u["type"] == "SETTLER"):
        pos = _coord_of(unit)
        near = any(hex_dist(pos, _coord_of(c)) <= 2 for c in cities)
        if not near:
            doc = await facade.found_city(
                unit["unit_id"], name=f"{profile.agent_id[:12]}-t{turn}")
            if doc.get("status") == "accepted":
                founded += 1
        else:
            await _march(facade, unit, (2, 2) if profile.player_id == 0 else (-2, -2))
    return founded


async def _march(facade: Any, unit: dict[str, Any], target: tuple[int, int]) -> None:
    for dest in _steps_toward(unit, target):
        doc = await facade.move_unit(unit["unit_id"], dest)
        if doc.get("status") == "accepted":
            return


def _pick_build(opts: list[dict], doctrine: dict[str, Any], city_count: int) -> str | None:
    if city_count < doctrine["max_cities"]:
        wanted = ["SETTLER", *doctrine["build_order"]]
    else:
        wanted = doctrine["build_order"]
    by_id = {o["item_id"] for o in opts}
    for item in wanted:
        if item in by_id:
            return item
    return None


def _pick_purchase(opts: list[dict], doctrine: dict[str, Any]) -> str | None:
    by_id = {o["item_id"] for o in opts}
    for item in doctrine["purchase_pref"]:
        if item in by_id:
            return item
    return None
