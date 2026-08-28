"""Regression tests for the Codex review findings (round 1).

Every test here pins a defect found by the adversarial review:
shallow-copy aliasing, facade ctx exposure, end-phase chaos escaping the
watchdog, forged leases, lease-wide rollback, abort-before-rollback,
rolled-back dedupe resurrection on resume, un-checkpointed chaos state,
unverified checkpoint content, remembered-tile city leaks, and log seq
failing open.
"""

from __future__ import annotations

import json

import pytest

from civ_arena.arena.checkpoints import CheckpointState
from civ_arena.arena.events import EventLog
from civ_arena.arena.idempotency import DedupeIndex
from civ_arena.arena.referee import MatchAborted
from civ_arena.game.sim.chaos import ChaosDirector, ChaosEvent, MutationSpec
from civ_arena.game.sim.visibility import ground_truth
from civ_arena.session.tools import SessionCtx, ToolFacade
from conftest import own_units
from test_watchdog import harness, violation_docs

# ---------------------------------------------------------------- P0-1 aliasing

async def test_observation_mutation_cannot_corrupt_state(tmp_path):
    """get_cities()/get_units() must return DEEP copies: an agent mutating a
    returned observation must not touch live game state."""
    adapter, referee, _log, ctx, _director = await harness(tmp_path)
    from civ_arena.game.adapter import ObserveKind

    settler = own_units(adapter.state, 0, "SETTLER")[0]
    await referee.execute(ctx, "found_city", {"unit_id": settler["unit_id"]})
    city_id = sorted(c["city_id"] for c in adapter.state.cities.values()
                     if c["owner"] == 0)[0]
    hash_before = adapter.state_hash()

    cities = await referee.observe(ctx, ObserveKind.CITIES)
    mine = next(c for c in cities if c["city_id"] == city_id)
    mine["buildings"].append("WALLS")           # mutate returned nested list
    mine["production_queue"].append("SETTLER")
    units = await referee.observe(ctx, ObserveKind.UNITS)
    for unit in units:
        unit["hp"] = 1
        unit["movement"] = 99

    assert adapter.state_hash() == hash_before, (
        "mutating returned observations changed live state (aliasing)"
    )
    assert adapter.state.city(city_id)["buildings"] == []
    assert referee.violation_count() == 0


# ---------------------------------------------------------------- P0-2 facade

async def test_facade_attribute_surface_has_no_referee(tmp_path):
    _adapter, _referee, _log, ctx, _director = await harness(tmp_path)
    facade = ToolFacade(ctx)
    assert not hasattr(facade, "_ctx") and not hasattr(facade, "referee")
    assert set(vars(facade)) == {"_bound"}
    with pytest.raises(AttributeError):
        facade._ctx  # noqa: B018


# ---------------------------------------------------------------- P0-3 end-phase

async def test_end_phase_chaos_flagged_not_carried_silently(tmp_path):
    """Chaos fired inside adapter.end_phase must be flagged on the ACTING
    agent and appear in the log — never silently reach the final state."""
    adapter, referee, log, ctx, director = await harness(
        tmp_path, chaos=[ChaosEvent(MutationSpec.STEAL_GOLD, hook="end_phase")])
    gold_before = adapter.state.player(0)["gold"]

    await referee.end_turn(ctx)

    assert director.fired == [MutationSpec.STEAL_GOLD]
    assert adapter.state.player(0)["gold"] == gold_before - 50  # it happened
    docs = violation_docs(log)
    assert docs, "end-phase chaos must not escape the watchdog"
    assert docs[-1]["agent_id"] == "roman", (
        "end-phase chaos must be blamed on the acting agent"
    )
    assert referee.violation_count("roman") >= 1


# ---------------------------------------------------------------- P1-4 leases

async def test_same_player_forged_lease_rejected(tmp_path):
    """A fabricated lease with matching player/agent/turn but foreign
    authority must be rejected: identity, not just structure."""
    adapter, referee, log, ctx, _director = await harness(tmp_path)
    from civ_arena.arena.turn_lease import TurnLease

    forged = TurnLease(
        lease_id="evil-authority", match_id="m-wd", game_instance_id="g1",
        player_id=0, agent_id="roman", turn=1, granted_seq=0,
    )
    forged_ctx = SessionCtx(referee=referee, player_id=0, agent_id="roman",
                            lease=forged, turn=1)
    doc = await referee.execute(forged_ctx, "fortify", {"unit_id": "u3"})
    assert doc["status"] == "rejected"
    unauth = [r for r in log.records() if r["kind"] == "UNAUTHORIZED_TOOL_CALL"]
    assert any("referee-issued" in (r.get("detail") or "") for r in unauth)
    # the REAL lease still works
    warrior = own_units(adapter.state, 0, "WARRIOR")[0]
    assert (await referee.execute(ctx, "fortify",
                                  {"unit_id": warrior["unit_id"]}))["status"] == "accepted"
    assert not ctx.lease.released


