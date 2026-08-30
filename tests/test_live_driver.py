"""M14b rehearsal: the live adapter phase surface + driver over FakeMod.

Every live step is rehearsed here first (docs/live-validation.md §4: one
translator entry + one parser entry + one fake test)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from civ_arena.arena.events import EventLog
from civ_arena.arena.referee import Referee, RefereeConfig
from civ_arena.arena.telemetry import TelemetryRegistry
from civ_arena.arena.visibility import VisibilityPolicy
from civ_arena.config import load_config
from civ_arena.game.civ6 import lua_translator
from civ_arena.game.civ6.fake_tuner_server import FakeMod, FakeTunerServer
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from civ_arena.game.civ6.response_parser import parse_digest, parse_ledger_lines
from civ_arena.session.tools import SessionCtx

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "configs" / "live-duel.yaml"


def fake_hook(event: str, player_id: int, turn: int = 0) -> str:
    return {
        "turn_start": f"Simulate.TurnStartAt({player_id}, {turn})",
        "turn_deactivated": f"Simulate.TurnDeactivated({player_id})",
        "advance_turn": "Simulate.AdvanceTurn()",
    }[event]


async def _adapter_with(mod: FakeMod | None = None, **kw):
    server = FakeTunerServer(mod=mod or FakeMod())
    port = await server.start()
    adapter = FireTunerAdapter(
        "127.0.0.1", port, simulate_hook=fake_hook,
        poll_timeout_s=2.0, **kw)
    return adapter, server


def _referee_for(adapter, tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    referee = Referee(
        adapter, VisibilityPolicy(), log, TelemetryRegistry(),
        "live-test", "live-test-i1",
        RefereeConfig(watchdog_mode="flag_and_continue", violation_limit=5),
    )
    return referee, log


def test_parse_ledger_lines_shapes():
    docs = parse_ledger_lines([
        "LEDGER|unit.moved|unit|u7|pos|2,3|2,4",
        "AMBIENT|city.growth|city|c1|population|3|4",
        "---END---",
    ])
    assert docs[0] == {
        "kind": "unit.moved", "entity_type": "unit", "entity_id": "u7",
        "attr": "pos", "before": "2,3", "after": "2,4", "origin": "ledger",
    }
    assert docs[1]["origin"] == "ambient"
    assert docs[1]["before"] == 3 and docs[1]["after"] == 4


def test_parse_ledger_rejects_floats_and_torn_rows():
    with pytest.raises(ValueError, match="non-canonical"):
        parse_ledger_lines(["LEDGER|unit.hp|unit|u7|hp|100|98.5"])
    with pytest.raises(ValueError, match="malformed"):
        parse_ledger_lines(["LEDGER|only|three|fields"])
    with pytest.raises(ValueError, match="non-ledger"):
        parse_ledger_lines(["TURNS|7"])


def test_coerce_strict_rejects_every_numeric_spelling():
    """Codex P2-9: .5 / 1. / 1e3 / nan / inf / +7 must ALL fail closed —
    only plain integers are canonical numbers on the wire."""
    from civ_arena.game.civ6.response_parser import _coerce_strict

    for bad in (".5", "1.", "1e3", "1E3", "nan", "inf", "-inf", "+7", "1_0"):
        with pytest.raises(ValueError, match="non-canonical"):
            _coerce_strict(bad)
    # and the legal shapes still pass
    assert _coerce_strict("-12") == -12
    assert _coerce_strict("true") is True
    assert _coerce_strict("2,3") == "2,3"  # positions stay strings
    assert _coerce_strict("u7") == "u7"


def test_parse_digest():
    assert parse_digest(["DIGEST|u1|0|1|2", "---END---"]) == "u1|0|1|2"
    with pytest.raises(ValueError, match="DIGEST"):
        parse_digest(["TURN|3"])


def test_translator_mod_commands():
    assert lua_translator.set_puppet(0, True) == "Puppeteer.SetPuppet(0, true)"
    assert lua_translator.set_puppet(1, False) == \
        "Puppeteer.SetPuppet(1, false)"
    assert lua_translator.restore_unit("u7") == "Puppeteer.RestoreUnit(7)"
    assert lua_translator.restore_unit("c3") == "Puppeteer.RestoreUnit(3)"
    assert "ACTION_ENDTURN" in lua_translator.request_end_turn(0)
    assert "SetCivic" not in lua_translator.request_end_turn(0)


async def test_begin_end_phase_cycle_and_zero_drift(tmp_path):
    """The exclusive-control rehearsal: idle lease, declared empty ambient,
    zero drift, zero violations — through the REAL referee machinery."""
    adapter, server = await _adapter_with()
    try:
        referee, log = _referee_for(adapter, tmp_path)
        await adapter.setup({})
        status = await adapter.poll_status()
        turn = int(status["TURN"])
        lease = referee.grant_lease(0, "turtler-a", turn)
        await referee.begin_turn(0, "turtler-a", turn)
        assert await adapter.current_phase() == {
            "turn": turn, "phase_player": 0, "phase_index": 0}
        digest_open = await adapter.refresh_digest()

        ctx = SessionCtx(referee=referee, player_id=0, agent_id="turtler-a",
                         lease=lease, turn=turn)
        await referee.end_turn(ctx)
        digest_close = adapter.state_hash()  # cached inside end_phase
        assert digest_open == digest_close, "idle lease must not drift"
        assert referee.violation_count() == 0
        assert (await adapter.current_phase())["phase_player"] == -1
        # the event log carries the phase trail
        kinds = [json.loads(line)["kind"] for line in
                 (tmp_path / "events.jsonl").read_text().splitlines()]
        assert "AMBIENT" in kinds and "LEASE_GRANT" in kinds
        assert "LEASE_RELEASE" in kinds and "TURN_END" in kinds
    finally:
        await server.stop()


async def test_undeclared_drift_flags_violation(tmp_path):
    """Simulate.* books an undeclared LEDGER row during the lease: the
    release dump makes it an actual with no manifest — the referee flags."""
    adapter, server = await _adapter_with()
    try:
        referee, _log = _referee_for(adapter, tmp_path)
        await adapter.setup({})
        status = await adapter.poll_status()
        turn = int(status["TURN"])
        lease = referee.grant_lease(0, "turtler-a", turn)
        await referee.begin_turn(0, "turtler-a", turn)
        # engine drift the referee never declared
        await adapter.read_raw("Simulate.Ledger(move, unit, 7, movement, 2, 0)")
        ctx = SessionCtx(referee=referee, player_id=0, agent_id="turtler-a",
                         lease=lease, turn=turn)
        await referee.end_turn(ctx)
        assert referee.violation_count() > 0, (
            "an undeclared mutation must flag — the watchdog has teeth live")
    finally:
        await server.stop()


async def test_phase_surface_guards(tmp_path):
    adapter, server = await _adapter_with()
    try:
        await adapter.setup({})
        # double-open refused
        await adapter.begin_phase(0, 1)
        with pytest.raises(RuntimeError, match="already open"):
            await adapter.begin_phase(0, 1)
        # closing the wrong player refused
        with pytest.raises(RuntimeError, match="not 1"):
            await adapter.end_phase(1, 1)
        await adapter.end_phase(0, 1)
        # state hash is a stable function of the cached digest
        assert adapter.state_hash() == adapter.state_hash()
        assert adapter.drain_mutations() == []
    finally:
        await server.stop()


async def test_end_phase_strategies_rehearsed(tmp_path):
    for strategy in ("h1", "h2", "h3"):
        adapter, server = await _adapter_with(end_phase_strategy=strategy)
        try:
            await adapter.setup({})
            await adapter.begin_phase(0, 1)
            await asyncio.wait_for(adapter.end_phase(0, 1), timeout=5.0)
            assert (await adapter.current_phase())["phase_player"] == -1
        finally:
            await server.stop()


async def test_turn_mismatch_is_loud():
    """The lease engaging at the WRONG turn is a refusal (Codex P1-3's
    LEASE_TURN half): the hook fires five turns ahead of the target."""
    def ahead_hook(event: str, player_id: int, turn: int = 0) -> str:
        return {
            "turn_start": f"Simulate.TurnStartAt({player_id}, {turn + 5})",
            "turn_deactivated": f"Simulate.TurnDeactivated({player_id})",
            "advance_turn": "Simulate.AdvanceTurn()",
        }[event]

    server = FakeTunerServer(mod=FakeMod())
    port = await server.start()
    adapter = FireTunerAdapter("127.0.0.1", port, simulate_hook=ahead_hook,
                               poll_timeout_s=1.0)
    try:
        await adapter.setup({})
        with pytest.raises(RuntimeError, match="timed out"):
            await adapter.begin_phase(0, 3)
    finally:
        await server.stop()


async def test_lease_for_the_wrong_player_is_refused():
    """Codex P1-3: an engaged lease for ANYONE else (or a stale turn) is a
    refusal — begin_phase must verify LEASE_PLAYER/LEASE_TURN, not just
    PUPPET_ACTIVE."""
    def wrong_player_hook(event: str, player_id: int, turn: int = 0) -> str:
        return {
            "turn_start": "Simulate.TurnStart(1)",  # the OTHER player's hook
            "turn_deactivated": f"Simulate.TurnDeactivated({player_id})",
            "advance_turn": "Simulate.AdvanceTurn()",
        }[event]

    server = FakeTunerServer(mod=FakeMod())
    port = await server.start()
    adapter = FireTunerAdapter("127.0.0.1", port,
                               simulate_hook=wrong_player_hook,
                               poll_timeout_s=1.0)
    try:
        await adapter.setup({})
        with pytest.raises(RuntimeError,
                           match=r"lease to engage for player 0 at turn 1"):
            await adapter.begin_phase(0, 1)
    finally:
        await server.stop()


def test_digest_rows_filter_by_owner():
    """Codex P1-4: a phase's hash covers ONLY the phase owner's rows —
    live play is asynchronous and the next player must not leak into this
    phase's hash trail."""
    from civ_arena.game.civ6.firetuner import filter_digest_rows

    digest = "u9|1|3|4|2|100|false;c3|0|7;p1|40|5;u2|0|1|1|2|95|true;p0|12|-1"
    assert filter_digest_rows(digest, 0) == ["c3|0|7", "p0|12|-1", "u2|0|1|1|2|95|true"]
    assert filter_digest_rows(digest, 1) == ["p1|40|5", "u9|1|3|4|2|100|false"]
    assert filter_digest_rows("rehearsal|turn=1|nonce=0", 0) == []


