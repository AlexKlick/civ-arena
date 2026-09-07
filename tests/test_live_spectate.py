"""phase_spectate over the real adapter + FakeTunerServer socket path.

The load-bearing invariants: the harness NEVER acts (no puppet arming, no
local-player switch, no blocker housekeeping, no popups, no desktop input
— pinned three ways: a monkeypatched Controller that fails on
construction, a wire-level forbidden-substring scan over every received
command, and a static source pin on the phase function), round events
alternate strictly, and the summary carries the input-free proof
(command_census.game_writes == 0).
"""

import json
from pathlib import Path

import pytest

from civ_arena.config import parse_config
from civ_arena.game.civ6 import live_driver as ld
from civ_arena.game.civ6.fake_tuner_server import FakeMod, FakeTunerServer
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from civ_arena.game.civ6.spectate_capture import SpectateLimits

SPECTATE_DOC = {
    "match": {"match_id": "spectate-live-test", "seed": 1,
              "adapter": "firetuner"},
    "spectate": {"operator": "alexk"},
    "agents": [],
}

MOD_LUA = ld.MOD_DEFAULT.read_text(encoding="utf-8")

# every game-affecting command shape the harness must NEVER send in
# spectate mode (the allowlist side: polls, observes, ambient recorders)
FORBIDDEN_SUBSTRINGS = (
    "SetPuppet", "SetLocalPlayer", "RequestAction", "ACTION_ENDTURN",
    "RestoreUnit", "FreezeUnit", "Simulate.", "RequestPolicyChanges",
    "SetProgressingCivic", "UNLOCK_POLICIES", "GuardedHandoff",
    "DiffSinceLast", "Puppeteer.Release", "UI.RequestAction",
)


class _NoInput:
    def __init__(self, *args, **kwargs) -> None:
        pytest.fail("spectate attempted to construct a desktop Controller")


def _spectate_spec(doc: dict | None = None) -> object:
    return parse_config(doc or SPECTATE_DOC)


async def run_spectate(tmp_path: Path, mod: FakeMod, turns: int = 3,
                       turn_budget_s: float | None = None,
                       match_s: float = 60.0, poll_s: float = 0.01):
    server = FakeTunerServer(mod=mod)
    port = await server.start()
    adapter = FireTunerAdapter("127.0.0.1", port)
    run_dir = tmp_path / "run"
    doc = json.loads(json.dumps(SPECTATE_DOC))  # deep copy
    if turn_budget_s is not None:
        doc["spectate"]["turn_budget_s"] = turn_budget_s
    try:
        rc = await ld.phase_spectate(
            _spectate_spec(doc), adapter, run_dir, turns, MOD_LUA,
            limits=SpectateLimits(poll_s=poll_s, heartbeat_s=3600.0,
                                  match_s=match_s))
        events = [json.loads(line)
                  for line in (run_dir / "events.jsonl").read_text()
                  .splitlines()]
        summary = json.loads((run_dir / "summary.json").read_text())
        return rc, events, summary, server
    finally:
        await adapter.teardown()
        await server.stop()


def _spectate_mod(polls: int = 3, **overrides) -> FakeMod:
    cfg = {"human_seat": 0, "ai_seats": [1], "polls_per_human_turn": polls}
    cfg.update(overrides)
    return FakeMod(spectate=cfg, ambient_diffs=True)


# -- happy path ---------------------------------------------------------------

