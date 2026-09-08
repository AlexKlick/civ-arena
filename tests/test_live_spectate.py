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


# -- validate_spectate ---------------------------------------------------------

async def _rerun(tmp_path, turns: int = 2) -> Path:
    import shutil

    from civ_arena.game.civ6.validate_run import validate  # noqa: F401
    run_dir = tmp_path / "run"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    mod = _spectate_mod()
    rc, _, _, _ = await run_spectate(tmp_path, mod, turns=turns)
    assert rc == 0
    return run_dir


async def test_validate_spectate_passes_on_fake_run(tmp_path) -> None:
    from civ_arena.game.civ6.validate_run import validate
    run_dir = await _rerun(tmp_path, turns=2)
    result = validate(run_dir, rounds=2, require_live=False)
    assert result["status"] == "PASS", result["errors"]
    assert result["operator"] == "alexk"


def _rewrite_events(run_dir: Path, mutate) -> None:
    lines = (run_dir / "events.jsonl").read_text().splitlines()
    lines = mutate(lines)
    (run_dir / "events.jsonl").write_text("\n".join(lines) + "\n")


def _rewrite_summary(run_dir: Path, mutate) -> None:
    summary = json.loads((run_dir / "summary.json").read_text())
    summary = mutate(summary) or summary
    (run_dir / "summary.json").write_text(json.dumps(summary, sort_keys=True))
    # event/summary equality: the MATCH_END event carries the old summary;
    # resync it so each tamper tests exactly ONE rule
    lines = (run_dir / "events.jsonl").read_text().splitlines()
    last = json.loads(lines[-1])
    last["summary"] = summary
    lines[-1] = json.dumps(last, sort_keys=True)
    (run_dir / "events.jsonl").write_text("\n".join(lines) + "\n")


def _drop_last_snapshot(lines: list[str]) -> list[str]:
    parsed = [json.loads(row) for row in lines]
    snap_i = max(i for i, r in enumerate(parsed)
                 if r["kind"] == "SPECTATOR_SNAPSHOT")
    del parsed[snap_i]
    return [json.dumps(r, sort_keys=True) for r in parsed]


def _swap_last_start_and_snapshot(lines: list[str]) -> list[str]:
    parsed = [json.loads(row) for row in lines]
    snap_i = max(i for i, r in enumerate(parsed)
                 if r["kind"] == "SPECTATOR_SNAPSHOT")
    start_i = max(i for i, r in enumerate(parsed)
                  if r["kind"] == "HUMAN_TURN_START")
    if start_i < snap_i:
        parsed[snap_i], parsed[start_i] = parsed[start_i], parsed[snap_i]
    return [json.dumps(r, sort_keys=True) for r in parsed]


async def test_validate_spectate_tamper_matrix(tmp_path) -> None:
    from civ_arena.game.civ6.validate_run import validate

    # (a) drop the last SPECTATOR_SNAPSHOT
    run_dir = await _rerun(tmp_path)
    _rewrite_events(run_dir, _drop_last_snapshot)
    result = validate(run_dir, rounds=2, require_live=False)
    assert result["status"] == "FAIL"
    assert any("round event counts" in e or "interleave" in e
               for e in result["errors"])

    # (b) flip clean in the summary
    run_dir = await _rerun(tmp_path)
    _rewrite_summary(run_dir, lambda s: {**s, "clean": False})
    result = validate(run_dir, rounds=2, require_live=False)
    assert result["status"] == "FAIL" and "clean outcome" in result["errors"]

    # (c) inject a TOOL_CALL before MATCH_END
    run_dir = await _rerun(tmp_path)
    _rewrite_events(run_dir, lambda ls: ls[:-1] + [
        json.dumps({"schema": 1, "seq": 999, "kind": "TOOL_CALL",
                    "ts": "2026-09-07T00:00:00+00:00", "match_id": "x",
                    "game_instance_id": "x", "turn": 1, "phase_player_id": 0,
                    "player_id": 0, "agent_id": "a", "tool": "move_unit",
                    "args": {}, "args_digest": "d",
                    "visibility_scope": "spectator"}, sort_keys=True)] + [ls[-1]])
    result = validate(run_dir, rounds=2, require_live=False)
    assert result["status"] == "FAIL"
    assert any("TOOL_CALL" in e for e in result["errors"])

    # (d) break seq contiguity
    run_dir = await _rerun(tmp_path)
    _rewrite_events(run_dir, lambda ls: ls[:3] + ls[4:])
    result = validate(run_dir, rounds=2, require_live=False)
    assert result["status"] == "FAIL" and "event sequence" in result["errors"]

    # (e) reorder a START after its SNAPSHOT
    run_dir = await _rerun(tmp_path)
    _rewrite_events(run_dir, _swap_last_start_and_snapshot)
    result = validate(run_dir, rounds=2, require_live=False)
    assert result["status"] == "FAIL"
    assert any("interleave" in e for e in result["errors"])


