"""Sim rules: legality, effects, combat determinism, founding, production."""

from __future__ import annotations

import pytest

from civ_arena.canonical import state_hash
from civ_arena.game.adapter import RejectionReason
from civ_arena.game.sim.layouts import STARTS, duel_start
from civ_arena.game.sim.rules import apply_action, check_action
from civ_arena.game.sim.state import (
    MAP_RADIUS,
    TERRAIN,
    SimState,
    hex_dist,
    map_tiles,
    tile_key,
)


def fresh(seed: int = 1) -> SimState:
    return SimState.from_doc(duel_start(seed))


def units_of(state: SimState, owner: int) -> list[dict]:
    return sorted(
        (u for u in state.units.values() if u["owner"] == owner),
        key=lambda u: int(u["unit_id"][1:]),
    )


def teleport(state: SimState, unit_id: str, q: int, r: int, terrain: str | None = None) -> None:
    unit = state.unit(unit_id)
    unit["q"], unit["r"] = q, r
    if terrain is not None:
        state.tiles[tile_key(q, r)]["terrain"] = terrain


def free_neighbor(state: SimState, q: int, r: int) -> tuple[int, int]:
    from civ_arena.game.sim.state import neighbors

    for nq, nr in sorted(neighbors(q, r)):
        if not state.units_at(nq, nr) and TERRAIN[state.tile(nq, nr)["terrain"]]["move"] > 0:
            return nq, nr
    raise AssertionError("no free neighbor")


# ---------------------------------------------------------------- start layout


def test_duel_start_shape():
    state = fresh(1)
    assert len(state.tiles) == len(map_tiles(MAP_RADIUS))
    assert len(units_of(state, 0)) == 5
    assert len(units_of(state, 1)) == 5
    assert hex_dist(STARTS[0], STARTS[1]) == 6
    types = sorted(u["type"] for u in units_of(state, 0))
    assert types == ["SCOUT", "SETTLER", "SETTLER", "WARRIOR", "WARRIOR"]
    for pid in (0, 1):
        assert state.revealed_keys(pid), "players start having revealed tiles"
    # starts + ring are passable land
    for start in STARTS.values():
        tile = state.tile(*start)
        assert TERRAIN[tile["terrain"]]["move"] > 0


def test_duel_start_deterministic():
    a, b = fresh(9), fresh(9)
    assert state_hash(a.to_doc()) == state_hash(b.to_doc())
    assert state_hash(fresh(9).to_doc()) != state_hash(fresh(10).to_doc())


def test_doc_roundtrip_preserves_hash():
    state = fresh(3)
    before = state_hash(state.to_doc())
    again = SimState.from_doc(state.to_doc())
    assert state_hash(again.to_doc()) == before


# ---------------------------------------------------------------- movement


def test_move_updates_position_and_movement():
    state = fresh(1)
    warrior = units_of(state, 0)[2]  # first WARRIOR
    dest = free_neighbor(state, warrior["q"], warrior["r"])
    cost = TERRAIN[state.tile(*dest)["terrain"]]["move"]
    reason = check_action(state, 0, "move_unit",
                           {"unit_id": warrior["unit_id"], "dest": tile_key(*dest)})
    assert reason is None
    muts = apply_action(state, 0, "move_unit",
                        {"unit_id": warrior["unit_id"], "dest": tile_key(*dest)})
    assert (warrior["q"], warrior["r"]) == dest
    assert warrior["movement"] == warrior["max_movement"] - cost
    kinds = [m.kind for m in muts]
    assert "unit.moved" in kinds and "unit.movement" in kinds


def test_move_onto_enemy_occupied_rejected():
    state = fresh(1)
    warrior = units_of(state, 0)[2]
    dest = free_neighbor(state, warrior["q"], warrior["r"])
    enemy = units_of(state, 1)[2]
    teleport(state, enemy["unit_id"], *dest)
    reason = check_action(state, 0, "move_unit",
                           {"unit_id": warrior["unit_id"], "dest": tile_key(*dest)})
    assert reason == RejectionReason.OCCUPIED