async def test_sealed_phase_hash_immune_to_foreign_drift():
    """Codex P1-4: after end_phase the hash is SEALED — later digest polls
    (foreign activity racing the next player's phase) cannot move what the
    referee's TURN_END records."""
    adapter, server = await _adapter_with()
    try:
        await adapter.setup({})
        await adapter.begin_phase(0, 1)
        await adapter.end_phase(0, 1)
        sealed_end = adapter.state_hash()
        # foreign drift AFTER the seal: the digest cache moves, the served
        # hash must not
        await adapter.read_raw("Simulate.Mutate()")
        await adapter._refresh_digest()  # noqa: SLF001 — the race made real
        assert adapter.state_hash() == sealed_end
    finally:
        await server.stop()


async def test_ambient_manifest_rehearsal(tmp_path):
    """A declared ambient effect (population growth inside the window)
    flows: window diff -> DumpAmbient rows -> the referee's AMBIENT
    manifest; the sweep stays CLEAN (declared ambient is authorized)."""
    mod = FakeMod(auto_ambient=("city.growth", "city", 1, "population", 3, 4))
    adapter, server = await _adapter_with(mod)
    try:
        referee, log = _referee_for(adapter, tmp_path)
        await adapter.setup({})
        turn = int((await adapter.poll_status())["TURN"])
        lease = referee.grant_lease(0, "turtler-a", turn)
        await referee.begin_turn(0, "turtler-a", turn)
        # begin_phase consumed DumpAmbient as the manifest: one growth row
        records = log.records()
        ambient_events = [r for r in records if r["kind"] == "AMBIENT"]
        assert len(ambient_events) == 1
        manifest = ambient_events[0]["manifest"]
        assert manifest[0]["kind"] == "city.growth"
        assert manifest[0]["origin"] == "ambient"
        ctx = SessionCtx(referee=referee, player_id=0, agent_id="turtler-a",
                         lease=lease, turn=turn)
        await referee.end_turn(ctx)
        # declared ambient never violates
        assert referee.violation_count() == 0
    finally:
        await server.stop()


