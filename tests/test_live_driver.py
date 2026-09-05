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
from civ_arena.game.civ6.response_parser import (
    _split_lines,
    parse_digest,
    parse_ledger_lines,
)
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
    assert lua_translator.restore_unit("u0:7") == "Puppeteer.RestoreUnit(7, 0)"
    assert lua_translator.restore_unit("u1:3") == "Puppeteer.RestoreUnit(3, 1)"
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
                               poll_timeout_s=1.0, turn_wait_s=1.0)
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
                               poll_timeout_s=1.0, turn_wait_s=1.0)
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


# -- M14d: action surface + dispatch -----------------------------------------

from civ_arena.game.adapter import ActionCommand, ObserveKind, ObserveRequest  # noqa: E402


def _cmd(tool: str, args: dict, player_id: int = 0) -> ActionCommand:
    return ActionCommand(tool=tool, args=args, player_id=player_id,
                         idempotency_key=f"k-{tool}", lease_id="lease-1")


def test_axial_offset_roundtrip():
    """The host-verified odd-row frame round-trips, including negative axes."""
    for x in range(-24, 24):
        for y in range(-24, 24):
            q, r = lua_translator.xy_to_axial(x, y)
            assert lua_translator.axial_to_xy(q, r) == (x, y)


def test_translator_act_routing_pins():
    """Every InGame tool emits its own Request* token and NO GameCore-only
    token; set_research is the inverse. The standing SetCivic prohibition
    (permanently breaks AI civics) covers the whole module."""
    ingame = {
        "move_unit": lua_translator.move_unit("u0:1", "2,3"),
        "attack": lua_translator.attack("u0:1", "u0:2"),
        "fortify": lua_translator.fortify("u0:1"),
        "found_city": lua_translator.found_city("u0:1"),
        "set_city_production": lua_translator.set_city_production("c0:1", "WARRIOR"),
        "purchase": lua_translator.purchase("c0:1", "MONUMENT"),
    }
    for tool, lua in ingame.items():
        assert "RequestOperation" in lua or "RequestCommand" in lua, tool
        assert "Puppeteer." not in lua, f"{tool}: Puppeteer is GameCore-only"
        assert "MoveUnit" not in lua, f"{tool}: MoveUnit is GameCore-only"
        assert "SetResearchingTech" not in lua, tool
        assert f"arena:tool={tool}" in lua, f"{tool}: fake marker"
    research = lua_translator.set_research(0, "MINING")
    assert "SetResearchingTech" in research and "CanResearch" in research
    assert "UI.RequestAction" not in research and "RequestOperation" not in research
    for name in dir(lua_translator):
        obj = getattr(lua_translator, name)
        if callable(obj) and not name.startswith("_"):
            try:
                out = obj() if obj.__code__.co_argcount == 0 else None
            except TypeError:
                out = None
            if isinstance(out, str):
                assert "SetCivic" not in out, f"{name} emits the forbidden SetCivic"


def test_act_args_injection_guard():
    """Agent-supplied ids are interpolated into Lua source — only strict
    spellings cross (defense in depth beyond the referee's type checks)."""
    from civ_arena.game.civ6.firetuner import _arg_violation

    assert _arg_violation("set_research", {"tech_id": "MINING"}) is None
    assert _arg_violation("move_unit", {"unit_id": "u0:7", "dest": "-3,4"}) is None
    for tool, bad in (
        ("set_research", {"tech_id": "MINING'] Evil() --"}),
        ("move_unit", {"unit_id": "u0:7", "dest": "1,2); Evil("}),
        ("attack", {"unit_id": "u7'", "target_id": "u0:1"}),
        ("purchase", {"city_id": "c0:1", "item_id": "lower_case"}),
    ):
        assert _arg_violation(tool, bad) is not None, (tool, bad)


def test_parse_act_verdicts():
    from civ_arena.game.civ6.response_parser import parse_act

    assert parse_act(["ACT|move_unit|OK|4,5", "---END---"]) == {
        "tool": "move_unit", "status": "accepted", "detail": "4,5"}
    assert parse_act(["ACT|purchase|ERR|INSUFFICIENT_GOLD|120gt100"]) == {
        "tool": "purchase", "status": "rejected",
        "rejection": "INSUFFICIENT_GOLD", "detail": "120gt100"}
    with pytest.raises(ValueError, match="no ACT row"):
        parse_act(["TURN|3"])
    with pytest.raises(ValueError, match="two ACT rows"):
        parse_act(["ACT|a|OK|1", "ACT|a|OK|2"])


