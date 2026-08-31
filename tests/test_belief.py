"""M15b belief forward model: the planner's world is projections only.

The harness projects omniscient sim state through the REAL VisibilityPolicy
(the same layer the referee uses), feeds the projections to PlannerBelief,
and pins: hidden entities stay absent, foreign fields are determinized pure
functions of allowlisted values (never of ground truth), over-informative
inputs are refused loudly, and the built doc is canonical, deterministic,
and rollable with the M15a machinery.
"""

from __future__ import annotations

import copy
import random

from civ_arena.arena.visibility import VisibilityPolicy
from civ_arena.canonical import state_hash
from civ_arena.game.sim.engine import run_ambient
from civ_arena.game.sim.layouts import STARTS, duel_start
from civ_arena.game.sim.rules import apply_action, check_action, legal_actions
from civ_arena.game.sim.state import UNIT_TYPES, SimState
from civ_arena.game.sim.visibility import ground_truth
from civ_arena.planner.belief import PlannerBelief, build_state_doc


def _sorted_units(state: SimState) -> list[dict]:
    return sorted((copy.deepcopy(u) for u in state.units.values()),
                  key=lambda u: int(u["unit_id"][1:]))


def _sorted_cities(state: SimState) -> list[dict]:
    return sorted((copy.deepcopy(c) for c in state.cities.values()),
                  key=lambda c: int(c["city_id"][1:]))


def feed(belief: PlannerBelief, state: SimState) -> None:
    """Project the omniscient state exactly as the referee would and feed
    every observation kind to the belief."""
    pid = belief.player_id
    vis = ground_truth(state, pid)
    policy = VisibilityPolicy()
    omni_overview = {
        "turn": state.turn,
        "phase_player": state.phase_player,
        "players": copy.deepcopy(state.players),
        "cities": copy.deepcopy(state.cities),
        "units": copy.deepcopy(state.units),
        "tiles": copy.deepcopy(state.tiles),
    }
    belief.observe_overview(policy.project(
        omni_overview, "overview", pid, vis.observable, vis.remembered))
    belief.observe_units(policy.project(
        _sorted_units(state), "units", pid, vis.observable, vis.remembered),
        state.turn)
    belief.observe_cities(policy.project(
        _sorted_cities(state), "cities", pid, vis.observable, vis.remembered),
        state.turn)
    belief.observe_map(policy.project(
        {"turn": state.turn, "tiles": copy.deepcopy(state.tiles)},
        "visible_map", pid, vis.observable, vis.remembered))


def test_unobserved_rival_gets_the_prior_roster_not_truth() -> None:
    state = SimState.from_doc(duel_start(21))
    belief = PlannerBelief(0)
    feed(belief, state)
    doc = build_state_doc(belief, seed=5)
    # at the duel start the rival is out of sight: its force is the PUBLIC
    # roster prior, stacked at the layout start — game setup, not truth
    rival = [u for u in doc["units"].values() if u["owner"] == 1]
    assert sorted(u["type"] for u in rival) == [
        "SCOUT", "SETTLER", "SETTLER", "WARRIOR", "WARRIOR"]
    assert all((u["q"], u["r"]) == STARTS[1] for u in rival)
    assert doc["cities"] == {}
    assert doc["players"]["1"]["gold"] == 100
    assert doc["players"]["1"]["researched"] == []
    # own units are reconstructed exactly
    own_true = {u["unit_id"]: u for u in state.units.values() if u["owner"] == 0}
    own_doc = {uid: u for uid, u in doc["units"].items() if u["owner"] == 0}
    assert set(own_doc) == set(own_true)
    for uid, u in own_doc.items():
        t = own_true[uid]
        assert (u["q"], u["r"], u["hp"], u["movement"]) == (
            t["q"], t["r"], t["hp"], t["movement"])
    # the prior tracks the SEED-independent setup, never live truth: move
    # every true rival unit and rebuild — the prior does not follow
    for u in list(state.units.values()):
        if u["owner"] == 1:
            u["q"], u["r"] = 0, 5
    feed(belief, state)
    doc2 = build_state_doc(belief, seed=5)
    rival2 = [u for u in doc2["units"].values() if u["owner"] == 1]
    assert all((u["q"], u["r"]) == STARTS[1] for u in rival2)


def test_foreign_fields_are_determinized_not_copied() -> None:
    state = SimState.from_doc(duel_start(21))
    own = next(u for u in state.units.values() if u["owner"] == 0)
    # an enemy warrior adjacent, damaged to hp 63 (bucket 2), spent, fortified
    _, enemy = state.spawn_unit(1, "WARRIOR", own["q"] + 1, own["r"])
    enemy["hp"] = 63
    enemy["movement"] = 0
    enemy["fortified"] = True
    belief = PlannerBelief(0)
    feed(belief, state)
    doc = build_state_doc(belief, seed=5)
    got = doc["units"][enemy["unit_id"]]
    assert got["hp"] == 62            # 25*2+12: a function of the bucket, not 63
    assert got["movement"] == UNIT_TYPES["WARRIOR"]["mv"]  # not the true 0
    assert got["fortified"] is False  # never projected, never copied
    assert got["owner"] == 1