def test_live_driver_rehearsal_end_to_end(tmp_path):
    """The driver's own entrypoint, rehearsed: probe then 2 clean idle turns."""
    runs_root = tmp_path / "runs"
    for phase, turns in (("probe", None), ("exclusive-control", "2")):
        cmd = [sys.executable, "-m", "civ_arena.game.civ6.live_driver",
               str(CONFIG), "--phase", phase, "--fake",
               "--runs-root", str(runs_root)]
        if turns:
            cmd += ["--turns", turns]
        proc = subprocess.run(
            cmd, cwd=REPO, capture_output=True, text=True, timeout=120.0)
        assert proc.returncode == 0, proc.stderr + proc.stdout
    run_dir = runs_root / "live-duel-001"
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["phase"] == "exclusive-control"
    assert summary["clean"] is True
    assert summary["violations_total"] == 0
    assert len(summary["per_turn"]) == 2
    # Arena.run envelope parity (Codex P2-6): replay/_strip read these
    assert summary["final_state_hash"] is not None
    assert "aborted" in summary and "scores" in summary
    assert "telemetry" in summary
    kinds = [json.loads(line)["kind"] for line in
             (run_dir / "events.jsonl").read_text().splitlines()]
    assert kinds[0] == "MATCH_START" and kinds[-1] == "MATCH_END"
    assert "TURN_END" in kinds