# ---------------------------------------------------------------- P1-5 rollback scope

async def test_rollback_preserves_earlier_accepted_command(tmp_path):
    """Per-command rollback: command A (accepted) must survive command B's
    violation; only B is undone."""
    adapter, referee, _log, ctx, _director = await harness(
        tmp_path, chaos=[ChaosEvent(MutationSpec.MOVE_UNCOMMANDED_UNIT, offset=1)],
        mode="rollback")
    settler = own_units(adapter.state, 0, "SETTLER")[0]
    doc_a = await referee.execute(ctx, "found_city", {"unit_id": settler["unit_id"]})
    assert doc_a["status"] == "accepted"
    hash_after_a = adapter.state_hash()

    warrior = own_units(adapter.state, 0, "WARRIOR")[0]
    doc_b = await referee.execute(ctx, "fortify", {"unit_id": warrior["unit_id"]})
    assert doc_b.get("rolled_back") is True
    assert adapter.state_hash() == hash_after_a, (
        "rollback must undo ONLY the violating command, not the whole lease"
    )
    assert adapter.state.cities, "the earlier accepted found_city must survive"


# ---------------------------------------------------------------- P1-6 abort+rollback

async def test_violation_limit_abort_still_rolls_back(tmp_path):
    adapter, referee, log, ctx, _director = await harness(
        tmp_path, chaos=[ChaosEvent(MutationSpec.STEAL_GOLD)],
        mode="rollback", violation_limit=0)
    settler = own_units(adapter.state, 0, "SETTLER")[0]
    pre = adapter.state_hash()
    with pytest.raises(MatchAborted):
        await referee.execute(ctx, "found_city", {"unit_id": settler["unit_id"]})
    # the command was rolled back even though the sweep aborted
    assert adapter.state_hash() == pre
    assert adapter.state.cities == {}
    rolled = [r for r in log.records()
              if r["kind"] == "TOOL_RESULT" and r.get("rolled_back")]
    assert rolled, "the rollback record must exist even on abort"


# ---------------------------------------------------------------- P1-7 dedupe

def test_from_log_skips_rolled_back_keys():
    records = [
        {"kind": "TOOL_RESULT", "status": "accepted", "duplicate": False,
         "idempotency_key": "k1", "tool": "found_city"},
        {"kind": "TOOL_RESULT", "status": "rejected", "rejection": "rollback",
         "rolled_back": True, "idempotency_key": "k1", "tool": "found_city"},
        {"kind": "TOOL_RESULT", "status": "accepted", "duplicate": False,
         "idempotency_key": "k2", "tool": "set_research"},
    ]
    idx = DedupeIndex.from_log(records)
    assert idx.seen("k1") is None, "rolled-back keys must stay forgettable"
    assert idx.seen("k2") is not None


# ---------------------------------------------------------------- P1-8 chaos state

def test_chaos_state_roundtrip():
    director = ChaosDirector([
        ChaosEvent(MutationSpec.FLIP_PRODUCTION, offset=2),
        ChaosEvent(MutationSpec.STEAL_GOLD, hook="end_phase"),
    ])
    # consume one hook so the offset mutates
    class _Adapter:
        state = None
    director.maybe_fire("act", _Adapter())
    restored = ChaosDirector([])
    restored.restore(director.state_doc())
    assert restored.state_doc() == director.state_doc()
    assert restored.armed == director.armed


# ---------------------------------------------------------------- P1-9 checkpoint

def test_tampered_checkpoint_rejected(tmp_path):
    path = tmp_path / "events.jsonl"
    EventLog(path).close()
    state = CheckpointState(
        match_id="m", game_instance_id_of_origin="g", turn=1, seq=0,
        sim_doc={"turn": 1, "gold_ok": True},
        rng_states={"sim": [3, [0], 0]},
        coordinator_state={"violations": 0},
        log_prefix_sha256="x",
    )
    doc = state.to_doc()
    assert doc["content_hash"]
    # tamper with the sim content after the fact
    doc["sim_doc"]["gold_ok"] = False
    with pytest.raises(ValueError, match="content hash mismatch"):
        CheckpointState.from_doc(doc)
    # missing hash (schema-1 style) also refused
    doc2 = state.to_doc()
    del doc2["content_hash"]
    with pytest.raises(ValueError, match="content_hash"):
        CheckpointState.from_doc(doc2)


