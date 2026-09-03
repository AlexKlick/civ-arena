"""B1: live-run replay mode — turn normalization, undriven-seat exclusion,
and the structural (hash-excluding) comparison.

The live driver's run differs from a simulated one in three structural
ways (all live-proven against runs/live-tourney-glm53, 2026-09-03):
it attaches mid-game at engine turn N (the sim always starts at 1), its
undriven seats are the ENGINE's own players outside the referee, and its
hashes are Civ VI engine digests the sim cannot re-derive. Live mode
normalizes the first two and excludes the hash fields; simulator mode
must remain byte-for-byte unchanged (tests/test_replay.py pins that).
"""

from __future__ import annotations

import json

from civ_arena.arena.coordinator import Arena
from civ_arena.replay import _live_turn_offset, _load_records, _shift_turns, _strip, replay_run
from test_match_end_to_end import duel_spec


def _mutate_into_live_shape(live_dir, offset: int = 1) -> None:
    """Bend a simulated run's log into LIVE shape in place: shift the
    driven-seat turns up (a live dispatch attaches at engine turn
    N=offset+1), strip the second seat's recorded calls (live seat 1 is
    the engine's own AI — nothing it did entered the referee), rewrite
    the hashes to live-style engine digests, and mark the summary."""
    path = live_dir / "events.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()
               if line]
    driven_agents = {rec["agent_id"] for rec in records
                     if rec["kind"] == "TOOL_CALL"}
    # strip the LAST agent alphabetically (deterministic seat-1 stand-in)
    undriven = sorted(driven_agents)[-1] if len(driven_agents) > 1 else None
    out = []
    for rec in records:
        # a live log carries ONLY the driven seat's referee records — the
        # undriven seat is the engine's own player, entirely outside
        if undriven and rec.get("agent_id") == undriven \
                and rec["kind"] in ("TOOL_CALL", "TOOL_RESULT", "AMBIENT",
                                    "TURN_END"):
            continue
        if rec["kind"] in ("TOOL_CALL", "TOOL_RESULT", "AMBIENT",
                           "TURN_END") and isinstance(rec.get("turn"), int):
            rec = dict(rec, turn=rec["turn"] + offset)
        if rec["kind"] == "TOOL_RESULT":
            rec = dict(rec, after_state_hash="liveenginehash")
        if rec["kind"] == "TURN_END":
            rec = dict(rec, state_hash="liveenginehash")
        out.append(rec)
    path.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in out) + "\n")
    summary = json.loads((live_dir / "summary.json").read_text())
    summary["phase"] = "dispatch"
    (live_dir / "summary.json").write_text(json.dumps(summary, sort_keys=True))


async def test_live_replay_normalizes_turn_offset_and_excludes_undriven(
        tmp_path):
    live_dir = tmp_path / "live"
    spec = duel_spec("replay-live", max_turns=25)
    await Arena(live_dir, spec).run()
    _mutate_into_live_shape(live_dir)

    # live mode (auto-detected via the summary's "phase" key). The
    # undriven seat's stripped calls mean the replayed BOARD cannot
    # reproduce the original (the engine's seat shaped it) — the honest
    # certificate is the SKELETON: every recorded TOOL_CALL re-issues
    # in order with an identical args_digest. Compare the TOOL_CALL
    # subsequences directly (a row-wise zip misaligns downstream of any
    # real boundary and would cascade).
    result = await replay_run(live_dir, spec, tmp_path / "replay-live")
    assert result["live_mode"] is True
    live_records = _load_records(live_dir / "events.jsonl")
    offset = _live_turn_offset(live_records)
    live_calls = [_shift_turns(r, -offset) for r in _strip(live_records,
                                                           live=True)
                  if r[0] == "TOOL_CALL"]
    driven_pids = {r[2] for r in live_calls}
    rep_calls = [r for r in _strip(
        _load_records(tmp_path / "replay-live" / "events.jsonl"),
        live=True)
        if r[0] == "TOOL_CALL" and r[2] in driven_pids]
    assert rep_calls == live_calls
    # the sim-legality boundary exists and is itemized (the replayed
    # board diverged, so some statuses differ) — documented, not silent
    assert result["divergences"], "expected itemized boundary rows"
    # the turn axis normalized: paired rows in the report never name
    # mismatched turns between live and replayed
    import re

    for line in result["divergences"][:5]:
        live_turn = re.search(r"live=.*?\bt(\d+)", line).group(1)
        rep_turn = re.search(r"replayed=.*?\bt(\d+)", line).group(1)
        assert live_turn == rep_turn, line

    # the SAME records under forced simulator semantics diverge too —
    # but from the UNNORMALIZED turn offset (a genuinely different code
    # path: hash fields compared, no shift, no seat filter)
    result = await replay_run(live_dir, spec, tmp_path / "replay-sim",
                              live=False)
    assert result["live_mode"] is False
    assert not result["identical"]
    assert result["first_divergence"] == 0  # turn 2 vs 1 at comparable-event 0


async def test_live_replay_detects_tampered_tool_call(tmp_path):
    """Live mode is still a tamper certificate: a changed arg diverges in
    the skeleton even though hash fields are excluded."""
    live_dir = tmp_path / "live"
    spec = duel_spec("replay-live-tamper", max_turns=25)
    await Arena(live_dir, spec).run()
    _mutate_into_live_shape(live_dir)
    path = live_dir / "events.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()
               if line]
    for rec in records:
        if rec["kind"] == "TOOL_CALL" and rec.get("tool") == "move_unit":
            rec["args"]["dest"] = "9,9"
            break
    else:
        raise AssertionError("no move_unit call found to tamper with")
    path.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n")

    result = await replay_run(live_dir, spec, tmp_path / "replay")
    assert result["live_mode"] is True
    assert not result["identical"]
    assert result["first_divergence"] is not None