async def test_spectate_three_rounds_clean(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ld.ui_control, "Controller", _NoInput)
    mod = _spectate_mod()
    rc, events, summary, server = await run_spectate(tmp_path, mod, turns=3)
    assert rc == 0
    assert summary["clean"] is True
    assert summary["phase"] == "spectate"
    assert summary["completed_rounds"] == 3
    assert summary["requested_rounds"] == 3
    assert summary["violations_total"] == 0
    assert summary["operator"] == "alexk"
    assert summary["command_census"]["game_writes"] == 0
    assert summary["command_census"]["recorder_commands"] > 0
    assert summary["cleanup"]["status"] == "disconnect_only_no_game_actions_no_leases"
    assert summary["identity"]["commit"]
    assert summary["attach"]["attached_mid_turn"] is True

    kinds = [e["kind"] for e in events]
    assert kinds[0] == "MATCH_START" and kinds[-1] == "MATCH_END"
    starts = [e for e in events if e["kind"] == "HUMAN_TURN_START"]
    ends = [e for e in events if e["kind"] == "HUMAN_TURN_END"]
    snaps = [e for e in events if e["kind"] == "SPECTATOR_SNAPSHOT"]
    assert len(starts) == len(ends) == len(snaps) == 3
    turns_seq = [e["turn"] for e in starts]
    assert turns_seq == sorted(set(turns_seq)) and len(turns_seq) == 3
    assert all(e["visibility_scope"] == "spectator" for e in starts + ends + snaps)
    # first round is the attach round
    assert starts[0]["window"] == "attach"
    assert all(s["window"] == "turn_start" for s in starts[1:])
    # the AI's real move lands in every FULL round's ambient manifest
    # (round 1 is the attach round — its windows opened at attach as the
    # baseline, before the AI's next turn)
    assert all(r["ai_ambient_rows"].get("1", 0) >= 1
               for r in summary["per_round"][1:])
    assert all(s["digest"]["consistent"] is True for s in snaps)
    # zero action kinds anywhere
    assert not [e for e in events if e["kind"] in (
        "TOOL_CALL", "TOOL_RESULT", "LEASE_GRANT", "LEASE_RELEASE",
        "VIOLATION", "UNAUTHORIZED_TOOL_CALL")]
    # the harness never sent a game-affecting command on the wire
    for command in server.received_commands:
        for forbidden in FORBIDDEN_SUBSTRINGS:
            assert forbidden not in command, (
                f"spectate sent forbidden command shape {forbidden!r}: "
                f"{command[:120]}")
    # and the fake engine was never leased or puppeted
    assert mod.lease is None
    assert mod.puppets == {}


async def test_spectate_overrun_is_audit_only(tmp_path) -> None:
    mod = _spectate_mod()
    rc, events, summary, _ = await run_spectate(
        tmp_path, mod, turns=2, turn_budget_s=0.0001)
    assert rc == 0  # the budget NEVER aborts the run
    assert summary["clean"] is True
    assert all(r["overrun"] is True for r in summary["per_round"])
    assert all(e["overrun"] is True for e in events
               if e["kind"] == "HUMAN_TURN_END")
    assert any(e.get("audit") == "human_turn_overrun" for e in events
               if e["kind"] == "HEARTBEAT")


async def test_spectate_match_timeout_aborts_cleanly(tmp_path) -> None:
    mod = _spectate_mod()
    rc, events, summary, _ = await run_spectate(
        tmp_path, mod, turns=5, match_s=0.02, poll_s=0.05)
    assert rc == 2
    assert summary["clean"] is False
    assert summary["failure_reason"] == "match timeout"
    assert events[-1]["kind"] == "MATCH_END"
    assert summary["command_census"]["game_writes"] == 0
    assert mod.lease is None


async def test_spectate_refuses_driven_config(tmp_path) -> None:
    doc = {
        "match": {"match_id": "x", "seed": 1, "adapter": "firetuner"},
        "agents": [{"agent_id": "a0", "player_id": 0, "policy": "turtler"}],
    }
    server = FakeTunerServer(mod=FakeMod())
    port = await server.start()
    adapter = FireTunerAdapter("127.0.0.1", port)
    try:
        with pytest.raises(RuntimeError, match="spectate: block"):
            await ld.phase_spectate(
                parse_config(doc), adapter, tmp_path / "run", 1, MOD_LUA,
                limits=SpectateLimits(0.01, 3600.0, 60.0))
    finally:
        await adapter.teardown()
        await server.stop()


# -- structural no-input pin ---------------------------------------------------

def test_phase_spectate_source_has_no_acting_paths() -> None:
    source = Path(ld.__file__).read_text(encoding="utf-8")
    start = source.index("async def phase_spectate")
    end = source.index("async def ", start + 10)
    body = source[start:end]
    for forbidden in ("ui_control.Controller", "set_puppet",
                      "activate_human_seat", "request_end_turn",
                      "_resolve_blockers", "_dismiss_popups",
                      "begin_phase", "end_phase", "adapter.act",
                      "write_raw"):
        assert forbidden not in body, (
            f"phase_spectate must never contain {forbidden!r}")