# ---------------------------------------------------------------- P1-10 remembered tiles

async def test_remembered_tile_does_not_reveal_new_city(tmp_path):
    from civ_arena.arena.visibility import VisibilityPolicy
    from civ_arena.game.adapter import ObserveKind, ObserveRequest
    from civ_arena.game.sim.rules import apply_action

    adapter, _referee, _log, _ctx, _director = await harness(tmp_path)
    # player 0 remembers a far tile (scout explored it earlier via teleport)
    scout = own_units(adapter.state, 0, "SCOUT")[0]
    far = (0, 0)
    from civ_arena.game.sim.state import tile_key
    from conftest import teleport

    teleport(adapter.state, scout["unit_id"], *far, terrain="PLAINS")
    adapter.state.extend_revealed(0, {tile_key(*far)})
    vis_before = ground_truth(adapter.state, 0)
    assert tile_key(*far) in vis_before.remembered
    # move the scout away so the tile is remembered but NOT observable
    teleport(adapter.state, scout["unit_id"], 4, 0, terrain="PLAINS")
    vis = ground_truth(adapter.state, 0)
    assert tile_key(*far) in vis.remembered
    assert tile_key(*far) not in vis.observable

    # player 1 founds a city on that remembered tile
    enemy_settler = own_units(adapter.state, 1, "SETTLER")[0]
    teleport(adapter.state, enemy_settler["unit_id"], *far)
    apply_action(adapter.state, 1, "found_city", {"unit_id": enemy_settler["unit_id"]})

    omni = await adapter.observe(ObserveRequest(kind=ObserveKind.CITIES, player_id=0))
    proj = VisibilityPolicy().project(omni, "cities", 0, vis.observable, vis.remembered)
    ids = {c["city_id"] for c in proj}
    enemy_city = next(c["city_id"] for c in adapter.state.cities.values()
                      if c["owner"] == 1)
    assert enemy_city not in ids, (
        "a city founded on a remembered-but-unobserved tile must be invisible"
    )


# ---------------------------------------------------------------- P1-11 seq

