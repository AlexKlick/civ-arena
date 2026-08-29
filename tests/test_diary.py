"""The diary: validated cross-turn memory that never touches game state."""

from __future__ import annotations

import random
from typing import Any

from civ_arena.arena.coordinator import Arena
from civ_arena.arena.diary import MAX_DIARY_CHARS, DiaryStore
from civ_arena.config import AgentSpec, MatchSpec
from civ_arena.replay import replay_run
from test_hostile_agent import hostile_setup


class DiaryBot:
    """Writes one note per turn (cycling through ``notes``), then ends."""

    def __init__(self, notes: list[str]) -> None:
        self.notes = notes
        self.i = 0
        # checkpoint plumbing expects an rng on every runtime; this one draws none
        self.rng = random.Random(0)

    async def take_turn(self, facade: Any) -> None:
        await facade.write_diary(self.notes[min(self.i, len(self.notes) - 1)])
        self.i += 1
        await facade.end_turn()


def diary_spec(match_id: str = "diary-run", max_turns: int = 3) -> MatchSpec:
    return MatchSpec(
        match_id=match_id, seed=424242, max_turns=max_turns, adapter="simulator",
        watchdog_mode="flag_and_continue", violation_limit=5, checkpoint_every=1,
        agents=[
            AgentSpec(agent_id="roman", player_id=0, policy="expansionist", seed=11),
            AgentSpec(agent_id="korea", player_id=1, policy="turtler", seed=22),
        ],
    )


def diary_bots() -> dict[int, DiaryBot]:
    return {
        0: DiaryBot(["roman-1", "roman-2", "roman-3"]),
        1: DiaryBot(["korea-1", "korea-2", "korea-3"]),
    }


# ------------------------------------------------------------ referee level

async def test_write_diary_accepted_and_stored(tmp_path):
    _adapter, _log, referee, _session, ctx, _lease = await hostile_setup(tmp_path)
    doc = await referee.write_diary(ctx, "scout seen east")
    assert doc["status"] == "accepted"
    assert doc["chars"] == len("scout seen east")
    assert referee.diary.get(0) == "scout seen east"
    assert referee.diary.get(1) == ""  # per-player isolation


async def test_write_diary_requires_valid_lease(tmp_path):
    _adapter, log, referee, _session, ctx, lease = await hostile_setup(tmp_path)
    lease.release()
    doc = await referee.write_diary(ctx, "too late")
    assert doc["status"] == "rejected"
    assert doc["rejection"] == "lease_expired"
    assert referee.diary.get(0) == ""
    # the expired attempt is on the durable record
    kinds = [(r["kind"], r.get("rejection")) for r in log.records()]
    assert ("UNAUTHORIZED_TOOL_CALL", "lease_expired") in kinds
    assert ("TOOL_RESULT", "lease_expired") in kinds


async def test_write_diary_last_write_wins_within_turn(tmp_path):
    _adapter, log, referee, _session, ctx, _lease = await hostile_setup(tmp_path)
    assert (await referee.write_diary(ctx, "first"))["status"] == "accepted"
    assert (await referee.write_diary(ctx, "second"))["status"] == "accepted"
    assert referee.diary.get(0) == "second"
    # NOT deduped: two accepted pairs, both attributable
    results = [r for r in log.records()
               if r["kind"] == "TOOL_RESULT" and r.get("tool") == "write_diary"]
    assert len(results) == 2
    assert all(r["status"] == "accepted" for r in results)


async def test_write_diary_rejects_oversized_and_non_string(tmp_path):
    _adapter, _log, referee, _session, ctx, _lease = await hostile_setup(tmp_path)
    assert (await referee.write_diary(ctx, "x" * (MAX_DIARY_CHARS + 1)))["rejection"] \
        == "args_invalid"
    assert (await referee.write_diary(ctx, "   "))["rejection"] == "args_invalid"
    assert (await referee.write_diary(ctx, 5))["rejection"] == "args_invalid"
    assert referee.diary.get(0) == ""