# -- CAP-01: interval semantics, attach cases, roster, transport audit -------

async def test_attach_active_human_without_prior_enter_is_partial_not_invented(
        tmp_path) -> None:
    """Default fake = attach mid-human-turn with no prior ENTER observed.
    Round 1 must be labeled a PARTIAL attach interval — never a claimed
    boundary state (we cannot bound when the turn began relative to our
    reads)."""
    mod = _spectate_mod()
    rc, events, summary, _ = await run_spectate(tmp_path, mod, turns=2)
    assert rc == 0
    starts = [e for e in events if e["kind"] == "HUMAN_TURN_START"]
    snaps = [e for e in events if e["kind"] == "SPECTATOR_SNAPSHOT"]
    assert starts[0]["boundary"] == "attach"
    assert starts[0]["state_at_boundary"] is False  # partial, not invented
    assert starts[0]["observed_at"] and starts[0]["source_cursor"]
    assert snaps[0]["census_phase"] == "read_time"
    assert snaps[0]["state_at_boundary"] is False
    # later rounds are directly observed hooks and MAY claim boundary state
    assert starts[1]["boundary"] == "hook_observed"
    assert starts[1]["state_at_boundary"] is True
    assert summary["per_round"][0]["boundary"] == "attach"


async def test_attach_during_ai_turn_does_not_replay_stale_human_history(
        tmp_path) -> None:
    """Attached between turns with a STALE ring: every pre-read entry is
    dropped as history (audited), and round 1 opens only at the next
    UNAMBIGUOUS fresh HOOK_ENTER — no turn from before or during the
    attach is ever recorded as an observed round."""
    mod = _spectate_mod(polls=2, attach_turn_active=False)
    mod.trace = ["1|HOOK_ENTER|0", "1|HOOK_DEACT|0"]
    rc, events, summary, _ = await run_spectate(
        tmp_path, mod, turns=1, poll_s=0.02)
    assert rc == 0
    # the drain audited the discarded history
    assert any(e.get("audit") == "attach_history_discarded" for e in events)
    starts = [e for e in events if e["kind"] == "HUMAN_TURN_START"]
    assert len(starts) == 1
    # round 1 opens at the first fresh ENTER observed AFTER the drain
    # (turn 2's ENTER arrives in the second trace read — genuinely new);
    # the drained pre-attach turn-1 history stays unrecorded
    assert starts[0]["turn"] == 2
    assert starts[0]["boundary"] == "hook_observed"
    assert not any(e["kind"] == "HUMAN_TURN_START" and e["turn"] == 1
                   for e in events)
    assert summary["attach"]["attached_mid_turn"] is False