def test_parse_observes_are_sim_shaped():
    from civ_arena.game.civ6.response_parser import (
        parse_available_production,
        parse_available_research,
        parse_cities,
        parse_overview,
        parse_units,
    )

    units = parse_units([
        "UNITROW|2|0|WARRIOR|5|2|100|2|2|20|0|false",
        "UNITROW|1|1|ARCHER|1|1|70|1|2|15|15|true",
    ])
    # every key the projection reads for OWN units must be present
    assert set(units[0]) == {
        "unit_id", "owner", "type", "q", "r", "hp", "movement",
        "max_movement", "strength", "ranged_strength", "fortified"}
    # numeric-id sort regardless of row order
    assert units[0]["unit_id"] == "u1" and units[0]["owner"] == 1
    assert units[1]["unit_id"] == "u2" and units[1]["owner"] == 0
    assert units == sorted(units, key=lambda u: int(u["unit_id"][1:]))
    cities = parse_cities(["CITYROW|1|0|ARENA|2|1|3|MONUMENT"])
    assert cities[0]["production_queue"] == ["MONUMENT"]
    assert parse_cities(["CITYROW|1|0|ARENA|2|1|3|-"])[0][
        "production_queue"] == []
    overview = parse_overview([
        "TURN|7", "OVROW|0|CIVILIZATION_ROME|120|MINING",
        "OVROW|1|CIVILIZATION_KOREA|100|-",
        "OVRESEARCHED|0|MINING;POTTERY",
    ])
    assert overview["turn"] == 7
    assert overview["players"]["0"]["researching"] == "MINING"
    assert overview["players"]["0"]["researched"] == ["MINING", "POTTERY"]
    assert overview["players"]["1"]["researching"] is None
    research = parse_available_research(["TECHROW|MINING|25"])
    assert research == [{"tech_id": "MINING", "cost": 25}]
    production = parse_available_production(["ITEMROW|building|WALLS|70|12",
                                             "ITEMROW|unit|WARRIOR|40|5"])
    assert [p["item_id"] for p in production] == ["WARRIOR", "WALLS"]
    assert production[0]["kind"] == "unit"


async def test_observes_over_fake_and_foreign_projection():
    """The six observes run over the fake wire. M17c: the map read is real
    (the M14d empty-set declaration retired) — visible tiles carry
    owner/city, fog tiles are terrain-only — and the REAL projection still
    hides foreign entities outside the observer's sight (the safe side of
    no-leak) while keeping own entities fully projected."""
    from civ_arena.arena.visibility import Scope

    adapter, server = await _adapter_with()
    try:
        await adapter.setup({})
        units = await adapter.observe(
            ObserveRequest(kind=ObserveKind.UNITS, player_id=0))
        assert any(u["owner"] == 1 for u in units), "omniscient read"
        assert any(u["type"] == "SETTLER" for u in units)
        cities = await adapter.observe(
            ObserveRequest(kind=ObserveKind.CITIES, player_id=0))
        assert cities and cities[0]["city_id"] == "c0:1"
        research = await adapter.observe(
            ObserveRequest(kind=ObserveKind.AVAILABLE_RESEARCH, player_id=0))
        assert {"tech_id": "MINING", "cost": 25} in research
        production = await adapter.observe(ObserveRequest(
            kind=ObserveKind.AVAILABLE_PRODUCTION, player_id=0,
            subject_id="c0:1"))
        assert {"item_id": "MONUMENT", "cost": 60, "turns": 10,
                "kind": "building"} in production
        vmap = await adapter.observe(
            ObserveRequest(kind=ObserveKind.VISIBLE_MAP, player_id=0))
        assert vmap["tiles"], "M17c: the revealed-tiles read is real"
        for tile in vmap["tiles"].values():
            # visible: terrain+owner+city; fog: terrain ONLY (the wire
            # never reads fog ownership)
            assert set(tile) in ({"terrain"}, {"terrain", "owner", "city"})
        # the projection with the adapter's own visibility ground truth
        policy = VisibilityPolicy()
        observable, remembered = adapter.visibility_for(0)
        assert observable | remembered == frozenset(vmap["tiles"])
        projected = policy.project(units, "units", 0, observable, remembered,
                                   Scope.PRIVATE_PLAYER)
        owners = {u["owner_id"] for u in projected}
        assert owners == {0}, "foreign units must be hidden, own present"
        # the projected map keeps fog terrain but never fog ownership
        pmap = policy.project(vmap, "visible_map", 0, observable, remembered,
                              Scope.PRIVATE_PLAYER)
        for key, tile in pmap["tiles"].items():
            if key in observable:
                assert "owner_id" in tile
            else:
                assert set(tile) == {"coord", "terrain"}
    finally:
        await server.stop()


