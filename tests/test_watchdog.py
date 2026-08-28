"""Watchdog under adversarial injection — every test asserts on BOTH sides:

the chaos mutation actually landed AND the referee flagged it. A watchdog
that fires on nothing cannot pass these.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from civ_arena.arena.events import EventLog
from civ_arena.arena.referee import MatchAborted, Referee, RefereeConfig
from civ_arena.arena.telemetry import TelemetryRegistry
from civ_arena.arena.visibility import VisibilityPolicy
from civ_arena.game.sim.chaos import ChaosDirector, ChaosEvent, MutationSpec
from civ_arena.game.sim.simulator import SimulatorAdapter
from civ_arena.session.tools import SessionCtx
from conftest import own_units


async def harness(
    tmp_path: Path,
    seed: int = 5,
    chaos: list[ChaosEvent] | None = None,
    mode: str = "flag_and_continue",
    violation_limit: int = 99,
    **setup_kw,
):
    adapter = SimulatorAdapter()
    director = ChaosDirector(chaos or [])
    await adapter.setup({"seed": seed, "chaos_director": director, **setup_kw})
    log = EventLog(tmp_path / "events.jsonl")
    telemetry = TelemetryRegistry()
    referee = Referee(adapter, VisibilityPolicy(), log, telemetry, "m-wd", "g1",
                      RefereeConfig(watchdog_mode=mode, violation_limit=violation_limit))
    lease = referee.grant_lease(0, "roman", 1)
    await referee.begin_turn(0, "roman", 1)
    ctx = SessionCtx(referee=referee, player_id=0, agent_id="roman",
                     lease=lease, turn=1)
    return adapter, referee, log, ctx, director


def violation_docs(log: EventLog) -> list[dict]:
    return [r for r in log.records() if r["kind"] == "VIOLATION"]


async def test_ambient_declared_allowed_clean_run(tmp_path):
    """The watchdog is silent when nothing happened (non-vacuity anchor)."""
    adapter, referee, log, ctx, _director = await harness(tmp_path, chaos=[])
    settler = own_units(adapter.state, 0, "SETTLER")[0]
    doc = await referee.execute(ctx, "found_city", {"unit_id": settler["unit_id"]})
    assert doc["status"] == "accepted"
    assert referee.violation_count() == 0
    assert violation_docs(log) == []
    assert (await referee.end_turn(ctx))["status"] == "accepted"


async def test_detects_uncommanded_move(tmp_path):
    adapter, referee, log, ctx, director = await harness(
        tmp_path, chaos=[ChaosEvent(MutationSpec.MOVE_UNCOMMANDED_UNIT)])
    # fortify = a command that moves nothing; any coordinate change is chaos
    units_before = {u["unit_id"]: (u["q"], u["r"])
                    for u in adapter.state.units.values() if u["owner"] == 0}
    warrior = own_units(adapter.state, 0, "WARRIOR")[0]
    doc = await referee.execute(ctx, "fortify", {"unit_id": warrior["unit_id"]})
    assert doc["status"] == "accepted"

    # side A: the mutation actually landed
    assert director.fired == [MutationSpec.MOVE_UNCOMMANDED_UNIT]
    units_after = {u["unit_id"]: (u["q"], u["r"])
                   for u in adapter.state.units.values() if u["owner"] == 0}
    moved = [uid for uid, pos in units_after.items() if units_before.get(uid) != pos]
    assert moved, "chaos must have actually moved an uncommanded unit"

    # side B: the referee flagged it
    assert referee.violation_count() >= 1
    docs = violation_docs(log)
    assert docs and docs[0]["watchdog"]["kind"] == "UNAUTHORIZED_ACTION"


async def test_detects_production_flip(tmp_path):
    # offset=2: fire on the 3rd act, after the city exists and has a queue
    adapter, referee, log, ctx, director = await harness(
        tmp_path, chaos=[ChaosEvent(MutationSpec.FLIP_PRODUCTION, offset=2)])
    settler = own_units(adapter.state, 0, "SETTLER")[0]
    await referee.execute(ctx, "found_city", {"unit_id": settler["unit_id"]})
    city_id = sorted(c["city_id"] for c in adapter.state.cities.values()
                     if c["owner"] == 0)[0]
    await referee.execute(ctx, "set_city_production",
                          {"city_id": city_id, "item_id": "WARRIOR"})
    assert adapter.state.city(city_id)["production_queue"] == ["WARRIOR"]

    warrior = own_units(adapter.state, 0, "WARRIOR")[0]
    await referee.execute(ctx, "fortify", {"unit_id": warrior["unit_id"]})

    # side A: the flip landed
    assert director.fired == [MutationSpec.FLIP_PRODUCTION]
    assert adapter.state.city(city_id)["production_queue"] == ["SCOUT"]
    # side B: flagged
    assert referee.violation_count() >= 1


async def test_detects_research_change(tmp_path):
    adapter, referee, log, ctx, director = await harness(
        tmp_path, chaos=[ChaosEvent(MutationSpec.CHANGE_RESEARCH)])
    # chaos fires INSIDE the first act — immediately after the set_research
    # command resolves
    await referee.execute(ctx, "set_research", {"tech_id": "POTTERY"})

    assert director.fired == [MutationSpec.CHANGE_RESEARCH]
    assert adapter.state.player(0)["researching"] == "WRITING"  # side A
    assert referee.violation_count() >= 1  # side B
    assert violation_docs(log)


async def test_detects_free_unit_and_stolen_gold(tmp_path):
    adapter, referee, log, ctx, director = await harness(
        tmp_path, chaos=[ChaosEvent(MutationSpec.STEAL_GOLD),
                         ChaosEvent(MutationSpec.SPAWN_FREE_UNIT)])
    gold_before = adapter.state.player(0)["gold"]
    n_units = len(own_units(adapter.state, 0))
    settler = own_units(adapter.state, 0, "SETTLER")[0]
    await referee.execute(ctx, "found_city", {"unit_id": settler["unit_id"]})  # act 1
    warrior = own_units(adapter.state, 0, "WARRIOR")[0]
    await referee.execute(ctx, "fortify", {"unit_id": warrior["unit_id"]})  # act 2

    assert director.fired == [MutationSpec.STEAL_GOLD, MutationSpec.SPAWN_FREE_UNIT]
    assert adapter.state.player(0)["gold"] == gold_before - 50  # side A
    assert len(own_units(adapter.state, 0)) == n_units  # settler consumed, free one spawned
    assert referee.violation_count() >= 2  # side B


async def test_ambient_like_trap_flagged_as_deviation(tmp_path):
    """Ambient classification is by DECLARATION, not shape: an undeclared
    ambient-shaped mutation (with a lying origin tag) must still be flagged —
    as a DEVIATION when the manifest covers the attribute."""
    # offset=2: skip turn 1's two acts (found_city, set_city_production) so
    # the trap fires inside turn 2, where the manifest covers the attribute
    adapter, referee, log, ctx, director = await harness(
        tmp_path, chaos=[ChaosEvent(MutationSpec.AMBIENT_LIKE_TRAP, offset=2)])
    # turn 1: found a city and leave a production queue in place
    settler = own_units(adapter.state, 0, "SETTLER")[0]
    await referee.execute(ctx, "found_city", {"unit_id": settler["unit_id"]})
    city_id = sorted(c["city_id"] for c in adapter.state.cities.values()
                     if c["owner"] == 0)[0]
    await referee.execute(ctx, "set_city_production",
                          {"city_id": city_id, "item_id": "WARRIOR"})
    await referee.end_turn(ctx)
    # run player 1's phase directly so the engine advances to turn 2
    await adapter.begin_phase(1, 1)
    await adapter.end_phase(1, 1)

    # turn 2: the phase manifest now covers city.production_bucket; the trap
    # fires mid-phase with a lying ambient tag
    lease2 = referee.grant_lease(0, "roman", 2)
    await referee.begin_turn(0, "roman", 2)
    ctx2 = SessionCtx(referee=referee, player_id=0, agent_id="roman",
                      lease=lease2, turn=2)
    bucket_before = adapter.state.city(city_id)["production_bucket"]
    manifest_covers = any(
        m["entity_type"] == "city" and m["attr"] == "production_bucket"
        for r in log.records() if r["kind"] == "AMBIENT" and r["turn"] == 2
        for m in r["manifest"]
    )
    assert manifest_covers, "scenario must have production_bucket declared ambient"

    warrior = own_units(adapter.state, 0, "WARRIOR")[0]
    await referee.execute(ctx2, "fortify", {"unit_id": warrior["unit_id"]})

    assert director.fired == [MutationSpec.AMBIENT_LIKE_TRAP]
    assert adapter.state.city(city_id)["production_bucket"] == bucket_before + 5
    docs = violation_docs(log)
    assert docs, "undeclared ambient-shaped mutation must be flagged"
    assert docs[-1]["watchdog"]["kind"] == "AMBIENT_DEVIATION"


async def test_broken_freeze_detected(tmp_path):
    """stock_ai runs when the freeze fails — the watchdog must catch it."""
    adapter, referee, log, ctx, _director = await harness(
        tmp_path, broken_freeze=True, stock_ai=True)
    # begin_turn already swept; the stock-AI mutations are violations
    assert referee.violation_count() >= 1
    docs = violation_docs(log)
    assert docs and docs[0]["watchdog"]["kind"] == "UNAUTHORIZED_ACTION"
    assert docs[0]["watchdog"]["mutations"], "the caught mutations are recorded"


async def test_rollback_restores_state(tmp_path):
    adapter, referee, log, ctx, _director = await harness(
        tmp_path, chaos=[ChaosEvent(MutationSpec.MOVE_UNCOMMANDED_UNIT)],
        mode="rollback")
    snapshot_hash = adapter.state_hash()
    settler = own_units(adapter.state, 0, "SETTLER")[0]
    doc = await referee.execute(ctx, "found_city", {"unit_id": settler["unit_id"]})
    # command + chaos both happened, then the lease snapshot was restored
    assert doc["status"] == "rejected" and doc.get("rolled_back") is True
    assert adapter.state_hash() == snapshot_hash
    assert adapter.state.cities == {}, "the founded city must be gone"
    rolled = [r for r in log.records()
              if r["kind"] == "TOOL_RESULT" and r.get("rolled_back")]
    assert rolled, "a follow-up rejection record marks the rollback for replay"


async def test_violation_limit_aborts_match(tmp_path):
    adapter, referee, _log, ctx, _director = await harness(
        tmp_path,
        chaos=[ChaosEvent(MutationSpec.STEAL_GOLD), ChaosEvent(MutationSpec.STEAL_GOLD)],
        violation_limit=1,
    )
    settler = own_units(adapter.state, 0, "SETTLER")[0]
    await referee.execute(ctx, "found_city", {"unit_id": settler["unit_id"]})  # 1st: flagged
    warrior = own_units(adapter.state, 0, "WARRIOR")[0]
    with pytest.raises(MatchAborted):
        await referee.execute(ctx, "fortify", {"unit_id": warrior["unit_id"]})  # 2nd: aborts


async def test_lying_origin_tag_still_caught(tmp_path):
    """The allowlist is the referee's ledger, not the mutation's origin tag."""
    from civ_arena.arena.watchdog import diff
    from civ_arena.game.adapter import MutationRecord

    allowed = [MutationRecord("unit.refreshed", "unit", "u3", "movement", 1, 2,
                              origin="ambient")]
    lying = [MutationRecord("unit.moved", "unit", "u9", "coord", "0,0", "1,0",
                            origin="ambient")]  # claims ambient, never requested
    violations = diff(allowed + lying, allowed)
    assert violations and violations[0].kind == "UNAUTHORIZED_ACTION"
