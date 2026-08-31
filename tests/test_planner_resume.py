"""M16a: resume without amnesia — the planner belief journal.

The strong pin: a match resumed from a checkpoint with a FRESH runtime is
BIT-IDENTICAL to its uninterrupted twin (same final_state_hash, same
per-turn tool-call sequence for the resumed turns) — impossible before
M16a, when the fresh planner started blind. Rebuild equality is pinned
directly: a runtime restored from the journal holds exactly the belief
the original runtime held at that turn (deep-compare of every belief
collection plus the option state).
"""

from __future__ import annotations

import copy
import json
from typing import Any

from civ_arena.agents.runtime import AgentProfile, build_runtime
from civ_arena.arena.checkpoints import CheckpointState
from civ_arena.arena.coordinator import Arena
from civ_arena.config import AgentSpec, MatchSpec
from civ_arena.planner.journal import PlannerJournal
from civ_arena.planner.runtime import PlannerRuntime


def spec_for(match_id: str, max_turns: int = 8) -> MatchSpec:
    return MatchSpec(
        match_id=match_id, seed=424242, max_turns=max_turns, adapter="simulator",
        watchdog_mode="flag_and_continue", violation_limit=5, checkpoint_every=2,
        agents=[
            AgentSpec(agent_id="planner", player_id=0, policy="planner", seed=7),
            AgentSpec(agent_id="turtler", player_id=1, policy="turtler", seed=22),
        ],
    )


def runtimes(budget: int = 4) -> dict[int, Any]:
    return {
        0: PlannerRuntime(0, 7, budget=budget),
        1: build_runtime(AgentProfile(agent_id="turtler", player_id=1,
                                      policy="turtler", seed=22)),
    }


def _belief_snapshot(bot: PlannerRuntime) -> dict[str, Any]:
    b = bot.belief
    return {
        "own_player": copy.deepcopy(b.own_player),
        "public_players": copy.deepcopy(b.public_players),
        "own_units": copy.deepcopy(b.own_units),
        "own_cities": copy.deepcopy(b.own_cities),
        "foreign_units": copy.deepcopy(b.foreign_units),
        "foreign_cities": copy.deepcopy(b.foreign_cities),
        "tiles": copy.deepcopy(b.tiles),
        "turn": b.turn,
        "active": bot.active,
        "chosen_at_turn": bot.chosen_at_turn,
    }


async def test_resumed_match_is_bit_identical(tmp_path):
    spec = spec_for("planner-journal-id", max_turns=40)
    arena = Arena(tmp_path / "run", spec, runtimes=runtimes())
    summary = await arena.run()
    assert summary["final_turn"] == 40 and summary["violations_total"] == 0
    first_log = (tmp_path / "run" / "events.jsonl").read_text().splitlines()
    tail_calls = [json.loads(x) for x in first_log
                  if json.loads(x)["kind"] == "TOOL_CALL"
                  and json.loads(x)["turn"] >= 21
                  and json.loads(x)["player_id"] == 0]

    ckpt = CheckpointState.from_doc(json.loads(
        (tmp_path / "run" / "checkpoints" / "ckpt-turn-0020.json").read_text()))

    resumed = Arena(tmp_path / "run", spec, runtimes=runtimes())
    rsummary = await resumed.run(resume_state=ckpt)
    assert rsummary["final_turn"] == 40 and rsummary["violations_total"] == 0
    assert rsummary["final_state_hash"] == summary["final_state_hash"]

    second_log = (tmp_path / "run" / "events.jsonl").read_text().splitlines()
    tail2 = [json.loads(x) for x in second_log
             if json.loads(x)["kind"] == "TOOL_CALL"
             and json.loads(x)["turn"] >= 21 and json.loads(x)["player_id"] == 0]

    def key(r):
        return (r["turn"], r["tool"], r["args_digest"])

    assert [key(r) for r in tail2] == [key(r) for r in tail_calls], (
        "resumed planner issued a different tool-call sequence than the "
        "uninterrupted twin — amnesia not closed")


async def test_belief_rebuild_equals_live(tmp_path):
    spec = spec_for("planner-journal-eq")
    arena = Arena(tmp_path / "run", spec, runtimes=runtimes())
    live_bot = arena.runtimes[0]
    snapshots: dict[int, dict[str, Any]] = {}
    original_turn = live_bot.take_turn

    async def snapshotting_turn(facade):
        await original_turn(facade)
        snapshots[live_bot.belief.turn] = _belief_snapshot(live_bot)

    live_bot.take_turn = snapshotting_turn
    await arena.run()
    assert 4 in snapshots  # the checkpoint turn exists

    fresh = PlannerRuntime(0, 7, budget=4)
    fresh.journal = PlannerJournal(
        tmp_path / "run" / "planner" / "p0-journal.jsonl")
    fresh._restore_from_journal(5)
    assert _belief_snapshot(fresh) == snapshots[4], (
        "journal-rebuilt belief differs from the live runtime's state at "
        "the checkpoint turn")


# ------------------------------------------------------------- journal unit


def test_journal_append_is_idempotent_per_turn(tmp_path):
    j = PlannerJournal(tmp_path / "j" / "p0-journal.jsonl")
    j.append(1, {"v": "a"})
    j.append(2, {"v": "b"})
    j.append(2, {"v": "b2"})  # re-executed turn overwrites, never duplicates
    j.append(3, {"v": "c"})
    recs, _ = j._load()
    assert [r["turn"] for r in recs] == [1, 2, 3]
    assert j.replay_upto(3)[-1]["v"] == "b2"
    assert len(j.replay_upto(9)) == 3
    assert len(j.replay_upto(1)) == 0  # strictly earlier only


def test_journal_tolerates_torn_tail_refuses_midfile(tmp_path):
    j = PlannerJournal(tmp_path / "j" / "p0-journal.jsonl")
    j.append(1, {"v": "a"})
    j.append(2, {"v": "b"})
    path = tmp_path / "j" / "p0-journal.jsonl"
    # torn TRAILING line (crash mid-write): tolerated, dropped
    path.write_text(path.read_text() + '{"turn": 3, "doc": {"v": "tor')
    assert len(j.replay_upto(9)) == 2
    # malformed MID-FILE line: refuses loudly
    lines = path.read_text().splitlines(keepends=True)
    lines.insert(1, "not-json\n")
    path.write_text("".join(lines))
    try:
        j.replay_upto(9)
        raise AssertionError("mid-file corruption accepted")
    except ValueError:
        pass
    # non-monotone turns refuse (tampered ordering)
    j2 = PlannerJournal(tmp_path / "j" / "p1-journal.jsonl")
    j2.append(1, {"v": "a"})
    p2 = tmp_path / "j" / "p1-journal.jsonl"
    p2.write_text(p2.read_text() + json.dumps({"turn": 1, "doc": {}}) + "\n")
    try:
        j2.replay_upto(9)
        raise AssertionError("non-monotone journal accepted")
    except ValueError:
        pass