async def test_act_accepted_books_commanded_rows_and_refreshes_digest():
    adapter, server = await _adapter_with()
    try:
        await adapter.setup({})
        await adapter.begin_phase(0, 1)
        commands: list[str] = []
        received = server.received_commands
        del received[:]
        received_callbacks = received  # alias: appended per command below
        _ = received_callbacks, commands
        pre_hash = adapter.state_hash()
        res = await adapter.act(_cmd("move_unit", {
            "unit_id": "u0:2", "dest": "5,4"}))
        assert res.status == "accepted", res
        kinds = [(m.kind, m.entity_id) for m in res.mutations]
        assert ("unit.moved", "u0:2") in kinds, kinds
        assert ("unit.moves", "u0:2") in kinds, kinds
        # the journal holds THE SAME records (allowed == actual multiset)
        drained = adapter.drain_mutations()
        assert [(m.kind, m.entity_id, m.attr, m.before, m.after)
                for m in drained] == [
            (m.kind, m.entity_id, m.attr, m.before, m.after)
            for m in res.mutations]
        assert all(m.origin == "command" for m in drained)
        # Codex P2-10: the digest refreshed — post-act hash moved
        assert adapter.state_hash() != pre_hash
        # restore ran BEFORE the move command (unfreeze-then-act)
        lua_sequence = [c for c in server.received_commands
                        if "Puppeteer.RestoreUnit" in c or "MOVE_TO" in c]
        assert lua_sequence and "RestoreUnit" in lua_sequence[0]
        await adapter.end_phase(0, 1)
    finally:
        await server.stop()


async def test_act_rejected_refreezes_and_books_nothing():
    adapter, server = await _adapter_with()
    try:
        await adapter.setup({})
        await adapter.begin_phase(0, 1)
        pre_hash = adapter.state_hash()
        res = await adapter.act(_cmd("move_unit", {
            "unit_id": "u0:999", "dest": "5,4"}))
        assert res.status == "rejected" and res.rejection == "unknown_entity"
        assert res.mutations == () and adapter.drain_mutations() == []
        assert adapter.state_hash() == pre_hash
        # the undo: FreezeUnit ran AFTER the failed command
        lua_sequence = [c for c in server.received_commands
                        if "FreezeUnit" in c or "MOVE_TO" in c]
        assert lua_sequence and "FreezeUnit" in lua_sequence[-1]
        await adapter.end_phase(0, 1)
    finally:
        await server.stop()


async def test_act_out_of_phase_refused():
    adapter, server = await _adapter_with()
    try:
        await adapter.setup({})
        res = await adapter.act(_cmd("fortify", {"unit_id": "u0:2"}))
        assert res.status == "rejected" and res.rejection == "no_lease"
        # unknown tool with a VALID open phase -> not_implemented (the
        # no-lease guard is checked first by design)
        await adapter.begin_phase(0, 1)
        res = await adapter.act(_cmd("spawn_dragons", {}))
        assert res.status == "rejected" and res.rejection == "not_implemented"
        await adapter.end_phase(0, 1)
    finally:
        await server.stop()


async def test_act_injection_guard_blocks_hostile_ids():
    """A hostile id never reaches Lua — the rejection happens adapter-side."""
    adapter, server = await _adapter_with()
    try:
        await adapter.setup({})
        await adapter.begin_phase(0, 1)
        res = await adapter.act(_cmd("set_research", {
            "tech_id": "MINING'] Evil() --"}))
        assert res.status == "rejected" and res.rejection == "args_invalid"
        assert not any("Evil" in c for c in server.received_commands)
        await adapter.end_phase(0, 1)
    finally:
        await server.stop()


def test_dispatch_rehearsal_end_to_end(tmp_path):
    """The M14d milestone rehearsal: the turtler takes real turns through
    the REAL referee over the fake wire — observes, acts, reconciliation,
    end turns — and the watchdog stays clean."""
    runs_root = tmp_path / "runs"
    cmd = [sys.executable, "-m", "civ_arena.game.civ6.live_driver",
           str(CONFIG), "--phase", "dispatch", "--fake", "--turns", "5",
           "--runs-root", str(runs_root)]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                          timeout=180.0)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    run_dir = runs_root / "live-duel-001"
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["phase"] == "dispatch"
    assert summary["clean"] is True
    assert summary["violations_total"] == 0
    assert len(summary["per_turn"]) == 5
    # turn 1 books real commanded effects (research + founding at least)
    assert summary["per_turn"][0]["allowed_mutations"] > 0
    assert summary["per_turn"][0]["digest_changed"] is True
    records = [json.loads(line) for line in
               (run_dir / "events.jsonl").read_text().splitlines()]
    tools = [r["tool"] for r in records if r["kind"] == "TOOL_CALL"]
    for expected in ("get_overview", "get_units", "get_cities",
                     "get_available_research", "get_available_production",
                     "set_research", "set_city_production", "found_city",
                     "fortify", "move_unit", "purchase", "end_turn"):
        assert expected in tools, f"{expected} never rehearsed: {sorted(set(tools))}"
    # every tool call has its result pair (the log's replay contract)
    results = [r for r in records if r["kind"] == "TOOL_RESULT"]
    assert len(results) == len([r for r in records
                                if r["kind"] == "TOOL_CALL"])


