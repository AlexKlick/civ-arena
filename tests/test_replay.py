"""Replay: every intermediate hash matches; tampered logs diverge loudly."""

from __future__ import annotations

import json

from civ_arena.arena.coordinator import Arena
from civ_arena.config import AgentSpec, ChaosSpec, MatchSpec
from civ_arena.replay import replay_run
from test_match_end_to_end import duel_spec


async def test_replay_final_hash_equals_live(tmp_path):
    live_dir = tmp_path / "live"
    arena = Arena(live_dir, duel_spec("replay-duel", max_turns=30))
    live_summary = await arena.run()
    assert live_summary["violations_total"] == 0

    result = await replay_run(live_dir, duel_spec("replay-duel", max_turns=30),
                              tmp_path / "replay")
    assert result["identical"], f"diverged at {result['first_divergence']}"
    assert result["replayed_final_hash"] == live_summary["final_state_hash"]
    assert result["live_final_hash"] == live_summary["final_state_hash"]


async def test_replay_intermediate_hashes_match(tmp_path):
    """The strip comparison covers EVERY TOOL_RESULT after_state_hash, so
    identical=True means no intermediate state ever diverged."""
    live_dir = tmp_path / "live"
    arena = Arena(live_dir, duel_spec("replay-int", max_turns=25))
    await arena.run()
    result = await replay_run(live_dir, duel_spec("replay-int", max_turns=25),
                              tmp_path / "replay")
    assert result["identical"]
    # there were substantive hashed events to compare
    assert result["live_events"] > 100


async def test_replay_detects_tampered_log(tmp_path):
    live_dir = tmp_path / "live"
    spec = duel_spec("replay-tamper", max_turns=25)
    arena = Arena(live_dir, spec)
    await arena.run()

    # tamper: change one recorded move destination (args only; digest stays)
    path = live_dir / "events.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines() if line]
    for rec in records:
        if rec["kind"] == "TOOL_CALL" and rec.get("tool") == "move_unit":
            rec["args"]["dest"] = "0,0"
            break
    else:
        raise AssertionError("no move_unit call found to tamper with")
    path.write_text("\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n")

    result = await replay_run(live_dir, spec, tmp_path / "replay")
    assert not result["identical"]
    assert result["first_divergence"] is not None


async def test_replay_reproduces_chaos_and_violations(tmp_path):
    spec = MatchSpec(
        match_id="replay-chaos", seed=777, max_turns=25, adapter="simulator",
        watchdog_mode="flag_and_continue", violation_limit=50, checkpoint_every=5,
        agents=[
            AgentSpec(agent_id="roman", player_id=0, policy="expansionist", seed=11),
            AgentSpec(agent_id="korea", player_id=1, policy="turtler", seed=22),
        ],
        chaos=[ChaosSpec("move_uncommanded_unit", offset=4),
               ChaosSpec("steal_gold", offset=9)],
    )
    live_dir = tmp_path / "live"
    arena = Arena(live_dir, spec)
    summary = await arena.run()
    assert summary["violations_total"] >= 2

    result = await replay_run(live_dir, spec, tmp_path / "replay")
    assert result["identical"], (
        f"chaos replay diverged at {result['first_divergence']} — chaos is not "
        f"deterministic across runs"
    )
    assert result["summary"]["violations_total"] == summary["violations_total"]