def test_move_without_movement_rejected():
    state = fresh(1)
    warrior = units_of(state, 0)[2]
    warrior["movement"] = 0
    dest = free_neighbor(state, warrior["q"], warrior["r"])
    reason = check_action(state, 0, "move_unit",
                           {"unit_id": warrior["unit_id"], "dest": tile_key(*dest)})
    assert reason == RejectionReason.NO_MOVEMENT


def test_move_onto_mountain_rejected():
    state = fresh(1)
    warrior = units_of(state, 0)[2]
    dest = free_neighbor(state, warrior["q"], warrior["r"])
    state.tiles[tile_key(*dest)]["terrain"] = "MOUNTAIN"
    reason = check_action(state, 0, "move_unit",
                           {"unit_id": warrior["unit_id"], "dest": tile_key(*dest)})
    assert reason == RejectionReason.ILLEGAL_DEST


def test_move_foreign_unit_rejected():
    state = fresh(1)
    enemy_settler = units_of(state, 1)[0]
    dest = free_neighbor(state, enemy_settler["q"], enemy_settler["r"])
    reason = check_action(state, 0, "move_unit",
                          {"unit_id": enemy_settler["unit_id"], "dest": tile_key(*dest)})
    assert reason == RejectionReason.NOT_YOUR_UNIT


def test_move_reveals_new_tiles():
    state = fresh(1)
    scout = units_of(state, 0)[4]
    teleport(state, scout["unit_id"], 4, 0, terrain="PLAINS")
    state.tiles[tile_key(5, 0)]["terrain"] = "PLAINS"
    before = state.revealed_keys(0)
    muts = apply_action(state, 0, "move_unit",
                        {"unit_id": scout["unit_id"], "dest": tile_key(5, 0)})
    after = state.revealed_keys(0)
    assert after > before
    assert any(m.kind == "player.revealed" for m in muts)


# ---------------------------------------------------------------- combat


def _setup_skirmish(state: SimState, fortify_defender: bool = False) -> tuple[dict, dict]:
    attacker = units_of(state, 0)[2]  # WARRIOR
    defender = units_of(state, 1)[2]  # WARRIOR
    spot = free_neighbor(state, attacker["q"], attacker["r"])
    teleport(state, defender["unit_id"], *spot, terrain="PLAINS")
    if fortify_defender:
        defender["fortified"] = True
    return attacker, defender


def test_melee_mutual_damage_and_determinism():
    state = fresh(1)
    attacker, defender = _setup_skirmish(state)
    assert check_action(state, 0, "attack",
                        {"unit_id": attacker["unit_id"], "target_id": defender["unit_id"]}) is None
    muts = apply_action(state, 0, "attack",
                        {"unit_id": attacker["unit_id"], "target_id": defender["unit_id"]})
    assert attacker["hp"] < 100 and defender["hp"] < 100
    assert attacker["movement"] == 0
    assert any(m.kind == "unit.damaged" for m in muts)

    # determinism: identical snapshot -> identical outcome
    state2 = SimState.from_doc(duel_start(1))
    a2, d2 = _setup_skirmish(state2)
    apply_action(state2, 0, "attack",
                 {"unit_id": a2["unit_id"], "target_id": d2["unit_id"]})
    assert (a2["hp"], d2["hp"]) == (attacker["hp"], defender["hp"])
    assert state_hash(state2.to_doc()) == state_hash(state.to_doc())


def test_fortified_defender_takes_less_damage():
    results = {}
    for fortify in (False, True):
        state = fresh(1)
        attacker, defender = _setup_skirmish(state, fortify_defender=fortify)
        apply_action(state, 0, "attack",
                     {"unit_id": attacker["unit_id"], "target_id": defender["unit_id"]})
        results[fortify] = defender["hp"]
    # same rng draw sequence in both runs; strictly better defense => strictly less damage
    assert results[True] > results[False]