def test_dispatch_target_turn_rules():
    """Live-learned runs 002/003/005: (1) an engaged lease for us IS the
    turn; (2) no lease + our turn not active = mid-AI-transition, our hook
    is ahead, drive TURN; (3) no lease + our turn ACTIVE = the attach
    case (hook fired before SetPuppet), drive TURN+1."""
    from civ_arena.game.civ6.live_driver import _target_turn

    # rule 1: OUR lease engaged on the parked current turn
    assert _target_turn(
        {"TURN": 3, "PUPPET_ACTIVE": True, "LEASE_PLAYER": 0,
         "LEASE_TURN": 3, "TURN_ACTIVE": True}, 0) == 3
    # rule 1 beats rule 3 even when the turn is active
    assert _target_turn(
        {"TURN": 3, "PUPPET_ACTIVE": True, "LEASE_PLAYER": 0,
         "LEASE_TURN": 3, "TURN_ACTIVE": False}, 0) == 3
    # rule 3: we JUST drove 7 ourselves; the AI is still playing it —
    # our next hook fires at 8 (targeting 7 would wait for a dead hook)
    assert _target_turn(
        {"TURN": 7, "PUPPET_ACTIVE": False, "LEASE_PLAYER": -1,
         "LEASE_TURN": -1, "TURN_ACTIVE": False}, 0, last_driven=7) == 8
    # rule 4: mid-transition into OUR next turn — hook imminent at TURN
    assert _target_turn(
        {"TURN": 8, "PUPPET_ACTIVE": False, "LEASE_PLAYER": -1,
         "LEASE_TURN": -1, "TURN_ACTIVE": False}, 0, last_driven=7) == 8
    # rule 3: attach case — parked local turn, hook already fired
    assert _target_turn(
        {"TURN": 1, "PUPPET_ACTIVE": False, "LEASE_PLAYER": -1,
         "LEASE_TURN": -1, "TURN_ACTIVE": True}, 0) == 2


async def test_attach_does_not_reinject_over_live_mod():
    """Live-learned run 004: re-executing the mod on attach RETIRES the
    running instance — and any ENGAGED lease on the engine's parked turn
    dies with it. A capable live mod must be VERIFIED, not re-injected."""
    mod = FakeMod()  # injected by default (a prior attach left it live)
    adapter, server = await _adapter_with(mod)
    try:
        doc = await adapter.inject_mod("-- PUPPET_PLAYERS = {}\n")
        assert doc["present"] is True
        assert mod.injections == 0, "verify-first must not re-execute"
    finally:
        await server.stop()

    # the fresh-attach path still injects exactly once
    mod2 = FakeMod(injected=False)
    adapter, server = await _adapter_with(mod2)
    try:
        doc = await adapter.inject_mod("-- PUPPET_PLAYERS = {}\n")
        assert doc["present"] is True and mod2.injections == 1
        assert doc["supports_command_diff"] is True
    finally:
        await server.stop()


def test_dispatch_targeting_rules_and_blockers():
    """The blocker housekeeping runs at lease start and clears the
    civic/policy pair (the fresh-game CODE_OF_LAWS wedge, run 011)."""
    from civ_arena.game.civ6 import live_driver

    assert live_driver._target_turn(
        {"TURN": 9, "PUPPET_ACTIVE": False, "LEASE_PLAYER": -1,
         "LEASE_TURN": -1, "TURN_ACTIVE": True}, 0) == 10
    assert live_driver._last_deact_turn(
        ["16|HOOK_DEACT|0", "16|HOOK_ENTER|0"]) == 16
    assert live_driver._last_deact_turn([]) is None


