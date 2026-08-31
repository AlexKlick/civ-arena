"""Game-rule legality checks and effect resolution.

``check_action`` answers "may this player do this now?" with a
``RejectionReason`` (or None). ``apply_action`` mutates the state and returns
the MutationRecords describing exactly what changed. The adapter calls check
first, then apply; the referee additionally enforces lease/ownership/dedupe
policy BEFORE the adapter is ever reached.
"""

from __future__ import annotations

import heapq
from typing import Any

from civ_arena.game.adapter import MutationRecord, RejectionReason
from civ_arena.game.sim.state import (
    BUILDINGS,
    FORT_BONUS,
    TECHS,
    TERRAIN,
    UNIT_TECH_REQ,
    UNIT_TYPES,
    SimState,
    hex_dist,
    neighbors,
    parse_key,
    tile_key,
    tiles_within,
)


def _m(
    kind: str,
    etype: str,
    eid: str,
    attr: str,
    before: Any,
    after: Any,
    origin: str = "command",
) -> MutationRecord:
    return MutationRecord(
        kind=kind, entity_type=etype, entity_id=eid, attr=attr,
        before=before, after=after, origin=origin,
    )


# --------------------------------------------------------------------------
# pathfinding


def path_cost(state: SimState, start: tuple[int, int], dest: tuple[int, int],
              mover_owner: int) -> int | None:
    """Dijkstra enter-cost over passable tiles; enemy-occupied tiles block.

    Returns None when unreachable.
    """
    dest_key = tile_key(*dest)
    if dest_key not in state.tiles:
        return None

    def enemy_blocked(q: int, r: int) -> bool:
        return bool(state.enemy_units_at(q, r, mover_owner))

    dist = {tile_key(*start): 0}
    heap: list[tuple[int, int, int]] = [(0, start[0], start[1])]
    while heap:
        d, q, r = heapq.heappop(heap)
        if (q, r) == dest:
            return d
        if d > dist.get(tile_key(q, r), 1 << 30):
            continue
        for nq, nr in neighbors(q, r):
            nkey = tile_key(nq, nr)
            tile = state.tiles.get(nkey)
            if tile is None:
                continue
            step = TERRAIN[tile["terrain"]]["move"]
            if step == 0 or enemy_blocked(nq, nr):
                continue
            nd = d + step
            if nd < dist.get(nkey, 1 << 30):
                dist[nkey] = nd
                heapq.heappush(heap, (nd, nq, nr))
    return None


