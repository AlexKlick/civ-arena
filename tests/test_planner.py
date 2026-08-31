"""M15a planner foundations: snapshot aliasing, legal-action enumeration,
value head.

The enumeration contract is pinned in both directions against
``check_action``: soundness (everything enumerated passes) over random
playout states, completeness (every sampled action that passes is
enumerated) over a biased random arg sampler. ``found_city`` is enumerated
without the optional ``name`` arg by declared equivalence.
"""

from __future__ import annotations

import random

from civ_arena.canonical import state_hash
from civ_arena.game.sim.engine import run_ambient
from civ_arena.game.sim.layouts import duel_start
from civ_arena.game.sim.rules import (
    apply_action,
    check_action,
    legal_actions,
    path_cost,
    reachable_dests,
)
from civ_arena.game.sim.state import (
    BUILDINGS,
    TECHS,
    UNIT_TYPES,
    SimState,
    parse_key,
    tiles_within,
)
from civ_arena.game.sim.value import (
    DEFAULT_WEIGHTS,
    scalarize,
    score_vector,
    value_of,
)

PREFER = ("found_city", "set_research", "set_city_production", "purchase")


def playout(seed: int, turns: int) -> SimState:
    """Random-legal playout driven by the enumerator itself, with ambient
    turns, biased toward state-enriching tools (cities, techs, buildings)."""
    state = SimState.from_doc(duel_start(seed))
    rng = random.Random(seed * 977 + 11)
    for _ in range(turns):
        for pid in (0, 1):
            run_ambient(state, pid)
            for _ in range(3):
                acts = legal_actions(state, pid)
                if not acts:
                    break
                preferred = [a for a in acts if a[0] in PREFER]
                pool = preferred if preferred and rng.random() < 0.5 else acts
                tool, args = pool[rng.randrange(len(pool))]
                apply_action(state, pid, tool, args)
        state.turn += 1
    return state


def as_key(tool: str, args: dict) -> tuple:
    return (tool, tuple(sorted(args.items())))


# --------------------------------------------------------------------------
# snapshot aliasing (the from_doc deep-copy regression)


def test_from_doc_does_not_alias_nested_state() -> None:
    doc = duel_start(3)
    before = state_hash(doc)
    state = SimState.from_doc(doc)
    unit = next(iter(state.units.values()))
    unit["q"] += 1
    unit["movement"] = 0
    state.player(0)["gold"] = 9999
    assert state_hash(doc) == before


def test_restore_twice_from_one_snapshot_is_independent() -> None:
    snap = SimState.from_doc(duel_start(4)).to_doc()
    baseline = state_hash(snap)

    first = SimState.from_doc(snap)
    for unit in list(first.units.values()):
        unit["hp"] = 1
    first.player(1)["gold"] = 0
    # the snapshot must be untouched, and a second restore must see it so
    assert state_hash(snap) == baseline
    second = SimState.from_doc(snap)
    assert state_hash(second.to_doc()) == baseline


# --------------------------------------------------------------------------
# reachable_dests vs path_cost parity


def test_reachable_dests_matches_path_cost_exactly() -> None:
    state = playout(11, 6)
    for unit in state.units.values():
        dests = reachable_dests(state, unit)
        start = (unit["q"], unit["r"])
        for key, cost in dests.items():
            assert cost <= unit["movement"] or key == f"{start[0]},{start[1]}"
            assert path_cost(state, start, parse_key(key), unit["owner"]) == cost
        # nothing within budget is missing
        for key in state.tiles:
            if key in dests:
                continue
            cost = path_cost(state, start, parse_key(key), unit["owner"])
            assert cost is None or cost > unit["movement"]


# --------------------------------------------------------------------------
# legal_actions: soundness, determinism, completeness


def test_legal_actions_sound_over_playouts() -> None:
    for seed, turns in ((1, 0), (7, 4), (23, 10), (41, 16)):
        state = playout(seed, turns)
        for pid in (0, 1):
            for tool, args in legal_actions(state, pid):
                assert check_action(state, pid, tool, args) is None, (
                    f"seed={seed} pid={pid} enumerated illegal {tool} {args}")


def test_legal_actions_deterministic_and_roundtrip_stable() -> None:
    state = playout(9, 8)
    first = legal_actions(state, 0)
    assert first == legal_actions(state, 0)
    rebuilt = SimState.from_doc(state.to_doc())
    assert legal_actions(rebuilt, 0) == first


def sample_candidates(rng: random.Random, state: SimState, n: int):
    unit_ids = sorted(state.units) or ["u0"]
    city_ids = sorted(state.cities) or ["c0"]
    tile_keys = sorted(state.tiles)
    techs = sorted(TECHS)
    items = sorted(UNIT_TYPES) + sorted(BUILDINGS)
    tools = ["move_unit", "attack", "fortify", "found_city",
             "set_research", "set_city_production", "purchase"]
    for _ in range(n):
        tool = rng.choice(tools)
        if tool == "move_unit":
            uid = rng.choice(unit_ids)
            unit = state.units.get(uid)
            if unit is not None and rng.random() < 0.7:
                near = [f"{q},{r}" for q, r in tiles_within((unit["q"], unit["r"]), 3)
                        if f"{q},{r}" in state.tiles]
                dest = rng.choice(near)
            else:
                dest = rng.choice(tile_keys)
            yield tool, {"unit_id": uid, "dest": dest}
        elif tool == "attack":
            yield tool, {"unit_id": rng.choice(unit_ids),
                         "target_id": rng.choice(unit_ids)}
        elif tool in ("fortify", "found_city"):
            yield tool, {"unit_id": rng.choice(unit_ids)}
        elif tool == "set_research":
            yield tool, {"tech_id": rng.choice(techs)}
        else:
            yield tool, {"city_id": rng.choice(city_ids), "item_id": rng.choice(items)}


def test_legal_actions_complete_against_sampler() -> None:
    rng = random.Random(1234)
    hits = 0
    for seed, turns in ((5, 2), (13, 8), (29, 14)):
        state = playout(seed, turns)
        for pid in (0, 1):
            enumerated = {as_key(t, a) for t, a in legal_actions(state, pid)}
            for tool, args in sample_candidates(rng, state, 600):
                if check_action(state, pid, tool, args) is None:
                    hits += 1
                    assert as_key(tool, args) in enumerated, (
                        f"seed={seed} pid={pid} legal-but-missing {tool} {args}")
    assert hits > 50  # the sampler must actually exercise the property


# --------------------------------------------------------------------------
# value head


def test_score_vector_and_value_symmetric_at_start() -> None:
    state = SimState.from_doc(duel_start(6))
    v0, v1 = score_vector(state, 0), score_vector(state, 1)
    assert v0 == v1
    assert v0["cities"] == 0 and v0["gold"] == 100 and v0["units"] > 0
    assert value_of(state, 0) == 0 and value_of(state, 1) == 0
    assert isinstance(scalarize(v0), int)


def test_value_moves_with_the_game() -> None:
    state = playout(17, 12)
    # the differential is antisymmetric for two players
    assert value_of(state, 0) == -value_of(state, 1)
    # founding a city for p0 strictly raises p0's value
    before = value_of(state, 0)
    fake_city = {"city_id": "c99", "owner": 0, "name": "T", "q": 0, "r": 0,
                 "population": 1, "hp": 100, "food_bucket": 0,
                 "production_bucket": 0, "production_queue": [], "buildings": [],
                 "border_radius": 2}
    state.cities["c99"] = fake_city
    after = value_of(state, 0)
    assert after - before == DEFAULT_WEIGHTS["cities"] + DEFAULT_WEIGHTS["population"]