def test_ranged_attack_no_retaliation():
    state = fresh(1)
    state.player(0)["researched"] = ["POTTERY", "ARCHERY"]
    _, archer = state.spawn_unit(0, "ARCHER", *STARTS[0])
    enemy = units_of(state, 1)[2]
    spot = free_neighbor(state, archer["q"], archer["r"])
    teleport(state, enemy["unit_id"], *spot, terrain="PLAINS")
    reason = check_action(state, 0, "attack",
                          {"unit_id": archer["unit_id"], "target_id": enemy["unit_id"]})
    assert reason is None
    apply_action(state, 0, "attack",
                 {"unit_id": archer["unit_id"], "target_id": enemy["unit_id"]})
    assert archer["hp"] == 100
    assert enemy["hp"] < 100


def test_out_of_range_melee_rejected():
    state = fresh(1)
    attacker = units_of(state, 0)[2]
    enemy = units_of(state, 1)[2]
    # enemy stays at its own start: distance 6
    reason = check_action(state, 0, "attack",
                          {"unit_id": attacker["unit_id"], "target_id": enemy["unit_id"]})
    assert reason == RejectionReason.OUT_OF_RANGE


def test_kill_removes_unit():
    state = fresh(1)
    attacker, defender = _setup_skirmish(state)
    defender["hp"] = 1
    muts = apply_action(state, 0, "attack",
                        {"unit_id": attacker["unit_id"], "target_id": defender["unit_id"]})
    assert defender["unit_id"] not in state.units
    assert any(m.kind == "unit.killed" for m in muts)


# ---------------------------------------------------------------- fortify


def test_fortify_then_already():
    state = fresh(1)
    warrior = units_of(state, 0)[2]
    assert check_action(state, 0, "fortify", {"unit_id": warrior["unit_id"]}) is None
    apply_action(state, 0, "fortify", {"unit_id": warrior["unit_id"]})
    assert warrior["fortified"] is True
    assert check_action(state, 0, "fortify", {"unit_id": warrior["unit_id"]}) == \
        RejectionReason.ALREADY


# ---------------------------------------------------------------- founding


def test_found_city_claims_territory_and_consumes_settler():
    state = fresh(1)
    settler = units_of(state, 0)[0]
    reason = check_action(state, 0, "found_city", {"unit_id": settler["unit_id"]})
    assert reason is None
    muts = apply_action(state, 0, "found_city", {"unit_id": settler["unit_id"], "name": "Nova"})
    assert settler["unit_id"] not in state.units
    city = next(c for c in state.cities.values() if c["name"] == "Nova")
    assert city["population"] == 1 and city["owner"] == 0
    claimed = [t for t in state.tiles.values() if t["city"] == city["city_id"]]
    assert len(claimed) > 10
    assert all(t["owner"] == 0 for t in claimed)
    assert any(m.kind == "tile.claimed" for m in muts)


def test_found_city_spacing_and_ownership_gates():
    state = fresh(1)
    first, second = units_of(state, 0)[0], units_of(state, 0)[1]
    apply_action(state, 0, "found_city", {"unit_id": first["unit_id"]})
    # second settler stands inside the first city's territory
    assert check_action(state, 0, "found_city", {"unit_id": second["unit_id"]}) == \
        RejectionReason.OCCUPIED
    # teleport far away (dist 4 from the first city, unowned, plains)
    teleport(state, second["unit_id"], 0, 0, terrain="PLAINS")
    assert check_action(state, 0, "found_city", {"unit_id": second["unit_id"]}) is None
    apply_action(state, 0, "found_city", {"unit_id": second["unit_id"]})
    assert len([c for c in state.cities.values() if c["owner"] == 0]) == 2


def test_found_city_on_mountain_rejected():
    state = fresh(1)
    settler = units_of(state, 0)[0]
    teleport(state, settler["unit_id"], 0, 0, terrain="MOUNTAIN")
    assert check_action(state, 0, "found_city", {"unit_id": settler["unit_id"]}) == \
        RejectionReason.ILLEGAL_DEST