def test_non_monotonic_seq_rejected_on_load(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    log.write("HEARTBEAT", match_id="m", game_instance_id="g", turn=1,
              phase_player_id=-1, player_id=None, agent_id=None,
              visibility_scope="referee")
    seq0 = log.records()[0]["seq"]
    log.close()
    # hand-tamper the only record's seq
    recs = [json.loads(line) for line in path.read_text().splitlines()]
    recs[0]["seq"] = 99
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    with pytest.raises(ValueError, match="seq broken"):
        EventLog(path)
    _ = seq0


def test_truncate_negative_rejected(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    with pytest.raises(ValueError, match="negative"):
        log.truncate_to(-1)
    log.close()


# ---------------------------------------------------------------- coordinator

async def test_match_id_mismatch_refuses_resume(tmp_path):
    from civ_arena.arena.coordinator import Arena
    from test_match_end_to_end import duel_spec

    spec10 = duel_spec("resume-guard", max_turns=10)
    arena = Arena(tmp_path / "run", spec10)
    await arena.run()
    from civ_arena.arena.checkpoints import CheckpointManager

    ckpt = CheckpointManager(tmp_path / "run" / "checkpoints", 5).latest()
    other = duel_spec("DIFFERENT-match", max_turns=30)
    arena2 = Arena(tmp_path / "run", other)
    with pytest.raises(ValueError, match="refusing to resume"):
        await arena2.run(resume_state=ckpt)


async def test_resume_with_pending_chaos_matches_clean(tmp_path):
    """Chaos still pending at the checkpoint must not re-fire or shift after
    resume (the director's queue+offsets are checkpointed)."""
    from civ_arena.arena.checkpoints import CheckpointManager
    from civ_arena.arena.coordinator import Arena
    from civ_arena.config import AgentSpec, ChaosSpec, MatchSpec

    def spec(match_id: str, max_turns: int) -> MatchSpec:
        return MatchSpec(
            match_id=match_id, seed=60606, max_turns=max_turns,
            adapter="simulator", watchdog_mode="flag_and_continue",
            violation_limit=50, checkpoint_every=5,
            agents=[AgentSpec("roman", 0, "expansionist", 11),
                    AgentSpec("korea", 1, "turtler", 22)],
            chaos=[ChaosSpec("move_uncommanded_unit", offset=80)],  # fires ~turn 30+
        )

    clean = Arena(tmp_path / "clean", spec("chaos-resume", 30))
    clean_summary = await clean.run()
    assert clean_summary["violations_total"] >= 1, "chaos must fire in the clean run"

    part = Arena(tmp_path / "part", spec("chaos-resume", 10))
    await part.run()
    ckpt = CheckpointManager(tmp_path / "part" / "checkpoints", 5).latest()
    resumed = Arena(tmp_path / "part", spec("chaos-resume", 30))
    resumed_summary = await resumed.run(resume_state=ckpt)
    assert resumed_summary["final_state_hash"] == clean_summary["final_state_hash"]
    assert resumed_summary["violations_total"] == clean_summary["violations_total"]


# ---------------------------------------------------- round-2 findings

async def test_two_identical_begin_phase_steals_both_rolled_back(tmp_path):
    """R2-1 (P0): after a begin-phase rollback, FRESH ledger bookkeeping must
    not let an identical second mutation hide behind the acknowledged first."""
    adapter, referee, log, ctx, _director = await harness(
        tmp_path,
        chaos=[ChaosEvent(MutationSpec.STEAL_GOLD, hook="begin_phase"),
               ChaosEvent(MutationSpec.STEAL_GOLD, hook="begin_phase")],
        mode="rollback", violation_limit=50)
    # begin_turn already ran inside harness: both steals fired across retry
    # attempts, each flagged, and the final attempt is violation-free (the
    # steals are undone), so the phase is fully usable
    docs = violation_docs(log)
    assert len(docs) >= 2, "each unauthorized begin-phase mutation must be flagged"
    settler = own_units(adapter.state, 0, "SETTLER")[0]
    doc = await referee.execute(ctx, "found_city", {"unit_id": settler["unit_id"]})
    assert doc["status"] == "accepted"


async def test_begin_phase_limit_abort_restores_pre_phase_state(tmp_path):
    """R2-2 (P1): a begin-phase violation that trips the limit must still
    restore the pre-phase snapshot before raising."""
    from civ_arena.arena.referee import Referee, RefereeConfig
    from civ_arena.arena.telemetry import TelemetryRegistry
    from civ_arena.arena.visibility import VisibilityPolicy
    from civ_arena.game.sim.simulator import SimulatorAdapter

    adapter = SimulatorAdapter()
    director = ChaosDirector([ChaosEvent(MutationSpec.STEAL_GOLD, hook="begin_phase")])
    await adapter.setup({"seed": 5, "chaos_director": director})
    log = EventLog(tmp_path / "e.jsonl")
    referee = Referee(adapter, VisibilityPolicy(), log, TelemetryRegistry(),
                      "m2", "g1", RefereeConfig(watchdog_mode="rollback",
                                                violation_limit=0))
    lease = referee.grant_lease(0, "roman", 1)
    pre = adapter.state_hash()
    with pytest.raises(MatchAborted):
        await referee.begin_turn(0, "roman", 1)
    assert adapter.state_hash() == pre, "abort must leave the pre-phase state"
    assert adapter.state.phase_player == -1
    assert not lease.released  # begin_turn aborts before any lease bookkeeping


async def test_abort_cleanup_sweeps_end_phase_chaos(tmp_path):
    """R2-3 (P1): the coordinator's post-abort phase close goes through the
    referee, so end-phase chaos during cleanup is flagged, not smuggled.
    limit=0 aborts on the FIRST act-time violation, MID-lease — the phase is
    still open when cleanup runs, which is exactly the path under test."""
    from civ_arena.arena.coordinator import Arena
    from civ_arena.config import AgentSpec, ChaosSpec, MatchSpec

    spec = MatchSpec(
        match_id="abort-cleanup", seed=90909, max_turns=10, adapter="simulator",
        watchdog_mode="flag_and_continue", violation_limit=0, checkpoint_every=5,
        agents=[AgentSpec("roman", 0, "expansionist", 11),
                AgentSpec("korea", 1, "turtler", 22)],
        chaos=[ChaosSpec("steal_gold", offset=1),          # act-time: aborts mid-lease
               ChaosSpec("spawn_free_unit", hook="end_phase", offset=0)],
    )
    arena = Arena(tmp_path / "run", spec)
    summary = await arena.run()
    assert summary["aborted"] is not None
    violations = [r for r in arena.log.records() if r["kind"] == "VIOLATION"]
    assert violations, "the act-time violation must be flagged"
    # the end-phase CLEANUP mutation (fired inside abort_cleanup's end_phase)
    # must ALSO be flagged — never unobserved
    spawned = any(
        m.get("kind") == "unit.spawned"
        for v in violations for m in v["watchdog"]["mutations"]
    )
    assert spawned, "end-phase chaos during abort cleanup must be swept"
    # cleanup actually closed the phase (the abort happened mid-lease)
    assert arena.adapter.state is not None
    assert arena.adapter.state.phase_player == -1


async def test_begin_phase_retry_exhaustion_leaves_phase_open(tmp_path):
    """R3-1 (P1): with MORE begin-phase violations than rollback retries, the
    final attempt's phase must stay OPEN — a closed phase would strand the
    lease (it could neither act nor end its turn)."""
    adapter, referee, log, ctx, _director = await harness(
        tmp_path,
        chaos=[ChaosEvent(MutationSpec.STEAL_GOLD, hook="begin_phase"),
               ChaosEvent(MutationSpec.STEAL_GOLD, hook="begin_phase"),
               ChaosEvent(MutationSpec.STEAL_GOLD, hook="begin_phase")],
        mode="rollback", violation_limit=50)
    assert adapter.state.phase_player == 0, (
        "after retry exhaustion the lease's phase must be open"
    )
    assert len(violation_docs(log)) >= 3
    # the lease remains fully usable
    settler = own_units(adapter.state, 0, "SETTLER")[0]
    doc = await referee.execute(ctx, "found_city", {"unit_id": settler["unit_id"]})
    assert doc["status"] == "accepted"
    assert (await referee.end_turn(ctx))["status"] == "accepted"


async def test_end_phase_limit_abort_is_replayable(tmp_path):
    """R2-4 (P1): even when the post-end-phase sweep aborts, the end_turn
    TOOL records must already be in the log (replay reconstructs the turn)."""
    adapter, referee, log, ctx, _director = await harness(
        tmp_path, chaos=[ChaosEvent(MutationSpec.STEAL_GOLD, hook="end_phase")],
        violation_limit=0)
    with pytest.raises(MatchAborted):
        await referee.end_turn(ctx)
    end_turn_results = [r for r in log.records()
                        if r["kind"] == "TOOL_RESULT" and r.get("tool") == "end_turn"]
    assert end_turn_results, "end_turn must be logged even when the sweep aborts"
    assert end_turn_results[-1]["status"] == "accepted"


def test_from_log_rolled_back_then_retried_key_wins():
    """R2-5 (P1): a key rolled back and then successfully retried must be
    REGISTERED after rebuild — the replayed log order decides, not a blanket
    exclusion."""
    records = [
        {"kind": "TOOL_RESULT", "status": "accepted", "duplicate": False,
         "idempotency_key": "k1", "tool": "purchase"},
        {"kind": "TOOL_RESULT", "status": "rejected", "rejection": "rollback",
         "rolled_back": True, "idempotency_key": "k1", "tool": "purchase"},
        {"kind": "TOOL_RESULT", "status": "accepted", "duplicate": False,
         "idempotency_key": "k1", "tool": "purchase"},  # the retry committed
    ]
    idx = DedupeIndex.from_log(records)
    assert idx.seen("k1") is not None, "a committed retry re-registers the key"


def test_checkpoint_turn_tamper_rejected(tmp_path):
    """R2-6 (P1): the content hash covers turn + match authority."""
    state = CheckpointState(
        match_id="m", game_instance_id_of_origin="g", turn=5, seq=10,
        sim_doc={"turn": 6}, rng_states={"sim": [3, [0], 0]},
        coordinator_state={"violations": 0}, log_prefix_sha256="x",
    )
    doc = state.to_doc()
    doc["turn"] = 100  # lie about progress
    with pytest.raises(ValueError, match="content hash mismatch"):
        CheckpointState.from_doc(doc)


async def test_released_lease_attempt_is_audited(tmp_path):
    """R2-7 (P2): reusing the exact released lease must leave an audit record."""
    adapter, referee, log, ctx, _director = await harness(tmp_path)
    await referee.end_turn(ctx)
    n_before = len(log.records())
    doc = await referee.execute(ctx, "fortify", {"unit_id": "u3"})
    assert doc["status"] == "rejected"
    assert len(log.records()) > n_before, (
        "an expired-lease attempt must not vanish from the audit log"
    )
    unauth = [r for r in log.records()[n_before:]
              if r["kind"] == "UNAUTHORIZED_TOOL_CALL"]
    assert unauth
