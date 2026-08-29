"""The LLM runtime against the REAL arena: fake models, real referee.

No test in this file touches the network — the client is always a FakeModel
double. The live wire is proven by scripts/llm_ping.py and the M6 match run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from civ_arena.agents.llm.prompts import SYSTEM_PROMPT, turn_header
from civ_arena.agents.llm.runtime import LLMAgentRuntime
from civ_arena.agents.runtime import AgentProfile, rng_doc
from civ_arena.arena.checkpoints import CheckpointState
from civ_arena.arena.coordinator import Arena
from civ_arena.canonical import rng_from_doc
from civ_arena.config import AgentSpec, LLMSpec, MatchSpec
from civ_arena.replay import replay_run
from civ_arena.strategy.view import render_memory
from fakes import FakeModel, garbage_model, never_ends_model, text, use

FAKE_LLM = LLMSpec(base_url="http://fake.local/anthropic/v1",
                   api_key_env="FAKE_UNUSED_KEY", model_id="fake-model")


def llm_spec(**overrides: Any) -> LLMSpec:
    fields = {"base_url": FAKE_LLM.base_url, "api_key_env": FAKE_LLM.api_key_env,
              "model_id": FAKE_LLM.model_id, **overrides}
    return LLMSpec(**fields)


def make_arena(tmp_path: Path, fake: FakeModel, *, match_id: str = "llm-run",
               max_turns: int = 1, spec: LLMSpec | None = None,
               agent_id: str = "roman") -> tuple[Arena, FakeModel]:
    llm = spec or FAKE_LLM
    ms = MatchSpec(
        match_id=match_id, seed=424242, max_turns=max_turns, adapter="simulator",
        watchdog_mode="flag_and_continue", violation_limit=5, checkpoint_every=1,
        agents=[
            AgentSpec(agent_id=agent_id, player_id=0, policy="llm", seed=11,
                      llm=llm),
            AgentSpec(agent_id="korea", player_id=1, policy="turtler", seed=22),
        ],
    )
    profile = AgentProfile(agent_id=agent_id, player_id=0, policy="llm",
                           seed=11, llm=llm)
    rt = LLMAgentRuntime.build(profile, client=fake)
    arena = Arena(tmp_path / "run", ms, runtimes={0: rt})
    # injected runtimes wire arena-owned services post-hoc (the build_runtime
    # path does this for non-injected ones)
    rt.telemetry = arena.telemetry
    rt.diary = arena.diary
    rt.strategy = arena.referee.strategy
    return arena, fake


def tool_results(log_records: list[dict], agent_id: str,
                 tool: str | None = None) -> list[dict]:
    out = []
    for rec in log_records:
        if (rec["kind"] == "TOOL_RESULT" and rec.get("agent_id") == agent_id
                and (tool is None or rec.get("tool") == tool)):
            out.append(rec)
    return out


# ------------------------------------------------------------------ basics

async def test_llm_turn_calls_tools_then_ends(tmp_path):
    fake = FakeModel(script=[
        [use("get_overview")],
        [use("set_research", {"tech_id": "MINING"})],
        [use("end_turn")],
    ])
    arena, _ = await _run(make_arena(tmp_path, fake))
    roman = [r for r in arena.log.records() if r.get("agent_id") == "roman"]
    tools_in_order = [r["tool"] for r in roman if r["kind"] == "TOOL_CALL"]
    assert tools_in_order == ["get_overview", "set_research", "end_turn"]
    assert all(r["status"] == "accepted"
               for r in tool_results(arena.log.records(), "roman"))
    assert arena.diary.get(0) == ""


async def _run(pair: tuple[Arena, FakeModel]) -> tuple[Arena, FakeModel]:
    arena, fake = pair
    await arena.run()
    return arena, fake


async def test_llm_runtime_exposes_rng_and_closes(tmp_path):
    fake = FakeModel(script=[[use("end_turn")]])
    arena, fake = await _run(make_arena(tmp_path, fake, max_turns=2))
    rt = arena.runtimes[0]
    assert isinstance(rt, LLMAgentRuntime)
    # checkpoint plumbing round-trips the rng (coordinator.py calls rng_to_doc
    # on EVERY runtime — this is the trap the dummy rng exists for)
    doc = rng_doc(rt)
    rt.rng = rng_from_doc(doc)
    assert rng_doc(rt) == doc
    ckpts = list((tmp_path / "run" / "checkpoints").glob("ckpt-turn-*.json"))
    assert len(ckpts) == 2
    assert fake.closed, "Arena.run must close runtime clients in its finally"


async def test_begin_turn_required_before_take_turn():
    from civ_arena.session.tools import ToolFacade

    fake = FakeModel(script=[[use("end_turn")]])
    profile = AgentProfile(agent_id="x", player_id=0, policy="llm", seed=1,
                           llm=FAKE_LLM)
    rt = LLMAgentRuntime.build(profile, client=fake)
    facade = ToolFacade.__new__(ToolFacade)
    with pytest.raises(RuntimeError, match="begin_turn"):
        await rt.take_turn(facade)


# ------------------------------------------------- hostile model behavior

async def test_unknown_tool_and_bad_args_recover(tmp_path):
    arena, fake = await _run(make_arena(tmp_path, garbage_model()))
    tel = arena.telemetry.snapshot()["roman"]
    assert tel["model_errors"].get("llm_unknown_tool") == 1
    assert tel["model_errors"].get("llm_malformed_args") == 1
    # neither attempt reached the referee as a tool call
    called = {r["tool"] for r in arena.log.records()
              if r["kind"] == "TOOL_CALL" and r.get("agent_id") == "roman"}
    assert "no_such_tool" not in called
    assert "move_unit" not in called  # zero positional args died at the call
    # the model SAW both errors and recovered
    results_msg = fake.requests[1]["messages"][-1]["content"]
    assert results_msg[0]["is_error"] is True
    assert "unknown tool" in results_msg[0]["content"]
    assert results_msg[1]["is_error"] is True
    assert "bad arguments" in results_msg[1]["content"]
    assert tool_results(arena.log.records(), "roman", "end_turn")[0]["status"] \
        == "accepted"


async def test_never_ending_model_is_force_closed(tmp_path):
    arena, fake = await _run(make_arena(
        tmp_path, never_ends_model(), max_turns=2,
        spec=llm_spec(max_tool_rounds=3)))
    tel = arena.telemetry.snapshot()["roman"]
    assert tel["tool_calls"]["get_overview"] == 6  # 3 rounds x 2 turns
    assert tel["tool_calls"]["end_turn"] == 2      # forced both turns
    assert arena.telemetry.snapshot()["roman"]["total_errors"] == 0


async def test_prose_only_reply_forces_end_turn(tmp_path):
    fake = FakeModel(script=[[text("I think therefore I am.")]])
    arena, _ = await _run(make_arena(tmp_path, fake, max_turns=1))
    assert tool_results(arena.log.records(), "roman", "end_turn")


async def test_budget_exhaustion_aborts_with_summary(tmp_path):
    fake = FakeModel(script=[
        [use("get_overview")],
        [use("get_overview")],
        [use("end_turn")],
    ])
    arena, fake = await _run(make_arena(
        tmp_path, fake, spec=llm_spec(max_requests_per_match=2)))
    summary = json.loads((tmp_path / "run" / "summary.json").read_text())
    assert summary["aborted"] and "budget" in summary["aborted"]
    assert fake.posts_sent == 2  # the third request was never sent
    assert summary["telemetry"]["roman"]["tool_calls"]["get_overview"] == 2


async def test_result_compaction_marks_truncation(tmp_path):
    arena, fake = await _run(make_arena(
        tmp_path, FakeModel(script=[[use("get_visible_map")], [use("end_turn")]]),
        spec=llm_spec(max_result_chars=200)))
    results_msg = fake.requests[1]["messages"][-1]["content"]
    content = results_msg[0]["content"]
    import re

    assert re.search(r"\.\.\.\[truncated, full \d+ chars\]$", content)
    assert len(content) <= 200 + 40  # cap plus the variable-width marker


async def test_multiple_tool_uses_in_one_reply_execute_in_order(tmp_path):
    fake = FakeModel(script=[
        [use("get_units"), use("get_overview"), use("end_turn")],
    ])
    arena, _ = await _run(make_arena(tmp_path, fake))
    order = [r["tool"] for r in arena.log.records()
             if r["kind"] == "TOOL_CALL" and r.get("agent_id") == "roman"]
    assert order == ["get_units", "get_overview", "end_turn"]


async def test_no_idempotency_key_from_model(tmp_path):
    fake = FakeModel(script=[
        [use("set_research", {"tech_id": "MINING"})],
        [use("set_research", {"tech_id": "MINING"})],
        [use("end_turn")],
    ])
    arena, _ = await _run(make_arena(tmp_path, fake))
    results = tool_results(arena.log.records(), "roman", "set_research")
    assert len(results) == 2
    assert results[1].get("duplicate") is True
    # the model supplied no nonces: both calls carry the SAME server-derived
    # content key (which is exactly why the second deduped)
    keys = [rec["idempotency_key"] for rec in arena.log.records()
            if rec["kind"] == "TOOL_CALL" and rec.get("agent_id") == "roman"
            and rec.get("tool") == "set_research"]
    assert keys[0] is not None and keys[0] == keys[1]


# ------------------------------------------------------- diary + prompts

async def test_diary_round_trip_across_turns(tmp_path):
    fake = FakeModel(script=[
        [use("write_diary", {"text": "scout seen east"}), use("end_turn")],
    ])
    arena, _ = await _run(make_arena(tmp_path, fake, max_turns=2))
    assert arena.diary.get(0) == "scout seen east"
    # turn 2's opening header carried the turn-1 note back to the model
    header2 = fake.requests[1]["messages"][0]["content"]
    assert header2 == turn_header(2, "scout seen east")
    assert "scout seen east" in header2


async def test_prompts_are_template_rendered_only(tmp_path):
    """Structural leak pin: the arena-authored parts of every request ARE the
    templates — so nothing the runtime knows about identity (agent id, lease,
    match id, instance id) can reach the model. A substring check on
    player_id 0/1 would be vacuous (it matches 'turn 1', coords); this pins
    the actual construction."""
    fake = FakeModel(script=[[use("write_diary", {"text": "note"}), use("end_turn")]])
    arena, _ = await _run(make_arena(
        tmp_path, fake, max_turns=2, match_id="probe-match-x7q",
        agent_id="llm-probe-x7q"))
    for i, req in enumerate(fake.requests):
        assert req["system"] == SYSTEM_PROMPT
        # the pin now CONSTRUCTS through the memory renderer too: any future
        # identity leak via the strategy view fails here, not on the wire.
        # (This store stays empty for this script, so memory renders "" —
        # the construction coverage is the point.)
        expected_header = turn_header(
            i + 1, "note" if i else "",
            render_memory(arena.referee.strategy, 0, i + 1))
        assert req["messages"][0] == {"role": "user", "content": expected_header}
        blob = json.dumps([req["system"], req["messages"]])
        for secret in ("llm-probe-x7q", "probe-match-x7q"):
            assert secret not in blob, f"{secret!r} leaked into request {i}"
        assert "lease" not in blob.lower()
        assert "game_instance" not in blob


# ------------------------------------------------------ usage accounting

async def test_usage_reported_to_telemetry(tmp_path):
    fake = FakeModel(script=[[use("end_turn")]], usage=(111, 222),
                     model_name="fake-m3")
    arena, _ = await _run(make_arena(tmp_path, fake, max_turns=2))
    tel = arena.telemetry.snapshot()["roman"]
    n_requests = len(fake.requests)
    assert tel["input_tokens"] == 111 * n_requests
    assert tel["output_tokens"] == 222 * n_requests
    assert tel["model"] == "fake-m3"
    summary = json.loads((tmp_path / "run" / "summary.json").read_text())
    assert summary["telemetry"]["roman"]["model"] == "fake-m3"


# ------------------------------------------------- replay + resume seams

async def test_llm_match_replays_model_free(tmp_path):
    fake = FakeModel(script=[
        [use("get_overview"), use("write_diary", {"text": "opening"})],
        [use("set_research", {"tech_id": "MINING"})],
        [use("end_turn")],
    ])
    arena, _ = await _run(make_arena(tmp_path, fake, max_turns=3,
                                     match_id="llm-replay"))
    result = await replay_run(tmp_path / "run", arena.spec, tmp_path / "replay")
    assert result["identical"], (
        f"replay diverged at comparable-event {result['first_divergence']}"
    )


async def test_llm_match_resume_from_checkpoint(tmp_path):
    """Resume restores to the turn boundary and the interrupted turns
    re-serve the SAME model script (script exhaustion makes continuation
    deterministic here); final state must equal an uninterrupted run."""
    fake = FakeModel(script=[
        [use("get_overview")],
        [use("set_research", {"tech_id": "MINING"})],
        [use("end_turn")],
    ])

    def build(root: Path, client: FakeModel, max_turns: int) -> Arena:
        ms = MatchSpec(
            match_id="llm-resume", seed=424242, max_turns=max_turns,
            adapter="simulator", watchdog_mode="flag_and_continue",
            violation_limit=5, checkpoint_every=1,
            agents=[
                AgentSpec(agent_id="roman", player_id=0, policy="llm", seed=11,
                          llm=FAKE_LLM),
                AgentSpec(agent_id="korea", player_id=1, policy="turtler",
                          seed=22),
            ],
        )
        profile = AgentProfile(agent_id="roman", player_id=0, policy="llm",
                               seed=11, llm=FAKE_LLM)
        rt = LLMAgentRuntime.build(profile, client=client)
        arena = Arena(root / "run", ms, runtimes={0: rt})
        rt.telemetry = arena.telemetry
        rt.diary = arena.diary
        return arena

    # first leg: 2 turns, then "crash" (in-process: just stop running)
    first = build(tmp_path / "first", fake, max_turns=2)
    await first.run()
    ckpt = CheckpointState.from_doc(json.loads(
        (tmp_path / "first" / "run" / "checkpoints" / "ckpt-turn-0002.json")
        .read_text()))

    # resume on a COPY of the run dir with the SAME model double continuing
    resumed_dir = tmp_path / "resumed"
    resumed_dir.mkdir()
    import shutil

    shutil.copytree(tmp_path / "first" / "run", resumed_dir / "run")
    resumed = build(resumed_dir, fake, max_turns=4)
    summary = await resumed.run(resume_state=ckpt)

    # clean uninterrupted 4-turn run with a fresh double serving the same script
    clean = build(tmp_path / "clean", FakeModel(script=list(fake.script)),
                  max_turns=4)
    clean_summary = await clean.run()

    assert summary["final_state_hash"] == clean_summary["final_state_hash"]
    assert summary["final_turn"] == clean_summary["final_turn"] == 4
