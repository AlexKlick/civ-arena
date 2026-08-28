"""Hostile-agent probes against the REAL referee: blocked, logged, match survives."""

from __future__ import annotations

from pathlib import Path

from civ_arena.arena.events import EventLog
from civ_arena.arena.referee import Referee, RefereeConfig
from civ_arena.arena.telemetry import TelemetryRegistry
from civ_arena.arena.turn_lease import TurnLease
from civ_arena.arena.visibility import Scope, VisibilityPolicy
from civ_arena.game.adapter import ObserveKind
from civ_arena.game.sim.rules import apply_action
from civ_arena.game.sim.simulator import SimulatorAdapter
from civ_arena.game.sim.state import tile_key
from civ_arena.session.player_session import PlayerSession
from civ_arena.session.tools import SessionCtx
from conftest import free_neighbor, own_units


async def hostile_setup(tmp_path: Path, seed: int = 4):
    adapter = SimulatorAdapter()
    await adapter.setup({"seed": seed})
    log = EventLog(tmp_path / "events.jsonl")
    telemetry = TelemetryRegistry()
    referee = Referee(adapter, VisibilityPolicy(), log, telemetry,
                      "m-hostile", "g1", RefereeConfig())
    session = PlayerSession(referee, player_id=0, agent_id="roman")
    lease = referee.grant_lease(0, "roman", 1)
    await referee.begin_turn(0, "roman", 1)
    ctx = SessionCtx(referee=referee, player_id=0, agent_id="roman",
                     lease=lease, turn=1)
    return adapter, log, referee, session, ctx, lease


async def test_command_enemy_unit_rejected(tmp_path):
    adapter, log, referee, _session, ctx, lease = await hostile_setup(tmp_path)
    enemy = own_units(adapter.state, 1, "WARRIOR")[0]
    pre_pos = (enemy["q"], enemy["r"])
    dest = free_neighbor(adapter.state, enemy["q"], enemy["r"])

    doc = await referee.execute(ctx, "move_unit",
                                {"unit_id": enemy["unit_id"], "dest": tile_key(*dest)})
    assert doc["status"] == "rejected"
    assert doc["rejection"] == "not_your_unit"
    # the enemy unit did NOT move
    assert (enemy["q"], enemy["r"]) == pre_pos
    # and the rejection is on the record
    results = [r for r in log.records() if r["kind"] == "TOOL_RESULT"]
    assert any(r.get("rejection") == "not_your_unit" for r in results)
    # match survives: end_turn works
    assert (await referee.end_turn(ctx))["status"] == "accepted"
    assert lease.released


async def test_lease_forgery_is_unauthorized(tmp_path):
    adapter, log, referee, _session, ctx, _lease = await hostile_setup(tmp_path)
    forged = TurnLease(
        lease_id="forged", match_id="m-hostile", game_instance_id="g1",
        player_id=1, agent_id="korea", turn=1, granted_seq=0,
    )
    forged_ctx = SessionCtx(referee=referee, player_id=0, agent_id="roman",
                            lease=forged, turn=1)
    doc = await referee.execute(forged_ctx, "fortify", {"unit_id": "u3"})
    assert doc["status"] == "rejected"
    unauth = [r for r in log.records() if r["kind"] == "UNAUTHORIZED_TOOL_CALL"]
    assert unauth, "lease forgery must be logged as unauthorized"


async def test_referee_scope_escalation_refused(tmp_path):
    adapter, log, referee, _session, ctx, _lease = await hostile_setup(tmp_path)
    doc = await referee.observe(ctx, ObserveKind.UNITS, scope=Scope.REFEREE)
    assert isinstance(doc, dict) and "error" in doc
    unauth = [r for r in log.records() if r["kind"] == "UNAUTHORIZED_TOOL_CALL"]
    assert any("referee scope" in (r.get("detail") or "") for r in unauth)


async def test_acts_outside_own_phase_rejected(tmp_path):
    adapter, log, referee, _session, ctx, _lease = await hostile_setup(tmp_path)
    # end the turn, then try to act with the (now expired) lease
    await referee.end_turn(ctx)
    stale = SessionCtx(referee=referee, player_id=0, agent_id="roman",
                       lease=ctx.lease, turn=1)
    warrior = own_units(adapter.state, 0, "WARRIOR")[0]
    doc = await referee.execute(stale, "fortify", {"unit_id": warrior["unit_id"]})
    assert doc["status"] == "rejected"
    assert doc["rejection"] in ("lease_expired", "lease_foreign")


async def test_foreign_city_production_read_returns_nothing(tmp_path):
    adapter, _log, referee, _session, ctx, _lease = await hostile_setup(tmp_path)
    # player 1 founds a city (raw rules call — test scaffolding, not an act)
    settler = own_units(adapter.state, 1, "SETTLER")[0]
    apply_action(adapter.state, 1, "found_city", {"unit_id": settler["unit_id"]})
    enemy_city = sorted(
        c["city_id"] for c in adapter.state.cities.values() if c["owner"] == 1
    )[0]
    options = await referee.observe(
        ctx, ObserveKind.AVAILABLE_PRODUCTION, subject_id=enemy_city)
    assert options == [], "foreign city production options must be empty"