async def test_multiple_turn_boundaries_in_one_poll_do_not_forge_historical_snapshots(
        tmp_path) -> None:
    """One poll batch containing [human DEACT, AI turns, next human ENTER]:
    the EARLIER boundary's events are written with reads that happened
    AFTER the later entries existed — they must carry
    state_at_boundary=False; only the batch's LAST boundary may claim
    boundary state."""
    mod = _spectate_mod(polls=1, advance_on=["trace"])
    rc, events, summary, _ = await run_spectate(
        tmp_path, mod, turns=2, poll_s=0.02)
    assert rc == 0
    ends = [e for e in events if e["kind"] == "HUMAN_TURN_END"]
    starts = [e for e in events if e["kind"] == "HUMAN_TURN_START"]
    # turns=2 => exactly two rounds (the attach round counts as round 1);
    # every TRACE read that advances rolls a whole round into one batch
    assert len(ends) == 2 and len(starts) == 2
    # a batch boundary EARLIER than the batch's last entry: its reads ran
    # after later entries existed — the forged-historical-state marker
    # must be False
    assert ends[0]["state_at_boundary"] is False
    assert ends[0]["window_start_cursor"] == starts[0]["source_cursor"]
    assert ends[0]["window_end_cursor"] == ends[0]["source_cursor"]
    # a batch's LAST boundary may claim boundary state
    assert any(s["state_at_boundary"] is True
               for s in starts if s["boundary"] == "hook_observed")


async def test_sparse_player_ids_and_unconfigured_actor_classes_have_coverage_status(
        tmp_path) -> None:
    """The OVERVIEW read discovers the REAL roster — sparse ids beyond the
    configured set (a city-state at pid 63) get explicit coverage status;
    the configured set is a claim, the board is the truth."""
    mod = _spectate_mod()
    mod.players[63] = {"gold": 0, "researching": "", "researched": []}
    rc, events, summary, _ = await run_spectate(tmp_path, mod, turns=1)
    assert rc == 0
    roster = next(e for e in events if e.get("audit") == "roster_discovery")
    assert roster["discovered"] == [0, 1, 63]
    assert roster["observed"] == [0, 1]
    assert roster["unconfigured_discovered"] == [63]
    assert roster["actor_classes"]["63"] == "minor_or_unconfigured"
    assert roster["actor_classes"]["0"] == "observed_major"
    assert summary["roster"]["unconfigured_discovered"] == [63]


async def test_bootstrap_and_teardown_are_inside_spectator_command_audit(
        tmp_path) -> None:
    """setup/inject/teardown are lifecycle ops — permitted, counted
    separately from recorder commands, and the capability census shows
    zero rejected attempts and zero game writes."""
    mod = _spectate_mod()
    rc, _, summary, _ = await run_spectate(tmp_path, mod, turns=1)
    assert rc == 0
    census = summary["command_census"]
    assert census["recorder_lifecycle"] == 3  # setup + inject + teardown
    assert census["recorder_commands"] > 0
    assert census["rejected"] == 0
    assert census["game_writes"] == 0
    assert census["status_polls"] >= 1 and census["trace_polls"] >= 1
    assert census["digest_reads"] >= 2  # the census bracket


async def test_trace_ring_wrap_declares_gap_and_gap_inferred_round(
        tmp_path) -> None:
    """A ring small enough to evict the cursor between polls: every
    subsequent round is opened as boundary=gap_inferred with capture-gap
    audits — the turns are recorded COARSELY and honestly, never silently
    lost or fabricated as direct observations."""
    mod = _spectate_mod(polls=1, trace_ring_cap=4)
    rc, events, summary, _ = await run_spectate(
        tmp_path, mod, turns=3, poll_s=0.02)
    assert rc == 0
    starts = [e for e in events if e["kind"] == "HUMAN_TURN_START"]
    assert starts[0]["boundary"] in ("attach", "gap_inferred")
    gap_rounds = [s for s in starts if s["boundary"] == "gap_inferred"]
    assert gap_rounds, "wrap must produce gap-inferred rounds"
    assert all(s["state_at_boundary"] is False for s in gap_rounds)
    gap_audits = [e for e in events if e.get("audit") == "trace_gap"]
    assert gap_audits and all("ring_size" in a for a in gap_audits)
    assert summary["trace_gaps"] >= 1
    assert summary["trace_generations"] >= 0  # epoch resets counted apart