def test_overinformative_observations_are_refused() -> None:
    belief = PlannerBelief(0)
    leaky_unit = {"unit_id": "u9", "owner_id": 1, "type": "WARRIOR",
                  "coord": "0,0", "hp_bucket": 2, "strength": 20,
                  "ranged_strength": 0, "hp": 55}
    try:
        belief.observe_units([leaky_unit], turn=3)
        raise AssertionError("leaky foreign unit accepted")
    except ValueError:
        pass
    leaky_city = {"city_id": "c9", "owner_id": 1, "name": "X", "coord": "0,0",
                  "hp": 100, "population": 2, "buildings": ["WALLS"]}
    try:
        belief.observe_cities([leaky_city], turn=3)
        raise AssertionError("leaky foreign city accepted")
    except ValueError:
        pass


def test_misshapen_own_entries_are_refused() -> None:
    """An own-labeled entry must match the own-projection shape exactly —
    a foreign-shaped doc relabeled as own cannot slip past the allowlist."""
    belief = PlannerBelief(0)
    relabeled = {"unit_id": "u9", "owner_id": 0, "type": "WARRIOR",
                 "coord": "0,0", "hp_bucket": 2, "strength": 20,
                 "ranged_strength": 0}
    try:
        belief.observe_units([relabeled], turn=3)
        raise AssertionError("misshapen own unit accepted")
    except ValueError:
        pass
    partial_city = {"city_id": "c9", "owner": 0, "coord": "0,0", "name": "X"}
    try:
        belief.observe_cities([partial_city], turn=3)
        raise AssertionError("misshapen own city accepted")
    except ValueError:
        pass


def _border_only_city_fixture() -> tuple[SimState, PlannerBelief]:
    """A rival city whose BORDER tile is observable while its center is not:
    the projection legally shows a c1-tagged tile with no c1 city record."""
    state = SimState.from_doc(duel_start(21))
    state.tiles["1,1"]["terrain"] = "GRASSLAND"
    _, settler = state.spawn_unit(1, "SETTLER", 1, 1)
    assert check_action(state, 1, "found_city",
                        {"unit_id": settler["unit_id"]}) is None
    apply_action(state, 1, "found_city", {"unit_id": settler["unit_id"]})
    belief = PlannerBelief(0)
    feed(belief, state)
    return state, belief


def test_city_id_on_tiles_bumps_next_city_id() -> None:
    """P1 regression: a freshly founded rollout city must never collide
    with a city id known only from observed border tiles — the engine
    would attribute the rival's tagged territory to it."""
    _, belief = _border_only_city_fixture()
    doc = build_state_doc(belief, seed=5)
    assert doc["tiles"]["-1,1"]["city"] == "c1"      # the observed border tile
    assert "c1" not in doc["cities"]                 # the hidden center
    assert doc["next_city_id"] == 2                  # no collision possible


def test_remembered_tiles_keep_last_live_ownership() -> None:
    """Ownership persists only from a live observation: once the border
    tile falls out of sight its last-seen owner is kept, and a tile never
    seen live carries none."""
    state, belief = _border_only_city_fixture()
    for u in list(state.units.values()):
        if u["owner"] == 0:
            u["q"], u["r"] = -5, 1
    feed(belief, state)  # the border tile is now remembered, terrain-only
    doc = build_state_doc(belief, seed=5)
    assert doc["tiles"]["-1,1"]["owner"] == 1
    assert doc["tiles"]["-1,1"]["city"] == "c1"
    assert doc["next_city_id"] == 2


def test_inferred_research_closes_over_prereqs() -> None:
    state = SimState.from_doc(duel_start(21))
    own = next(u for u in state.units.values() if u["owner"] == 0)
    state.spawn_unit(1, "ARCHER", own["q"] + 1, own["r"])
    belief = PlannerBelief(0)
    feed(belief, state)
    doc = build_state_doc(belief, seed=5)
    # ARCHER implies ARCHERY implies POTTERY
    assert doc["players"]["1"]["researched"] == ["ARCHERY", "POTTERY"]


def test_build_is_deterministic_and_seed_varies_unknowns() -> None:
    state = SimState.from_doc(duel_start(21))
    belief = PlannerBelief(0)
    feed(belief, state)
    a = build_state_doc(belief, seed=5)
    b = build_state_doc(belief, seed=5)
    c = build_state_doc(belief, seed=6)
    assert state_hash(a) == state_hash(b)
    assert state_hash(a) != state_hash(c)  # unknown terrain resampled
    # known terrain is NOT resampled: every remembered tile matches truth
    for key in belief.tiles:
        assert a["tiles"][key]["terrain"] == state.tiles[key]["terrain"]
        assert c["tiles"][key]["terrain"] == state.tiles[key]["terrain"]


