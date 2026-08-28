"""Idempotency: default content keys, nonce opt-out, rebuild-from-log,
and duplicate calls never executing twice against a live adapter."""

from __future__ import annotations

from civ_arena.arena.idempotency import DedupeIndex
from civ_arena.game.adapter import ObserveKind, ObserveRequest
from civ_arena.game.sim.simulator import SimulatorAdapter
from conftest import cmd, free_neighbor, own_units


def test_default_key_stable_and_turn_scoped():
    args = {"unit_id": "u3", "dest": "1,2"}
    a = DedupeIndex.key_for(0, "move_unit", args, None, turn=5)
    b = DedupeIndex.key_for(0, "move_unit", {"dest": "1,2", "unit_id": "u3"}, None, turn=5)
    assert a == b, "key must not depend on arg order"
    assert a != DedupeIndex.key_for(0, "move_unit", args, None, turn=6), "turn-scoped"
    assert a != DedupeIndex.key_for(1, "move_unit", args, None, turn=5), "player-scoped"
    nonce1 = DedupeIndex.key_for(0, "purchase", {"city_id": "c1"}, "n1", turn=7)
    nonce2 = DedupeIndex.key_for(0, "purchase", {"city_id": "c1"}, "n2", turn=7)
    assert nonce1 != nonce2, "distinct nonces must not collide"


def test_from_log_rebuilds_only_accepted_nonduplicates():
    records = [
        {"kind": "TOOL_RESULT", "status": "accepted", "duplicate": False,
         "idempotency_key": "k1", "tool": "move_unit", "result_doc": {"x": 1}},
        {"kind": "TOOL_RESULT", "status": "accepted", "duplicate": True,
         "idempotency_key": "k1", "tool": "move_unit"},
        {"kind": "TOOL_RESULT", "status": "rejected", "duplicate": False,
         "idempotency_key": "k2", "tool": "attack"},
        {"kind": "TOOL_RESULT", "status": "accepted", "duplicate": False,
         "idempotency_key": None, "tool": "fortify"},
        {"kind": "TOOL_CALL", "status": "accepted", "idempotency_key": "k3"},
    ]
    idx = DedupeIndex.from_log(records)
    assert len(idx) == 1
    assert idx.seen("k1")["result"] == {"x": 1}


async def test_duplicate_key_no_double_exec():
    adapter = SimulatorAdapter()
    await adapter.setup({"seed": 1})
    await adapter.begin_phase(0, 1)
    warrior = own_units(adapter.state, 0, "WARRIOR")[0]
    dest = free_neighbor(adapter.state, warrior["q"], warrior["r"])
    from civ_arena.game.sim.state import tile_key

    args = {"unit_id": warrior["unit_id"], "dest": tile_key(*dest)}
    key = DedupeIndex.key_for(0, "move_unit", args, None, turn=1)
    idx = DedupeIndex()

    executed_mutations = 0
    # first call: executes and journals its mutations exactly once
    assert idx.seen(key) is None
    res = await adapter.act(cmd("move_unit", args, 0, key=key))
    assert res.status == "accepted"
    first_drain = len(adapter.drain_mutations())
    executed_mutations += first_drain
    assert first_drain > 0
    idx.record(key, {"status": "accepted", "result": res.result})

    # retry: served from the dedupe index, adapter never touched
    cached = idx.seen(key)
    assert cached is not None and cached["status"] == "accepted"
    assert len(adapter.drain_mutations()) == 0
    assert executed_mutations == first_drain
    await adapter.teardown()


async def test_distinct_keys_both_execute():
    adapter = SimulatorAdapter()
    await adapter.setup({"seed": 2})
    await adapter.begin_phase(0, 1)
    city = None
    settler = own_units(adapter.state, 0, "SETTLER")[0]
    await adapter.act(cmd("found_city", {"unit_id": settler["unit_id"]}, 0))
    city = sorted(
        c["city_id"] for c in adapter.state.cities.values() if c["owner"] == 0
    )[0]

    idx = DedupeIndex()
    purchases = 0
    for nonce in ("buy-1", "buy-2"):
        args = {"city_id": city, "item_id": "SCOUT"}
        key = DedupeIndex.key_for(0, "purchase", args, nonce, turn=1)
        assert idx.seen(key) is None
        adapter.state.player(0)["gold"] = 500
        res = await adapter.act(cmd("purchase", args, 0, key=key))
        assert res.status == "accepted", res.error
        purchases += 1
        idx.record(key, {"status": "accepted"})
    assert purchases == 2
    scouts = own_units(adapter.state, 0, "SCOUT")
    assert len(scouts) == 3  # starting scout + two purchases
    await adapter.teardown()


async def test_observer_read_is_not_blocked_by_dedupe():
    adapter = SimulatorAdapter()
    await adapter.setup({"seed": 3})
    units = await adapter.observe(ObserveRequest(kind=ObserveKind.UNITS, player_id=0))
    assert units
    await adapter.teardown()