# ---------------------------------------------------------------- research / production / purchase


def _one_city(state: SimState) -> str:
    settler = units_of(state, 0)[0]
    apply_action(state, 0, "found_city", {"unit_id": settler["unit_id"]})
    return next(iter(state.cities))


def test_set_research_prereq_and_already():
    state = fresh(1)
    assert check_action(state, 0, "set_research", {"tech_id": "ARCHERY"}) == \
        RejectionReason.PREREQ_UNMET
    assert check_action(state, 0, "set_research", {"tech_id": "POTTERY"}) is None
    apply_action(state, 0, "set_research", {"tech_id": "POTTERY"})
    assert state.player(0)["researching"] == "POTTERY"
    assert check_action(state, 0, "set_research", {"tech_id": "POTTERY"}) == \
        RejectionReason.ALREADY
    state.player(0)["researched"].append("POTTERY")
    state.player(0)["researching"] = ""
    assert check_action(state, 0, "set_research", {"tech_id": "ARCHERY"}) is None


def test_set_city_production_tech_gate_and_replace():
    state = fresh(1)
    city_id = _one_city(state)
    assert check_action(state, 0, "set_city_production",
                        {"city_id": city_id, "item_id": "ARCHER"}) == \
        RejectionReason.PREREQ_UNMET
    assert check_action(state, 0, "set_city_production",
                        {"city_id": city_id, "item_id": "WARRIOR"}) is None
    apply_action(state, 0, "set_city_production",
                 {"city_id": city_id, "item_id": "WARRIOR"})
    assert state.city(city_id)["production_queue"] == ["WARRIOR"]
    apply_action(state, 0, "set_city_production",
                 {"city_id": city_id, "item_id": "SCOUT"})
    assert state.city(city_id)["production_queue"] == ["SCOUT"]


def test_foreign_city_rejected():
    state = fresh(1)
    city_id = _one_city(state)
    assert check_action(state, 1, "set_city_production",
                        {"city_id": city_id, "item_id": "WARRIOR"}) == \
        RejectionReason.NOT_YOUR_CITY
    assert check_action(state, 1, "purchase",
                        {"city_id": city_id, "item_id": "WARRIOR"}) == \
        RejectionReason.NOT_YOUR_CITY


def test_purchase_gold_and_effects():
    state = fresh(1)
    city_id = _one_city(state)
    assert state.player(0)["gold"] == 100
    assert check_action(state, 0, "purchase",
                        {"city_id": city_id, "item_id": "MONUMENT"}) is None
    apply_action(state, 0, "purchase", {"city_id": city_id, "item_id": "MONUMENT"})
    assert state.player(0)["gold"] == 0
    assert "MONUMENT" in state.city(city_id)["buildings"]
    assert check_action(state, 0, "purchase",
                        {"city_id": city_id, "item_id": "GRANARY"}) == \
        RejectionReason.INSUFFICIENT_GOLD
    state.player(0)["gold"] = 500
    assert check_action(state, 0, "purchase",
                        {"city_id": city_id, "item_id": "MONUMENT"}) == \
        RejectionReason.ALREADY
    n_units = len(units_of(state, 0))
    apply_action(state, 0, "purchase", {"city_id": city_id, "item_id": "WARRIOR"})
    assert len(units_of(state, 0)) == n_units + 1


def test_unknown_tool_rejected():
    state = fresh(1)
    assert check_action(state, 0, "nuke_everything", {}) == RejectionReason.TOOL_UNKNOWN


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_no_none_values_in_state_doc(seed):
    from civ_arena.canonical import canonical

    state = fresh(seed)
    canonical(state.to_doc())  # must not raise
    doc = state.to_doc()

    def walk(node):
        if node is None:
            raise AssertionError("None inside sim state")
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(doc)