def test_belief_state_rolls_out_with_m15a_machinery() -> None:
    """The determinized doc is a real SimState: enumerate, act, run ambient
    for a few turns without ever touching ground truth."""
    state = SimState.from_doc(duel_start(21))
    belief = PlannerBelief(0)
    feed(belief, state)
    rolled = SimState.from_doc(build_state_doc(belief, seed=9))
    rng = random.Random(99)
    acted = 0
    for _ in range(4):
        for pid in (0, 1):
            run_ambient(rolled, pid)
            acts = legal_actions(rolled, pid)
            for tool, args in rng.sample(acts, min(3, len(acts))):
                if check_action(rolled, pid, tool, args) is None:
                    apply_action(rolled, pid, tool, args)
                    acted += 1
        rolled.turn += 1
    assert acted > 0
    state_hash(rolled.to_doc())  # still canonical after the rollout


def test_contradicted_last_seen_units_do_not_reconstruct() -> None:
    """A foreign entry whose recorded tile is CURRENTLY observable but
    absent from the current observation is suppressed from reconstruction
    (it died or moved — materializing it would offer phantom attack
    targets); the store keeps it (no-expiry) and it reconstructs again
    once its tile falls back out of sight."""
    state = SimState.from_doc(duel_start(21))
    own = next(u for u in state.units.values() if u["owner"] == 0)
    _, enemy = state.spawn_unit(1, "WARRIOR", own["q"] + 1, own["r"])
    belief = PlannerBelief(0)
    feed(belief, state)
    assert enemy["unit_id"] in build_state_doc(belief, seed=5)["units"]

    state.remove_unit(enemy["unit_id"])  # dies out of our turn
    feed(belief, state)                  # tile still observable, unit absent
    doc = build_state_doc(belief, seed=5)
    assert enemy["unit_id"] not in doc["units"]        # contradicted: hidden
    assert enemy["unit_id"] in belief.foreign_units    # but never expired

    for u in list(state.units.values()):               # walk away: fog returns
        if u["owner"] == 0:
            u["q"], u["r"] = -5, 1
    feed(belief, state)
    doc = build_state_doc(belief, seed=5)
    assert enemy["unit_id"] in doc["units"]            # uncontradicted again


def test_belief_accumulates_and_wholesale_replaces_own() -> None:
    state = SimState.from_doc(duel_start(21))
    belief = PlannerBelief(0)
    feed(belief, state)
    n_before = len(belief.own_units)
    # an own unit dies (removed from state) — the next observation drops it
    dead = next(u for u in state.units.values() if u["owner"] == 0)
    state.remove_unit(dead["unit_id"])
    feed(belief, state)
    assert len(belief.own_units) == n_before - 1
    assert dead["unit_id"] not in belief.own_units


def test_engine_frame_recenters_into_the_sim_map() -> None:
    """M17c frame seam: the engine's axial coords are arbitrary (a duel
    start sits around (10,4) — hex distance 10 from the origin, far
    outside the radius-5 sim map). The belief RE-CENTERS its world on the
    first own anchor: own entities land inside the map, known terrain
    follows them, and the sim frame stays the planner's internal
    language. A frame already inside the map (the sim's own) keeps
    (0,0) — every M15/M16 pin is untouched."""
    state = SimState.from_doc(duel_start(21))
    belief = PlannerBelief(0)
    feed(belief, state)
    assert belief.ensure_origin() == (0, 0)  # the sim frame needs no shift
    before = build_state_doc(belief, seed=5)

    # translate the whole world into an engine-like frame
    OQ, OR = 34, -12
    shifted = SimState.from_doc(state.to_doc())
    for u in shifted.units.values():
        u["q"], u["r"] = u["q"] + OQ, u["r"] + OR
    for c in shifted.cities.values():
        c["q"], c["r"] = c["q"] + OQ, c["r"] + OR
    eb = PlannerBelief(0)
    feed(eb, shifted)
    anchor = eb.ensure_origin()
    assert anchor != (0, 0)
    if shifted.cities:
        first = min(shifted.cities.values(), key=lambda c: int(c["city_id"][1:]))
    else:
        first = min(shifted.units.values(), key=lambda u: int(u["unit_id"][1:]))
    assert anchor == (first["q"], first["r"])  # the shifted anchor itself
    after = build_state_doc(eb, seed=5)
    # the own roster is unchanged by the translation; every own unit and
    # city now stands on a sim-map tile
    assert set(after["units"]) == set(before["units"])
    for u in after["units"].values():
        assert f"{u['q']},{u['r']}" in after["tiles"]
    for c in after["cities"].values():
        assert f"{c['q']},{c['r']}" in after["tiles"]
    # known terrain followed the shift: a tile the projection showed has
    # the same terrain in both frames (compare by offset)
    for key in list(before["tiles"])[:10]:
        q, r = (int(x) for x in key.split(","))
        sim_key = f"{q},{r}"
        eng_key = f"{q + OQ},{r + OR}"
        if eng_key in eb.tiles and sim_key in after["tiles"]:
            assert (before["tiles"][sim_key]["terrain"]
                    == after["tiles"][sim_key]["terrain"])