async def test_ensure_research_resolves_the_research_blocker():
    """M17d: game four's turn-17 freeze — completed research with no
    follow-up parks ENDTURN_BLOCKING_RESEARCH on the cycle. The
    housekeeping sets the preference-first available tech, and the wire
    clears the pending blocker with it (never leaves the slot empty
    while techs remain)."""
    from civ_arena.game.civ6 import live_driver

    adapter, server = await _adapter_with()
    try:
        await adapter.setup({})
        await adapter.begin_phase(0, 1)
        mod = server.mod
        assert mod is not None
        mod.players[0]["researching"] = ""
        mod.pending_blockers = ["BLOCKING|ENDTURN_BLOCKING_RESEARCH"]

        rows = _split_lines(
            await adapter.write_raw(lua_translator.blocker_query()))
        blockers = [r for r in rows if r.startswith("BLOCKING|")]
        assert blockers == ["BLOCKING|ENDTURN_BLOCKING_RESEARCH"]

        await live_driver._ensure_research(adapter, 0, turn=17)
        # the preference order's first offerable tech landed
        assert mod.players[0]["researching"] == "MINING"
        assert mod.pending_blockers == [], "the blocker cleared with it"

        # a filled slot is left alone (no redundant set_research)
        again = await adapter.observe(ObserveRequest(
            kind=ObserveKind.OVERVIEW, player_id=0))
        assert again["players"]["0"]["researching"]
        await live_driver._ensure_research(adapter, 0, turn=18)
        assert mod.players[0]["researching"] == "MINING"

        # exhausted preference falls back to sorted-first, never empty
        mod.players[0]["researching"] = ""
        mod.TECHS = {"ZEBRA_HUSBANDRY": 30}  # type: ignore[assignment]
        await live_driver._ensure_research(adapter, 0, turn=19)
        assert mod.players[0]["researching"] == "ZEBRA_HUSBANDRY"
    finally:
        await server.stop()


def test_llm_client_string_content_normalizes():
    """Provider-shape tolerance (Z.AI glm-g1): plain-string content wraps
    into a text block; garbage still fails loud."""
    from civ_arena.agents.llm.client import _normalize_blocks

    assert _normalize_blocks("plain") == [{"type": "text", "text": "plain"}]
    assert _normalize_blocks(
        ["str", {"type": "text", "text": "b"}]) == [
        {"type": "text", "text": "str"}, {"type": "text", "text": "b"}]
    assert _normalize_blocks([{"notype": 1}]) is None
    assert _normalize_blocks(42) is None
    assert _normalize_blocks(None) is None


def test_set_city_production_submits_hash_for_subsequent_readback():
    """The engine applies requests after the Lua chunk; the adapter verifies later."""
    lua = lua_translator.set_city_production("c0:1", "MONUMENT")
    assert "PRODUCTION_REQUEST|" in lua
    assert "tostring(item.Hash)" in lua
    assert "GetCurrentProductionTypeHash" not in lua


def test_cities_read_uses_production_type_hash():
    """B2 content pin: the queue read resolves the hash through
    GameInfo.Units/Buildings exactly as the shipped UI consumers do."""
    lua = lua_translator.cities_read()
    assert "GetCurrentProductionTypeHash" in lua
    assert "GetCurrentProductionType(" not in lua
    assert "GameInfo.Units()" in lua and "GameInfo.Buildings()" in lua
    assert "h ~= 0" in lua                    # 0 = nothing building


def test_current_production_read_shape():
    """B2: the housekeeping gate read — CURPROD|<hash>, 0 = idle,
    -1 = unknown city."""
    lua = lua_translator.current_production_read("c0:1")
    assert "CURPROD|" in lua
    assert "GetCurrentProductionTypeHash" in lua


def test_fake_mod_curprod_tracks_queue():
    """B2 rehearsal fidelity: a queued fake city answers a non-zero
    hash (housekeeping must skip it); an empty queue answers 0."""
    from civ_arena.game.civ6.fake_tuner_server import FakeMod

    mod = FakeMod(injected=True)
    city = next(iter(mod.cities.values()))
    read = ("local me = Game.GetLocalPlayer() "
            "local pCity = CityManager.GetCity(me, 1) "
            "local bq = pCity:GetBuildQueue() "
            "local h = bq:GetCurrentProductionTypeHash() "
            "print('CURPROD|' .. tostring(h)) print('---END---')")
    rows = mod.respond(read)
    assert rows and rows[0] == "CURPROD|0"
    mod.respond("-- arena:tool=set_city_production\n"
                "local me = Game.GetLocalPlayer() "
                "local pCity = CityManager.GetCity(me, 1) "
                "CityManager.RequestOperation(pCity, CityOperationTypes.BUILD,"
                " tParams)")
    city["queue"] = "MONUMENT"   # the act handler's effect, set directly
    rows = mod.respond(read)
    assert rows and rows[0] == f"CURPROD|{mod._production_hash('MONUMENT')}"
