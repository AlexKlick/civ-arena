"""M18: the hotseat 1v1 rehearsed through the live dispatch surface.

Both seats arena-driven over the FakeTunerServer mini-engine: the engine
AI is out of the game entirely (the class of engine-AI hangs that ends
long games dies with it). The rehearsal proves the ALTERNATION — each
round drives every seat once, in seat order, through the real
PlayerSession/Referee/tool surface — which is the readiness gate for the
first live hotseat dispatch.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "configs" / "live-hotseat-001.yaml"


def test_hotseat_dispatch_rehearsal_alternates_seats(tmp_path):
    runs_root = tmp_path / "runs"
    cmd = [sys.executable, "-m", "civ_arena.game.civ6.live_driver",
           str(CONFIG), "--phase", "dispatch-hotseat", "--fake",
           "--turns", "2", "--runs-root", str(runs_root)]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                          timeout=300.0)
    assert proc.returncode == 0, proc.stderr + proc.stdout

    summary = json.loads(
        (runs_root / "live-hotseat-001" / "summary.json").read_text())
    assert summary["phase"] == "dispatch-hotseat"
    assert summary["clean"] is True
    assert summary["violations_total"] == 0
    rows = summary["per_turn"]
    # ALTERNATION: each round drives every seat once, in seat order
    assert [(r["turn"], r["player"]) for r in rows] == [
        (1, 0), (1, 1), (2, 0), (2, 1)]
    agents = {r["player"]: r["agent"] for r in rows}
    assert agents[0] == "hotseat-planner"
    assert agents[1] == "hotseat-turtler"
    # both seats actually played (commanded effects landed for each)
    assert sum(r["allowed_mutations"] for r in rows if r["player"] == 0) > 0
    assert sum(r["allowed_mutations"] for r in rows if r["player"] == 1) > 0

    records = [json.loads(line) for line in
               (runs_root / "live-hotseat-001" / "events.jsonl")
               .read_text().splitlines()]
    # every tool call has its result pair (the log's replay contract)
    results = [r for r in records if r["kind"] == "TOOL_RESULT"]
    assert len(results) == len([r for r in records
                                if r["kind"] == "TOOL_CALL"])
    # both seats' calls are in the log
    players_called = {r["player_id"] for r in records
                      if r["kind"] == "TOOL_CALL"}
    assert players_called == {0, 1}
    # the planner seat's belief journal landed per seat
    assert (runs_root / "live-hotseat-001" / "planner"
            / "p0-journal.jsonl").exists()


    from civ_arena.game.civ6.validate_run import validate
    result = validate(runs_root / "live-hotseat-001", 2, require_live=False)
    expected_errors = ["clean commit identity"] if summary["identity"]["dirty"] else []
    assert result["errors"] == expected_errors
    # Even a coordinated summary + MATCH_END edit cannot hide a missing
    # seat turn from the authoritative release/tool/completion event rows.
    summary["per_turn"].pop()
    records[-1]["summary"] = summary
    (runs_root / "live-hotseat-001" / "summary.json").write_text(json.dumps(summary))
    (runs_root / "live-hotseat-001" / "events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n")
    result = validate(runs_root / "live-hotseat-001", 2, require_live=False)
    assert result["status"] == "FAIL"
    assert "completed seat count" in result["errors"]


def _fake_mod():
    from civ_arena.game.civ6.fake_tuner_server import FakeMod

    mod = FakeMod(injected=True)
    return mod


# -- M4: the spectator_world audit (contract §4a) -------------------------------

SPECTATOR_DOC = {
    "match": {"match_id": "m4p-spectator-hotseat", "seed": 271828,
              "max_turns": 30, "checkpoint_every": 5, "adapter": "firetuner",
              "watchdog_mode": "flag_and_continue", "violation_limit": 10,
              "completeness_gate": True, "declare_own_endpath_drift": True,
              "spectator_capture": True},
    "agents": [
        {"agent_id": "hotseat-planner", "player_id": 0, "policy": "planner",
         "seed": 7},
        {"agent_id": "hotseat-turtler", "player_id": 1, "policy": "turtler",
         "seed": 22},
    ],
    "chaos": [],
}


async def _run_hotseat(tmp_path, mod, rounds=1):
    from civ_arena.config import parse_config
    from civ_arena.game.civ6 import live_driver as ld
    from civ_arena.game.civ6.fake_tuner_server import FakeTunerServer
    from civ_arena.game.civ6.firetuner import FireTunerAdapter

    server = FakeTunerServer(mod=mod)
    port = await server.start()
    adapter = FireTunerAdapter("127.0.0.1", port,
                               simulate_hook=ld._fake_hook)  # noqa: SLF001
    run_dir = tmp_path / "run"
    try:
        result = await ld.phase_dispatch_hotseat(
            parse_config(json.loads(json.dumps(SPECTATOR_DOC))), adapter,
            run_dir, rounds, "h1", ld.MOD_DEFAULT.read_text())
    finally:
        await adapter.teardown()
        await server.stop()
    events = [json.loads(line) for line in
              (run_dir / "events.jsonl").read_text().splitlines()]
    summary = json.loads((run_dir / "summary.json").read_text())
    return result, events, summary, run_dir


async def test_spectator_capture_rehearsal_writes_baseline_and_per_seat_audits(
        tmp_path):
    from civ_arena.game.civ6.fake_tuner_server import FakeMod

    mod = FakeMod(hotseat=[0, 1])
    result, events, summary, run_dir = await _run_hotseat(tmp_path, mod, rounds=1)
    assert result == 0, summary["failure_reason"]
    assert summary["clean"] is True
    worlds = [e for e in events if e.get("audit") == "spectator_world"]
    # baseline (after the initial-attach step) + one per completed seat turn
    assert [w["after_seat"] for w in worlds] == [-1, 0, 1]
    assert all(w["visibility_scope"] == "spectator" for w in worlds)
    assert all(w["phase_player_id"] == -1 and w["player_id"] is None
               for w in worlds)
    baseline, after0, after1 = (w["world"] for w in worlds)
    for world in (baseline, after0, after1):
        assert world["schema"] == 1
        assert {r["kind"] for r in world["roster"]} == {"major"}
        assert [p["player_id"] for p in world["players"]] == [0, 1]
        assert world["cities"] and world["cities"][0]["owner"] == 0
        assert world["cities"][0]["hp"] == 200  # real hp, not the placeholder
        assert set(world["owned_tiles_columns"]) == {"0"}
        assert world["contexts"] == {"roster": "gamecore", "tiles": "gamecore",
                                     "palette": "ingame"}
        assert world["palette"]["0"]["primary"] == -1000000 + 2**32
        assert world["game_era"] == "ERA_FAKE"
        assert world["fog_audit"]["requested"] >= 0
        assert world["truncated"]["world"] is False
        assert len(json.dumps(world)) <= 256 * 1024
    # the baseline precedes the first completed seat turn in the log
    first_completed = next(e for e in events
                           if e.get("audit") == "completed_seat_turn")
    assert worlds[0]["seq"] < first_completed["seq"]
    # CAP-02 territory: a clean hotseat validation IGNORES the audit
    from civ_arena.game.civ6.validate_run import validate
    verdict = validate(run_dir, 1, require_live=False)
    expected_errors = (["clean commit identity"]
                       if summary["identity"]["dirty"] else [])
    assert verdict["errors"] == expected_errors


async def test_fail_spectator_audits_failure_and_match_stays_clean(tmp_path):
    from civ_arena.game.civ6.fake_tuner_server import FakeMod

    mod = FakeMod(hotseat=[0, 1], fail_spectator=True)
    result, events, summary, _ = await _run_hotseat(tmp_path, mod, rounds=1)
    assert result == 0, "the spectator audit must never kill the match"
    assert summary["clean"] is True
    failures = [e for e in events if e.get("audit") == "spectator_world_failed"]
    assert [f["after_seat"] for f in failures] == [-1, 0, 1]
    assert all(f["visibility_scope"] == "spectator" for f in failures)
    # the message ran through the existing redactor: bounded, non-empty
    assert all(isinstance(f.get("error"), str) and f["error"]
               and len(f["error"]) <= 1500 for f in failures)
    assert not [e for e in events if e.get("audit") == "spectator_world"]


def test_fake_mod_ownership_follows_local_player_switch():
    """A2: on the Architecture-1 path the engine makes the lease-holder
    local — the fake's `me` derives from local_player (Codex r1 P2-10)
    so a missing switch fails loudly in rehearsal: an owner-checked act
    on a seat-1 entity is rejected until the switch runs, accepted
    after."""
    from civ_arena.game.civ6 import lua_translator as lt

    mod = _fake_mod()
    seat1_units = [uid for uid, u in mod.units.items() if u["owner"] == 1]
    assert seat1_units, "fake roster must include a seat-1 unit"
    # Explicit owner and full engine id.
    uid = f"u1:{seat1_units[0]}"
    lua = lt.move_unit(uid, "1,2")

    # lease engaged but NO switch: `me` is still 0 — the act is rejected
    mod.lease = {"player": 1, "turn": 1}
    out = mod._act("move_unit", lua)  # noqa: SLF001
    assert out and out[0].startswith("ACT|move_unit|ERR")

    # the driver's switch ran: `me` is 1 — the act is owned and accepted
    rows = mod.respond("PlayerManager.SetLocalPlayerAndObserver(1) "
                       "print('LOCAL_SWITCHED|1|1') print('---END---')")
    assert any(r.startswith("LOCAL_SWITCHED|1|1") for r in rows)
    out = mod._act("move_unit", lua)  # noqa: SLF001
    assert out and out[0].startswith("ACT|move_unit|OK"), out


def test_fake_mod_answers_local_switch_and_unpause():
    """A2: the driver's lease-engagement commands (switch_local_player,
    unpause_local) must be answered by the fake — the switch updates the
    modeled local player and echoes the read-back row."""
    mod = _fake_mod()
    rows = mod.respond(
        'PlayerManager.SetLocalPlayerAndObserver(1) '
        'print("LOCAL_SWITCHED|1|" .. tostring(Game.GetLocalPlayer())) '
        'print("---END---")')
    assert rows and any(r.startswith("LOCAL_SWITCHED|1|1") for r in rows)
    assert mod.local_player == 1
    rows = mod.respond(
        'local lp = Game.GetLocalPlayer() '
        'PlayerConfigurations[lp]:SetWantsPause(false) '
        'print("UNPAUSED|" .. tostring(lp)) print("---END---")')
    assert rows and any(r.startswith("UNPAUSED|") for r in rows)


def test_driver_hotseat_fires_local_switch_after_engagement():
    """A2 static pin: the hotseat loop switches the local player to the
    lease-holder right after begin_turn engages (the A1-proven delta —
    the NONE-load path never makes the seat local on its own) and fires
    the idempotent un-pause; stall recovery is Return-first there."""
    src = (REPO / "src" / "civ_arena" / "game" / "civ6"
           / "live_driver.py").read_text()
    hotseat = src[src.index("async def phase_dispatch_hotseat"):
                  src.index("_TECH_PREFERENCE")]
    assert "activate_human_seat(agent.player_id, turn)" in hotseat
    assert "unpause_local()" in hotseat
    # the switch fires AFTER the begin_turn retry block, BEFORE the
    # ENGAGE-TIME digest (the earlier refresh_digest near the top is the
    # match_start seeding — a different call)
    i_begin = hotseat.index("await driver.referee.begin_turn(")
    i_switch = hotseat.index("activate_human_seat(agent.player_id, turn)")
    i_digest = hotseat.index("refresh_digest()", i_switch)
    assert i_begin < i_switch < i_digest
    # Return-first stall sweep on the hotseat path only
    assert 'keys=("Return", "Escape", "Escape")' in hotseat
    dispatch = src[src.index("async def phase_dispatch("):
                   src.index("async def phase_dispatch_hotseat")]
    assert "Return" not in dispatch.split("_recover_stall")[-1].split("\n")[0]


def test_driver_abort_paths_write_match_end():
    """A2 static pin: both dispatch loops catch MatchAborted, run the
    referee's abort_cleanup, and write a match_end carrying `aborted` —
    an LLM auth-death mid-match must leave a replay-consumable record."""
    src = (REPO / "src" / "civ_arena" / "game" / "civ6"
           / "live_driver.py").read_text()
    assert "except (Exception, asyncio.CancelledError, KeyboardInterrupt) as exc:" in src
    assert '"aborted": failure' in src
    assert "driver.referee.abort_cleanup(" in src
    # single-seat parity: the runtime is aclose()d in phase_dispatch too
    dispatch = src[src.index("async def phase_dispatch("):
                   src.index("async def phase_dispatch_hotseat")]
    assert "aclose" in dispatch
