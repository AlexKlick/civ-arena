"""M16c: typed uncertainty over beliefs — derived at read time, consumed.

The no-expiry invariant holds (a stale ghost still materializes in the
determinized doc, carrying its decayed confidence), while the STRATEGY
stops responding to it: rush needs a recently-sighted unit target (a
known foreign city is permanent and never decays), and defend counts
only probably-real military as a threat. All confidences are integers
0..10000 — canonical JSON never sees a float.
"""

from __future__ import annotations

import asyncio

from civ_arena.game.sim.layouts import duel_start
from civ_arena.game.sim.rules import check_action, legal_actions
from civ_arena.game.sim.state import SimState, tile_key
from civ_arena.planner.belief import PlannerBelief, build_state_doc
from civ_arena.planner.options import OPTIONS
from civ_arena.planner.uncertainty import (
    CONF_MAX,
    RUSH_CONF_MIN,
    STALENESS_PENALTY_PER_TURN,
    THREAT_CONF_MIN,
    confident_foreign_units,
    staleness_confidence,
)
from test_action_dag import FakeFacade, feed_via


def seeded_belief_with_adjacent_enemy(seen_turn: int):
    """A belief where a foreign WARRIOR sits adjacent to our own at
    ``seen_turn``; the current turn is passed by the caller."""
    state = SimState.from_doc(duel_start(21))
    own = next(u for u in state.units.values()
               if u["owner"] == 0 and u["strength"] > 0)
    _, enemy = state.spawn_unit(1, "WARRIOR", own["q"] + 1, own["r"])
    facade = FakeFacade(state, 0)
    belief = PlannerBelief(0)

    async def go():
        await feed_via(facade, belief)

    asyncio.run(go())
    belief.foreign_units[enemy["unit_id"]]["last_seen_turn"] = seen_turn
    return belief, enemy


def test_staleness_math_is_clamped_integer() -> None:
    assert staleness_confidence(5, 5) == CONF_MAX
    assert staleness_confidence(4, 5) == CONF_MAX - STALENESS_PENALTY_PER_TURN
    assert staleness_confidence(1, 5) == 0          # clamped at zero
    assert staleness_confidence(9, 5) == CONF_MAX   # future-seen: clamped fresh


def test_doc_stamps_derived_confidence() -> None:
    belief, enemy = seeded_belief_with_adjacent_enemy(seen_turn=8)
    belief.turn = 10  # two turns stale
    doc = build_state_doc(belief, seed=5)
    assert doc["units"][enemy["unit_id"]]["confidence"] == 5000
    assert all(u.get("confidence", CONF_MAX) == CONF_MAX
               for uid, u in doc["units"].items()
               if u["owner"] == 0)  # own entities: full self-knowledge


def test_rush_gates_on_stale_ghosts_but_cities_are_permanent() -> None:
    # fresh sighting: rush sees the unit target
    belief, enemy = seeded_belief_with_adjacent_enemy(seen_turn=10)
    belief.turn = 10
    bstate = SimState.from_doc(build_state_doc(belief, seed=5))
    assert OPTIONS["rush"].initiation(bstate, 0) is True

    # five turns later: the ghost still materializes (no-expiry) at
    # confidence 0, but rush no longer initiates on it
    belief.turn = 15
    bstate = SimState.from_doc(build_state_doc(belief, seed=5))
    assert bstate.units[enemy["unit_id"]]["confidence"] == 0
    assert OPTIONS["rush"].initiation(bstate, 0) is False

    # a known foreign CITY keeps rush alive regardless of staleness
    bstate.cities["c9"] = {
        "city_id": "c9", "owner": 1, "name": "X", "q": 3, "r": -1,
        "population": 1, "hp": 100, "food_bucket": 0, "production_bucket": 0,
        "production_queue": [], "buildings": [], "border_radius": 2}
    assert OPTIONS["rush"].initiation(bstate, 0) is True


def test_defend_ignores_stale_ghosts() -> None:
    belief, enemy = seeded_belief_with_adjacent_enemy(seen_turn=10)
    belief.turn = 10
    bstate = SimState.from_doc(build_state_doc(belief, seed=5))
    assert OPTIONS["defend"].initiation(bstate, 0) is True  # adjacent, fresh

    belief.turn = 14  # confidence 0: same ghost, no threat response
    bstate = SimState.from_doc(build_state_doc(belief, seed=5))
    assert bstate.units[enemy["unit_id"]]["confidence"] == 0
    assert OPTIONS["defend"].initiation(bstate, 0) is False
    # the gating is the confidence floor, not absence
    assert len(confident_foreign_units(bstate, 0, THREAT_CONF_MIN)) == 0
    assert len(confident_foreign_units(bstate, 0, 0)) == 1


def test_confidence_keys_never_break_the_rules_layer() -> None:
    """The stamped key is additive: enumeration, legality, and execution
    over a confidence-carrying doc behave exactly as before."""
    belief, _enemy = seeded_belief_with_adjacent_enemy(seen_turn=10)
    belief.turn = 12
    bstate = SimState.from_doc(build_state_doc(belief, seed=5))
    for tool, args in legal_actions(bstate, 0):
        assert check_action(bstate, 0, tool, args) is None
    from civ_arena.game.sim.rules import apply_action

    moves = [a for t, a in legal_actions(bstate, 0)
             if t == "move_unit" and a["dest"] != tile_key(0, 0)][:1]
    if moves:
        apply_action(bstate, 0, "move_unit", moves[0])  # must not raise
    from civ_arena.canonical import state_hash

    state_hash(bstate.to_doc())  # still canonical with the int key
    _ = RUSH_CONF_MIN