async def test_write_diary_never_touches_game_state(tmp_path):
    adapter, log, referee, _session, ctx, _lease = await hostile_setup(tmp_path)
    before = adapter.state_hash()
    assert (await referee.write_diary(ctx, "note"))["status"] == "accepted"
    assert adapter.state_hash() == before
    call, result = [r for r in log.records() if r.get("tool") == "write_diary"]
    assert call["kind"] == "TOOL_CALL" and call["args"] == {"text": "note"}
    assert result["kind"] == "TOOL_RESULT"
    # a non-action: no hashes, no receipts, no mutations on the record
    for key in ("after_state_hash", "before_state_hash", "receipts", "mutations"):
        assert key not in result, f"write_diary must not carry {key}"


# ------------------------------------------------------------- from_log

async def test_diary_rebuilt_from_log_prefixes(tmp_path):
    _adapter, log, referee, _session, ctx, _lease = await hostile_setup(tmp_path)
    seqs = []
    for note in ("w1", "w2", "w3"):
        await referee.write_diary(ctx, note)
        seqs.append(len(log.records()))
    assert referee.diary.get(0) == "w3"
    assert DiaryStore.from_log(log.records()).get(0) == "w3"
    # prefix truncation equals the store state at that point (resume semantics)
    assert DiaryStore.from_log(log.records()[: seqs[0]]).get(0) == "w1"
    assert DiaryStore.from_log(log.records()[: seqs[1]]).get(0) == "w2"
    # a rejected write never lands in the rebuilt store
    await referee.write_diary(ctx, "x" * (MAX_DIARY_CHARS + 1))
    assert DiaryStore.from_log(log.records()).get(0) == "w3"


# ------------------------------------------------------------- arena level

class EndBot:
    """Never writes a diary — whatever it finds in the store on resume got
    there by REBUILD, not by this runtime."""

    def __init__(self) -> None:
        self.rng = random.Random(0)

    async def take_turn(self, facade: Any) -> None:
        await facade.end_turn()


async def test_diary_survives_resume(tmp_path):
    import json

    from civ_arena.arena.checkpoints import CheckpointState

    spec = diary_spec("diary-resume", max_turns=4)
    arena = Arena(tmp_path / "run", spec, runtimes=diary_bots())
    await arena.run()
    assert arena.diary.get(0) == "roman-3"  # t3/t4 both write the capped note

    ckpt = CheckpointState.from_doc(json.loads(
        (tmp_path / "run" / "checkpoints" / "ckpt-turn-0002.json").read_text()))
    rebuilt = DiaryStore.from_log(arena.log.records()[: ckpt.seq])
    assert rebuilt.get(0) == "roman-2"
    assert rebuilt.get(1) == "korea-2"

    # resume with runtimes that never write: the diary the resumed arena holds
    # can ONLY have come from the from_log rebuild in _resume_from
    resumed = Arena(tmp_path / "run", spec,
                    runtimes={0: EndBot(), 1: EndBot()})
    summary = await resumed.run(resume_state=ckpt)
    assert summary["final_turn"] == 4
    assert resumed.diary.get(0) == "roman-2"
    assert resumed.diary.get(1) == "korea-2"


async def test_replay_replays_write_diary_calls(tmp_path):
    spec = diary_spec("diary-replay", max_turns=3)
    arena = Arena(tmp_path / "run", spec, runtimes=diary_bots())
    await arena.run()
    result = await replay_run(tmp_path / "run", spec, tmp_path / "replay")
    assert result["identical"], (
        f"replay diverged at comparable-event {result['first_divergence']}"
    )
    # the replayed run performed the same diary writes through the same path
    import json

    replay_records = [
        json.loads(line)
        for line in (tmp_path / "replay" / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert DiaryStore.from_log(replay_records).get(0) == "roman-3"