def _damage(rng: Any, atk_str: int, def_str: int) -> int:
    """All-integer combat roll; a single rng draw per strike."""
    ratio = max(25, min(400, (atk_str * 100) // max(1, def_str)))
    return (24 * ratio * (90 + rng.randint(0, 20))) // 10000


# --------------------------------------------------------------------------
# validation helpers


def _parse_coord(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, str):
        return None
    try:
        q, r = parse_key(value)
    except ValueError:
        return None
    # Canonical spellings only: "-03,+01" and " 1,2" parse to the same ints
    # as "-3,1" and "1,2" but would give the same semantic action a distinct
    # args_digest/dedupe key and defeat enumeration completeness.
    if tile_key(q, r) != value:
        return None
    return q, r


def _tech_ok(state: SimState, player_id: int, tech: str) -> bool:
    return tech in state.player(player_id)["researched"]


def _item_valid(city: dict[str, Any], item: str) -> bool:
    return item in BUILDINGS or item in UNIT_TYPES


def available_research(state: SimState, player_id: int) -> list[dict[str, Any]]:
    player = state.player(player_id)
    out = []
    for tech, spec in sorted(TECHS.items()):
        if tech in player["researched"]:
            continue
        if all(p in player["researched"] for p in spec["prereq"]):
            out.append({"tech_id": tech, "cost": spec["cost"],
                        "prereq": list(spec["prereq"])})
    return out


def available_production(state: SimState, player_id: int, city_id: str) -> list[dict[str, Any]]:
    city = state.city(city_id)
    if city is None or city["owner"] != player_id:
        return []
    out = []
    for item in sorted(UNIT_TYPES):
        tech = UNIT_TECH_REQ.get(item)
        if tech is not None and not _tech_ok(state, player_id, tech):
            continue
        out.append({"item_id": item, "cost": UNIT_TYPES[item]["cost"], "kind": "unit"})
    for item in sorted(BUILDINGS):
        if item in city["buildings"]:
            continue
        out.append({"item_id": item, "cost": BUILDINGS[item]["cost"], "kind": "building"})
    return out


def purchase_cost(state: SimState, item: str) -> int | None:
    if item in BUILDINGS:
        return BUILDINGS[item]["cost"] * 2  # gold premium over production cost
    if item in UNIT_TYPES:
        return UNIT_TYPES[item]["cost"] * 2
    return None


# --------------------------------------------------------------------------
# enumeration (M15a) — the planner-facing legal-action surface


def _numeric(entity_id: str) -> int:
    return int(entity_id[1:])


def reachable_dests(state: SimState, unit: dict[str, Any]) -> dict[str, int]:
    """Every tile key the unit may enter this turn, mapped to its path cost.

    Same expansion rule as ``path_cost`` (impassable terrain and enemy-
    occupied tiles never relax), bounded by the unit's remaining movement.
    Includes the unit's own tile at cost 0 — a zero-cost move is legal.
    """
    budget = unit["movement"]
    owner = unit["owner"]
    start = (unit["q"], unit["r"])
    dist: dict[str, int] = {tile_key(*start): 0}
    heap: list[tuple[int, int, int]] = [(0, start[0], start[1])]
    while heap:
        d, q, r = heapq.heappop(heap)
        if d > dist.get(tile_key(q, r), 1 << 30):
            continue
        for nq, nr in neighbors(q, r):
            nkey = tile_key(nq, nr)
            tile = state.tiles.get(nkey)
            if tile is None:
                continue
            step = TERRAIN[tile["terrain"]]["move"]
            if step == 0 or state.enemy_units_at(nq, nr, owner):
                continue
            nd = d + step
            if nd <= budget and nd < dist.get(nkey, 1 << 30):
                dist[nkey] = nd
                heapq.heappush(heap, (nd, nq, nr))
    # An enemy co-located on the start tile (it entered before this unit was
    # purchased there) makes the zero-cost self-move OCCUPIED per check_action.
    if state.enemy_units_at(*start, owner):
        del dist[tile_key(*start)]
    return dist


def _can_found(state: SimState, player_id: int, unit: dict[str, Any]) -> bool:
    tile = state.tile(unit["q"], unit["r"])
    if tile is None or tile["owner"] != -1:
        return False
    if TERRAIN[tile["terrain"]]["move"] == 0:
        return False
    for city in state.cities.values():
        if hex_dist((city["q"], city["r"]), (unit["q"], unit["r"])) <= 2:
            return False
    return not state.enemy_units_at(unit["q"], unit["r"], player_id)


def legal_actions(state: SimState, player_id: int) -> list[tuple[str, dict[str, Any]]]:
    """Enumerate every action ``check_action`` would accept for this player.

    Soundness (everything enumerated passes ``check_action``) and
    completeness (everything that passes is enumerated) are test-pinned.
    ``found_city`` is enumerated without the optional ``name`` arg — a named
    variant is the same decision, not a different one. Order is
    deterministic: a fixed tool ladder, numeric entity ids, sorted dests.
    """
    out: list[tuple[str, dict[str, Any]]] = []
    my_units = sorted(
        (u for u in state.units.values() if u["owner"] == player_id),
        key=lambda u: _numeric(u["unit_id"]))
    foreign_units = sorted(
        (u for u in state.units.values() if u["owner"] != player_id),
        key=lambda u: _numeric(u["unit_id"]))
    my_cities = sorted(
        (c for c in state.cities.values() if c["owner"] == player_id),
        key=lambda c: _numeric(c["city_id"]))
    player = state.player(player_id)

    for unit in my_units:
        if unit["movement"] > 0:
            for dest in sorted(reachable_dests(state, unit)):
                out.append(("move_unit", {"unit_id": unit["unit_id"], "dest": dest}))
    for unit in my_units:
        if unit["movement"] <= 0:
            continue
        if unit["strength"] <= 0 and unit["ranged_strength"] <= 0:
            continue
        ranged = unit["ranged_strength"] > 0
        for target in foreign_units:
            dist = hex_dist((unit["q"], unit["r"]), (target["q"], target["r"]))
            if (dist <= 2) if ranged else (dist == 1):
                out.append(("attack", {"unit_id": unit["unit_id"],
                                       "target_id": target["unit_id"]}))
    for unit in my_units:
        if not unit["fortified"]:
            out.append(("fortify", {"unit_id": unit["unit_id"]}))
    for unit in my_units:
        if unit["type"] == "SETTLER" and _can_found(state, player_id, unit):
            out.append(("found_city", {"unit_id": unit["unit_id"]}))
    for entry in available_research(state, player_id):
        if entry["tech_id"] != player["researching"]:
            out.append(("set_research", {"tech_id": entry["tech_id"]}))
    for city in my_cities:
        for entry in available_production(state, player_id, city["city_id"]):
            out.append(("set_city_production",
                        {"city_id": city["city_id"], "item_id": entry["item_id"]}))
    for city in my_cities:
        for entry in available_production(state, player_id, city["city_id"]):
            cost = purchase_cost(state, entry["item_id"])
            if cost is not None and player["gold"] >= cost:
                out.append(("purchase",
                            {"city_id": city["city_id"], "item_id": entry["item_id"]}))
    return out


# --------------------------------------------------------------------------
# check


def check_action(
    state: SimState, player_id: int, tool: str, args: dict[str, Any]
) -> RejectionReason | None:
    if tool == "move_unit":
        unit = state.unit(args.get("unit_id", ""))
        if unit is None or not isinstance(args.get("unit_id"), str):
            return RejectionReason.UNKNOWN_ENTITY
        if unit["owner"] != player_id:
            return RejectionReason.NOT_YOUR_UNIT
        dest = _parse_coord(args.get("dest"))
        if dest is None or state.tile(*dest) is None:
            return RejectionReason.ARGS_INVALID
        if TERRAIN[state.tile(*dest)["terrain"]]["move"] == 0:
            return RejectionReason.ILLEGAL_DEST
        if state.enemy_units_at(*dest, player_id):
            return RejectionReason.OCCUPIED
        if unit["movement"] <= 0:
            return RejectionReason.NO_MOVEMENT
        cost = path_cost(state, (unit["q"], unit["r"]), dest, player_id)
        if cost is None:
            return RejectionReason.ILLEGAL_MOVE
        if cost > unit["movement"]:
            return RejectionReason.NO_MOVEMENT
        return None

    if tool == "attack":
        unit = state.unit(args.get("unit_id", ""))
        target = state.unit(args.get("target_id", ""))
        if unit is None or target is None:
            return RejectionReason.UNKNOWN_ENTITY
        if unit["owner"] != player_id:
            return RejectionReason.NOT_YOUR_UNIT
        if target["owner"] == player_id:
            return RejectionReason.ARGS_INVALID
        if unit["movement"] <= 0:
            return RejectionReason.NO_MOVEMENT
        if unit["strength"] <= 0 and unit["ranged_strength"] <= 0:
            return RejectionReason.CANNOT_ATTACK
        dist = hex_dist((unit["q"], unit["r"]), (target["q"], target["r"]))
        if unit["ranged_strength"] > 0:
            if dist > 2:
                return RejectionReason.OUT_OF_RANGE
        elif dist != 1:
            return RejectionReason.OUT_OF_RANGE
        return None

    if tool == "fortify":
        unit = state.unit(args.get("unit_id", ""))
        if unit is None:
            return RejectionReason.UNKNOWN_ENTITY
        if unit["owner"] != player_id:
            return RejectionReason.NOT_YOUR_UNIT
        if unit["fortified"]:
            return RejectionReason.ALREADY
        return None

    if tool == "found_city":
        unit = state.unit(args.get("unit_id", ""))
        if unit is None:
            return RejectionReason.UNKNOWN_ENTITY
        if unit["owner"] != player_id:
            return RejectionReason.NOT_YOUR_UNIT
        if unit["type"] != "SETTLER":
            return RejectionReason.ARGS_INVALID
        tile = state.tile(unit["q"], unit["r"])
        if tile is None or tile["owner"] != -1:
            return RejectionReason.OCCUPIED
        if TERRAIN[tile["terrain"]]["move"] == 0:
            return RejectionReason.ILLEGAL_DEST
        for city in state.cities.values():
            if hex_dist((city["q"], city["r"]), (unit["q"], unit["r"])) <= 2:
                return RejectionReason.OCCUPIED
        if state.enemy_units_at(unit["q"], unit["r"], player_id):
            return RejectionReason.OCCUPIED
        name = args.get("name")
        if name is not None and (not isinstance(name, str) or not (1 <= len(name.strip()) <= 23)):
            return RejectionReason.ARGS_INVALID
        if name is not None and any(c["name"] == name.strip() for c in state.cities.values()):
            return RejectionReason.ALREADY
        return None

    if tool == "set_research":
        tech = args.get("tech_id")
        if not isinstance(tech, str) or tech not in TECHS:
            return RejectionReason.UNKNOWN_ENTITY
        player = state.player(player_id)
        if tech in player["researched"] or player["researching"] == tech:
            return RejectionReason.ALREADY
        if not all(p in player["researched"] for p in TECHS[tech]["prereq"]):
            return RejectionReason.PREREQ_UNMET
        return None

    if tool == "set_city_production":
        city = state.city(args.get("city_id", ""))
        if city is None or not isinstance(args.get("city_id"), str):
            return RejectionReason.UNKNOWN_ENTITY
        if city["owner"] != player_id:
            return RejectionReason.NOT_YOUR_CITY
        item = args.get("item_id")
        if not isinstance(item, str) or not _item_valid(city, item):
            return RejectionReason.UNKNOWN_ENTITY
        if item in BUILDINGS and item in city["buildings"]:
            return RejectionReason.ALREADY
        tech = UNIT_TECH_REQ.get(item)
        if tech is not None and not _tech_ok(state, player_id, tech):
            return RejectionReason.PREREQ_UNMET
        return None

    if tool == "purchase":
        city = state.city(args.get("city_id", ""))
        if city is None or not isinstance(args.get("city_id"), str):
            return RejectionReason.UNKNOWN_ENTITY
        if city["owner"] != player_id:
            return RejectionReason.NOT_YOUR_CITY
        item = args.get("item_id")
        if not isinstance(item, str) or not _item_valid(city, item):
            return RejectionReason.UNKNOWN_ENTITY
        if item in BUILDINGS and item in city["buildings"]:
            return RejectionReason.ALREADY
        tech = UNIT_TECH_REQ.get(item)
        if tech is not None and not _tech_ok(state, player_id, tech):
            return RejectionReason.PREREQ_UNMET
        cost = purchase_cost(state, item)
        if cost is None or state.player(player_id)["gold"] < cost:
            return RejectionReason.INSUFFICIENT_GOLD
        return None

    return RejectionReason.TOOL_UNKNOWN


# --------------------------------------------------------------------------
# apply


def apply_action(
    state: SimState, player_id: int, tool: str, args: dict[str, Any]
) -> list[MutationRecord]:
    """Execute a checked action and describe it as MutationRecords."""
    muts: list[MutationRecord] = []

    if tool == "move_unit":
        unit = state.unit(args["unit_id"])
        dest = _parse_coord(args["dest"])
        assert unit is not None and dest is not None
        cost = path_cost(state, (unit["q"], unit["r"]), dest, player_id)
        assert cost is not None and cost <= unit["movement"]
        before_pos = (unit["q"], unit["r"])
        old_movement = unit["movement"]
        unit["movement"] = old_movement - cost
        unit["q"], unit["r"] = dest
        muts.append(_m("unit.moved", "unit", unit["unit_id"], "coord",
                       tile_key(*before_pos), tile_key(*dest)))
        muts.append(_m("unit.movement", "unit", unit["unit_id"], "movement",
                       old_movement, unit["movement"]))
        if unit["fortified"]:
            unit["fortified"] = False
            muts.append(_m("unit.unfortified", "unit", unit["unit_id"], "fortified",
                           True, False))
        muts.extend(_reveal_after_sight_change(state, player_id))
        return muts

    if tool == "attack":
        unit = state.unit(args["unit_id"])
        target = state.unit(args["target_id"])
        assert unit is not None and target is not None
        old_movement = unit["movement"]
        unit["movement"] = 0
        muts.append(_m("unit.attacked", "unit", unit["unit_id"], "movement",
                       old_movement, 0))
        if unit["fortified"]:
            unit["fortified"] = False
            muts.append(_m("unit.unfortified", "unit", unit["unit_id"], "fortified",
                           True, False))

        def_def = target["strength"] + (FORT_BONUS if target["fortified"] else 0)
        if unit["ranged_strength"] > 0:
            dmg = _damage(state.rng, unit["ranged_strength"], max(1, def_def))
            muts.extend(_apply_damage(state, target, dmg))
        else:
            dmg = _damage(state.rng, unit["strength"], max(1, def_def))
            killed = _apply_damage(state, target, dmg)
            muts.extend(killed)
            if not any(m.kind == "unit.killed" and m.entity_id == target["unit_id"]
                       for m in killed):
                back = _damage(state.rng, def_def, max(1, unit["strength"]))
                muts.extend(_apply_damage(state, unit, back))
        return muts

    if tool == "fortify":
        unit = state.unit(args["unit_id"])
        assert unit is not None
        unit["fortified"] = True
        muts.append(_m("unit.fortified", "unit", unit["unit_id"], "fortified", False, True))
        return muts

    if tool == "found_city":
        unit = state.unit(args["unit_id"])
        assert unit is not None
        q, r = unit["q"], unit["r"]
        city_id = f"c{state.doc['next_city_id']}"
        state.doc["next_city_id"] += 1
        name = args.get("name") or f"{state.player(player_id)['civ_name']} {city_id.upper()}"
        city = {
            "city_id": city_id, "owner": player_id, "name": name.strip(),
            "q": q, "r": r, "population": 1, "hp": 100, "food_bucket": 0,
            "production_bucket": 0, "production_queue": [], "buildings": [],
            "border_radius": 2,
        }
        state.cities[city_id] = city
        muts.append(_m("city.founded", "city", city_id, "created", 0, 1))
        for tq, tr in tiles_within((q, r), city["border_radius"]):
            key = tile_key(tq, tr)
            tile = state.tiles.get(key)
            if tile is not None and tile["owner"] == -1:
                tile["owner"] = player_id
                tile["city"] = city_id
                muts.append(_m("tile.claimed", "tile", key, "owner", -1, player_id))
        state.remove_unit(unit["unit_id"])
        muts.append(_m("unit.consumed", "unit", unit["unit_id"], "alive", 1, 0))
        muts.extend(_reveal_after_sight_change(state, player_id))
        return muts

    if tool == "set_research":
        player = state.player(player_id)
        old = player["researching"]
        player["researching"] = args["tech_id"]
        muts.append(_m("player.research_set", "player", str(player_id), "researching",
                       old, args["tech_id"]))
        return muts

    if tool == "set_city_production":
        city = state.city(args["city_id"])
        assert city is not None
        old = list(city["production_queue"])
        city["production_queue"] = [args["item_id"]]
        muts.append(_m("city.production_set", "city", city["city_id"],
                       "production_queue", old, list(city["production_queue"])))
        return muts

    if tool == "purchase":
        city = state.city(args["city_id"])
        assert city is not None
        item = args["item_id"]
        player = state.player(player_id)
        cost = purchase_cost(state, item)
        assert cost is not None
        old_gold = player["gold"]
        player["gold"] = old_gold - cost
        muts.append(_m("player.gold", "player", str(player_id), "gold", old_gold,
                       player["gold"]))
        if item in BUILDINGS:
            old_buildings = list(city["buildings"])
            city["buildings"] = sorted([*old_buildings, item])
            muts.append(_m("city.building_added", "city", city["city_id"], "buildings",
                           old_buildings, list(city["buildings"])))
        else:
            _, unit = state.spawn_unit(player_id, item, city["q"], city["r"])
            muts.append(_m("unit.purchased", "unit", unit["unit_id"], "created", 0, 1))
        return muts

    raise ValueError(f"apply_action on unknown tool {tool!r}")


def _apply_damage(state: SimState, unit: dict[str, Any], dmg: int) -> list[MutationRecord]:
    old_hp = unit["hp"]
    unit["hp"] = max(0, old_hp - dmg)
    muts = [_m("unit.damaged", "unit", unit["unit_id"], "hp", old_hp, unit["hp"])]
    if unit["hp"] == 0:
        state.remove_unit(unit["unit_id"])
        muts.append(_m("unit.killed", "unit", unit["unit_id"], "alive", 1, 0))
    return muts


def _reveal_after_sight_change(state: SimState, player_id: int) -> list[MutationRecord]:
    before = list(state.doc["revealed"][str(player_id)])
    after = state.extend_revealed(player_id, state.sight_tiles(player_id))
    if before == after:
        return []
    return [_m("player.revealed", "revealed", str(player_id), "keys", before, after)]