def test_live_driver_refuses_finished_rerun(tmp_path):
    runs_root = tmp_path / "runs"
    cmd = [sys.executable, "-m", "civ_arena.game.civ6.live_driver",
           str(CONFIG), "--phase", "exclusive-control", "--fake", "--turns", "1",
           "--runs-root", str(runs_root)]
    first = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                           timeout=120.0)
    assert first.returncode == 0, first.stderr
    second = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                            timeout=120.0)
    assert second.returncode != 0
    assert "already exists" in second.stderr


def test_live_driver_refuses_partial_rerun(tmp_path):
    """Codex P1-5: a PARTIAL log (timed-out attempt, no MATCH_END) must
    also refuse — appending a second MATCH_START would splice attempts."""
    run_dir = tmp_path / "runs" / "live-duel-001"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text('{"kind": "MATCH_START"}\n')
    cmd = [sys.executable, "-m", "civ_arena.game.civ6.live_driver",
           str(CONFIG), "--phase", "exclusive-control", "--fake", "--turns", "1",
           "--runs-root", str(tmp_path / "runs")]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                          timeout=120.0)
    assert proc.returncode != 0
    assert "already exists" in proc.stderr


def test_rollback_mode_refused(tmp_path):
    from civ_arena.game.civ6 import live_driver

    spec = load_config(CONFIG)
    spec.watchdog_mode = "rollback"
    with pytest.raises(ValueError, match="rollback"):
        live_driver.LiveDriver(
            spec, FireTunerAdapter("127.0.0.1", 1), tmp_path, "x-i1")
