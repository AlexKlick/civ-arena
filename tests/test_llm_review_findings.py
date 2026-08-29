"""Codex round-1 review pins: one test per finding, all network-free.

Findings source: the 2026-08-28 adversarial review of d10f3b9..2e097d9
(9x P1, 3x P2). Each test names the finding it pins; the fixes live in the
runtime dispatch, client armor, coordinator resume propagation, replay
injection, and config bounds.
"""

from __future__ import annotations

import httpx
import pytest

from civ_arena.agents.llm.client import MiniMaxMessagesClient, ModelUnavailable
from civ_arena.agents.llm.prompts import turn_header
from civ_arena.agents.llm.runtime import LLMAgentRuntime
from civ_arena.agents.runtime import AgentProfile
from civ_arena.arena.checkpoints import CheckpointState
from civ_arena.arena.coordinator import Arena
from civ_arena.arena.diary import DiaryStore
from civ_arena.arena.referee import MatchAborted
from civ_arena.config import AgentSpec, ConfigError, LLMSpec, MatchSpec
from civ_arena.replay import replay_run
from fakes import FakeModel, use

FAKE_LLM = LLMSpec(base_url="http://fake.local/anthropic/v1",
                   api_key_env="FAKE_UNUSED_KEY", model_id="fake-model")


def llm_spec(**overrides) -> LLMSpec:
    return LLMSpec(**{"base_url": FAKE_LLM.base_url,
                      "api_key_env": FAKE_LLM.api_key_env,
                      "model_id": FAKE_LLM.model_id, **overrides})


def two_agent_spec(match_id: str, max_turns: int) -> MatchSpec:
    return MatchSpec(
        match_id=match_id, seed=424242, max_turns=max_turns, adapter="simulator",
        watchdog_mode="flag_and_continue", violation_limit=5, checkpoint_every=1,
        agents=[
            AgentSpec(agent_id="roman", player_id=0, policy="llm", seed=11,
                      llm=FAKE_LLM),
            AgentSpec(agent_id="korea", player_id=1, policy="turtler", seed=22),
        ],
    )


def build_arena(run_dir, match_id, max_turns, fake: FakeModel,
                spec: LLMSpec | None = None) -> tuple[Arena, LLMAgentRuntime]:
    llm = spec or FAKE_LLM
    arena = Arena(run_dir, two_agent_spec(match_id, max_turns))
    profile = AgentProfile(agent_id="roman", player_id=0, policy="llm",
                           seed=11, llm=llm)
    rt = LLMAgentRuntime.build(profile, client=fake)
    arena.runtimes[0] = rt
    rt.telemetry = arena.telemetry
    rt.diary = arena.diary
    return arena, rt


# Finding 1: resume rebuilt the diary into a store the runtime never read.
async def test_resume_feeds_rebuilt_diary_to_llm_runtime(tmp_path):
    import json
    import shutil

    fake1 = FakeModel(script=[[use("write_diary", {"text": "carry me"}),
                               use("end_turn")]])
    arena1, _ = build_arena(tmp_path / "leg1", "f1-resume", 2, fake1)
    await arena1.run()
    assert arena1.diary.get(0) == "carry me"

    shutil.copytree(tmp_path / "leg1", tmp_path / "leg2", dirs_exist_ok=True)
    ckpt = CheckpointState.from_doc(json.loads(
        (tmp_path / "leg2" / "checkpoints" / "ckpt-turn-0002.json").read_text()))
    fake2 = FakeModel(script=[[use("end_turn")]])
    arena2, rt2 = build_arena(tmp_path / "leg2", "f1-resume", 4, fake2)
    await arena2.run(resume_state=ckpt)
    # the resumed runtime READ the rebuilt store: turn 3's header carries it
    header = fake2.requests[0]["messages"][0]["content"]
    assert header == turn_header(3, "carry me")
    assert rt2.diary is arena2.diary


# Finding 2: malformed args must never reposition into valid ones.
async def test_malformed_args_cannot_reposition(tmp_path):
    fake = FakeModel(script=[
        [use("found_city", {"name": "u1"}), use("end_turn")],
    ])
    arena, _ = build_arena(tmp_path, "f2-reposition", 1, fake)
    summary = await arena.run()
    tel = summary["telemetry"]["roman"]
    assert tel["model_errors"].get("llm_malformed_args") == 1
    called = [r["tool"] for r in arena.log.records()
              if r["kind"] == "TOOL_CALL" and r.get("agent_id") == "roman"]
    assert "found_city" not in called, "a repositioned arg reached the referee"
    assert summary["scores"]["ROME"]["cities"] == 0  # the settler never founded
    # end_turn in the same reply closed the turn: exactly one model request
    assert len(fake.requests) == 1


async def test_type_mismatch_is_an_error_result(tmp_path):
    fake = FakeModel(script=[
        [use("write_diary", {"text": 5})],
        [use("end_turn")],
    ])
    arena, _ = build_arena(tmp_path, "f2-type", 1, fake)
    summary = await arena.run()
    tel = summary["telemetry"]["roman"]
    assert tel["model_errors"].get("llm_malformed_args") == 1
    assert arena.diary.get(0) == ""
    result_block = fake.requests[1]["messages"][-1]["content"][0]
    assert result_block["is_error"] is True


# Finding 3: a facade attribute that is not a tool must degrade, not crash.
async def test_facade_attribute_name_degrades_to_unknown_tool(tmp_path):
    fake = FakeModel(script=[[use("names"), use("end_turn")]])
    arena, _ = build_arena(tmp_path, "f3-names", 1, fake)
    summary = await arena.run()
    assert summary["aborted"] is None
    assert summary["telemetry"]["roman"]["model_errors"]["llm_unknown_tool"] == 1


# Finding 4: malformed 200 bodies degrade to ModelUnavailable.
def test_client_parse_armor():
    client = MiniMaxMessagesClient(FAKE_LLM)
    for bad in ["not a dict", {"content": "no"}, {"content": [5]},
                {"content": [{"type": 7}]},
                {"content": [], "usage": {"input_tokens": "x"}}]:
        with pytest.raises(ModelUnavailable, match="malformed"):
            client._parse(bad)
    # a non-dict usage is TOLERATED (treated as empty), not fatal
    reply = client._parse({"content": [{"type": "text", "text": "hi"}],
                           "usage": "no"})
    assert reply.input_tokens == 0
    reply = client._parse({"content": [{"type": "text", "text": "hi"}],
                           "usage": {"input_tokens": 3, "output_tokens": 4}})
    assert reply.input_tokens == 3 and reply.output_tokens == 4


# Finding 5: transport-failed attempts consume budget.
async def test_transport_failures_consume_budget(monkeypatch):
    monkeypatch.setenv("FAKE_UNUSED_KEY", "fake-key-for-budget-test")
    spec = llm_spec(max_retries=1, max_requests_per_match=1,
                    request_timeout_s=5)
    client = MiniMaxMessagesClient(spec)

    class _Dead:
        async def post(self, *a, **k):
            raise httpx.TransportError("read error mid-flight")

    client._http = _Dead()  # type: ignore[assignment]
    with pytest.raises(ModelUnavailable):
        await client.create(system="s", messages=[], tools=[])
    assert client.posts_sent == 1, "the in-flight attempt must count"


# Finding 6: posts and tokens accumulate across resume.
async def test_posts_and_tokens_accumulate_across_resume(tmp_path):
    import json
    import shutil

    fake1 = FakeModel(script=[[use("end_turn")]], usage=(100, 50))
    arena1, _ = build_arena(tmp_path / "leg1", "f6-cumulative", 2, fake1)
    await arena1.run()
    assert len(fake1.requests) == 2

    shutil.copytree(tmp_path / "leg1", tmp_path / "leg2", dirs_exist_ok=True)
    ckpt = CheckpointState.from_doc(json.loads(
        (tmp_path / "leg2" / "checkpoints" / "ckpt-turn-0002.json").read_text()))
    fake2 = FakeModel(script=[[use("end_turn")]], usage=(100, 50))
    arena2, rt2 = build_arena(tmp_path / "leg2", "f6-cumulative", 4, fake2)
    summary = await arena2.run(resume_state=ckpt)
    # cumulative, not reset: 4 turns x (100, 50)
    tel = summary["telemetry"]["roman"]
    assert tel["input_tokens"] == 400 and tel["output_tokens"] == 200
    assert rt2.client.posts_sent == 4, "the budget must span legs"


async def test_budget_spans_resume(tmp_path):
    import json
    import shutil

    fake1 = FakeModel(script=[[use("end_turn")]])
    arena1, _ = build_arena(tmp_path / "leg1", "f6-budget", 2, fake1,
                            spec=llm_spec(max_requests_per_match=3))
    await arena1.run()

    shutil.copytree(tmp_path / "leg1", tmp_path / "leg2", dirs_exist_ok=True)
    ckpt = CheckpointState.from_doc(json.loads(
        (tmp_path / "leg2" / "checkpoints" / "ckpt-turn-0002.json").read_text()))
    fake2 = FakeModel(script=[[use("end_turn")]])
    arena2, rt2 = build_arena(tmp_path / "leg2", "f6-budget", 6, fake2,
                              spec=llm_spec(max_requests_per_match=3))
    summary = await arena2.run(resume_state=ckpt)
    assert summary["aborted"] and "budget" in summary["aborted"]
    assert rt2.client.posts_sent == 3, "leg-1 posts counted against leg 2"


# Finding 7: replay of an aborted LLM match with zero recorded calls stays
# model-free.
async def test_replay_of_aborted_llm_match_never_builds_live_runtime(
        tmp_path, monkeypatch):
    class ExplodingRuntime:
        def __init__(self) -> None:
            self.rng = __import__("random").Random(0)

        async def take_turn(self, facade) -> None:
            raise MatchAborted("model auth dead before any call")

    spec = two_agent_spec("f7-aborted", 1)
    arena = Arena(tmp_path / "run", spec)
    arena.runtimes[0] = ExplodingRuntime()
    summary = await arena.run()
    assert summary["aborted"] == "model auth dead before any call"
    assert not [r for r in arena.log.records()
                if r["kind"] == "TOOL_CALL" and r.get("agent_id") == "roman"]

    def _bomb(*a, **k):
        raise AssertionError("replay must never build the live LLM runtime")

    monkeypatch.setattr(LLMAgentRuntime, "build", _bomb)
    result = await replay_run(tmp_path / "run", spec, tmp_path / "replay")
    assert isinstance(result["identical"], bool)  # completed without bombing


# Finding 8: a key reflected in an error body is redacted before it can
# reach the durable log.
async def test_client_redacts_reflected_key(monkeypatch):
    monkeypatch.setenv("REFLECT_KEY_ENV", "sk-secret-123")
    spec = LLMSpec(base_url="http://fake.local", api_key_env="REFLECT_KEY_ENV",
                   model_id="fake", max_retries=0)
    client = MiniMaxMessagesClient(spec)

    class _Reflect:
        async def post(self, *a, **k):
            return httpx.Response(
                401, text="unauthorized: received x-api-key=sk-secret-123")

    client._http = _Reflect()  # type: ignore[assignment]
    with pytest.raises(ModelUnavailable) as excinfo:
        await client.create(system="s", messages=[], tools=[])
    assert "sk-secret-123" not in str(excinfo.value)
    assert "<redacted>" in str(excinfo.value)


# Finding 9: config bounds keep the wall-clock exposure finite.
def test_config_bounds_finite_and_capped():
    import math

    from civ_arena.config import parse_config

    def doc_with(**llm):
        block = {"base_url": "https://x.example/v1", "api_key_env": "K",
                 "model_id": "m", **llm}
        return {"match": {"match_id": "m", "seed": 1},
                "agents": [{"agent_id": "a", "player_id": 0, "policy": "llm",
                            "llm": block}]}
    with pytest.raises(ConfigError, match="finite"):
        parse_config(doc_with(request_timeout_s=math.inf))
    with pytest.raises(ConfigError, match="finite"):
        parse_config(doc_with(request_timeout_s=601))
    with pytest.raises(ConfigError, match="max_retries"):
        parse_config(doc_with(max_retries=11))
    assert parse_config(doc_with(request_timeout_s=600,
                                 max_retries=10)).agents[0].llm


# Finding 10: the diary bound applies to the RAW text, not just the trim —
# bounded at the runtime (malformed args), never an event.
async def test_padded_oversized_diary_rejected(tmp_path):
    fake = FakeModel(script=[[use("write_diary", {"text": "x" + " " * 5000}),
                              use("end_turn")]])
    arena, _ = build_arena(tmp_path, "f10-padded", 1, fake)
    summary = await arena.run()
    assert arena.diary.get(0) == ""
    assert summary["telemetry"]["roman"]["model_errors"]["llm_malformed_args"] == 1
    assert not [r for r in arena.log.records() if r.get("tool") == "write_diary"]


# Finding 11: from_log requires namespace identity within the pair.
def test_from_log_requires_namespace_identity():
    records = [
        {"kind": "TOOL_CALL", "tool": "write_diary", "player_id": 0,
         "agent_id": "a", "turn": 1, "args": {"text": "steal me"}},
        # foreign result immediately after: adjacency alone must not authorize
        {"kind": "TOOL_RESULT", "tool": "write_diary", "status": "accepted",
         "player_id": 1, "agent_id": "b", "turn": 1},
    ]
    assert DiaryStore.from_log(records).get(0) == ""
    records[1].update(player_id=0, agent_id="a")
    assert DiaryStore.from_log(records).get(0) == "steal me"


# ------------------------------------------------------------------ round 2

# R2-1: a JSON infinity in usage must degrade, not OverflowError the arena.
def test_parse_infinity_degrades():
    client = MiniMaxMessagesClient(FAKE_LLM)
    with pytest.raises(ModelUnavailable, match="malformed"):
        client._parse({"content": [{"type": "text", "text": "hi"}],
                       "usage": {"input_tokens": 1e309}})


# R2-2: redaction covers the sent key even when it straddles the slice
# boundary or the env var rotates mid-flight.
async def test_redaction_covers_boundary_and_rotation(monkeypatch):
    monkeypatch.setenv("ROTATE_KEY_ENV", "sk-old-key-value")
    spec = LLMSpec(base_url="http://fake.local", api_key_env="ROTATE_KEY_ENV",
                   model_id="fake", max_retries=0)
    client = MiniMaxMessagesClient(spec)
    key = "sk-old-key-value"

    class _Reflect:
        async def post(self, *a, **k):
            # the env var rotates AFTER the request was sent with the old key
            monkeypatch.setenv("ROTATE_KEY_ENV", "sk-new-key-value")
            padding = "x" * 295  # push the key across the 300-char cut
            return httpx.Response(
                401, text=f"{padding} {key} rejected")

    client._http = _Reflect()  # type: ignore[assignment]
    with pytest.raises(ModelUnavailable) as excinfo:
        await client.create(system="s", messages=[], tools=[])
    message = str(excinfo.value)
    assert key not in message, "rotated-away sent key leaked"
    assert key[:8] not in message, "a straddling prefix leaked past the cut"
    assert "<redacted" in message


# R2-3: a missing api key burns no budget (no POST happened).
async def test_missing_key_consumes_no_budget(monkeypatch):
    monkeypatch.delenv("FAKE_UNUSED_KEY", raising=False)
    spec = llm_spec(max_requests_per_match=5)
    client = MiniMaxMessagesClient(spec)

    class _Counting:
        def __init__(self):
            self.posts = 0

        async def post(self, *a, **k):
            self.posts += 1
            raise httpx.TransportError("unreachable")

    dead = _Counting()
    client._http = dead  # type: ignore[assignment]
    with pytest.raises(ModelUnavailable, match="is not set"):
        await client.create(system="s", messages=[], tools=[])
    assert client.posts_sent == 0


# R2-4: an llm-policy match refuses to resume from a checkpoint written
# before cross-leg accounting existed (reset spend is not silent).
async def test_resume_refuses_pre_accounting_checkpoint(tmp_path):
    import json

    fake = FakeModel(script=[[use("end_turn")]])
    arena1, _ = build_arena(tmp_path / "run", "r24-refuse", 2, fake)
    await arena1.run()
    path = tmp_path / "run" / "checkpoints" / "ckpt-turn-0002.json"
    doc = json.loads(path.read_text())
    del doc["coordinator_state"]["llm_posts"]  # simulate a pre-fix checkpoint
    # recompute the content hash over the edited body (same formula as
    # CheckpointState.compute_content_hash)
    from civ_arena.canonical import checkpoint_hash

    doc["content_hash"] = checkpoint_hash(
        doc["sim_doc"], doc["rng_states"],
        {**doc["coordinator_state"], "_turn": doc["turn"],
         "_match_id": doc["match_id"], "_seq": doc["seq"]})
    path.write_text(json.dumps(doc, sort_keys=True))

    arena2, _ = build_arena(tmp_path / "run", "r24-refuse", 4, FakeModel(
        script=[[use("end_turn")]]))
    ckpt = CheckpointState.from_doc(json.loads(path.read_text()))
    with pytest.raises(ValueError, match="predates"):
        await arena2.run(resume_state=ckpt)


# R2-5: duplicate agent_id is refused at config parse (attribution key).
def test_duplicate_agent_id_rejected():
    from civ_arena.config import parse_config

    doc = {
        "match": {"match_id": "m", "seed": 1},
        "agents": [
            {"agent_id": "same", "player_id": 0, "policy": "turtler"},
            {"agent_id": "same", "player_id": 1, "policy": "turtler"},
        ],
    }
    with pytest.raises(ConfigError, match="duplicate agent_id"):
        parse_config(doc)


# R2-6: model-side dispatch errors never break the telemetry-vs-log
# recount parity (they live in model_errors, not tool_calls).
async def test_model_errors_do_not_break_recount_parity(tmp_path):

    from fakes import garbage_model

    arena, _ = build_arena(tmp_path, "r26-parity", 1, garbage_model())
    summary = await arena.run()
    tel = summary["telemetry"]["roman"]
    recount = sum(1 for r in arena.log.records()
                  if r["kind"] == "TOOL_RESULT" and r.get("agent_id") == "roman")
    assert tel["total_calls"] == recount
    assert tel["model_errors"], "the garbage model must have produced some"
    assert "llm_unknown_tool" not in tel["tool_calls"]


# R2-8/R3-5: oversized diary text is bounded at the RUNTIME (malformed
# args, no event) — and the referee logs args RAW so replay digests match.
async def test_oversized_model_diary_is_malformed_and_replay_stable(tmp_path):
    fake = FakeModel(script=[[use("write_diary", {"text": "x" + " " * 5000}),
                              use("end_turn")]])
    arena, _ = build_arena(tmp_path, "r35-oversized", 1, fake)
    summary = await arena.run()
    tel = summary["telemetry"]["roman"]
    assert tel["model_errors"].get("llm_malformed_args") == 1
    assert arena.diary.get(0) == ""
    assert not [r for r in arena.log.records()
                if r.get("tool") == "write_diary"], "no event may carry it"
    result = await replay_run(tmp_path, arena.spec, tmp_path / "replay")
    assert result["identical"]


async def test_referee_oversized_diary_logs_raw_for_replay(tmp_path):
    from test_hostile_agent import hostile_setup

    _a, log, referee, _s, ctx, _l = await hostile_setup(tmp_path)
    big = "x" + " " * 5000
    doc = await referee.write_diary(ctx, big)
    assert doc["rejection"] == "args_invalid"
    call = [r for r in log.records()
            if r["kind"] == "TOOL_CALL" and r.get("tool") == "write_diary"][-1]
    assert call["args"]["text"] == big, "logged args must be EXACTLY as sent"


# R2-9: from_log requires match identity within the pair.
def test_from_log_requires_match_identity():
    records = [
        {"kind": "TOOL_CALL", "tool": "write_diary", "player_id": 0,
         "agent_id": "a", "turn": 1, "match_id": "m1",
         "args": {"text": "cross"}},
        {"kind": "TOOL_RESULT", "tool": "write_diary", "status": "accepted",
         "player_id": 0, "agent_id": "a", "turn": 1, "match_id": "m2"},
    ]
    assert DiaryStore.from_log(records).get(0) == ""
    records[1]["match_id"] = "m1"
    assert DiaryStore.from_log(records).get(0) == "cross"


# ------------------------------------------------------------------ round 3

# R3-1: spend survives the post-checkpoint crash window via spend.jsonl.
async def test_spend_survives_crash_window(tmp_path):
    import json

    fake1 = FakeModel(script=[[use("end_turn")]])
    arena1, _ = build_arena(tmp_path, "r31-spend", 2, fake1)
    # wire the production spend sink into the fake client
    arena1.runtimes[0].client.on_post = arena1._spend_sink(arena1.spec.agents[0])
    await arena1.run()
    assert len(fake1.requests) == 2
    assert arena1.spend.counts() == {"0": 2}

    # three requests happened after the last checkpoint, then a crash:
    sink = arena1._spend_sink(arena1.spec.agents[0])
    for _ in range(3):
        sink()

    ckpt = CheckpointState.from_doc(json.loads(
        (tmp_path / "checkpoints" / "ckpt-turn-0002.json").read_text()))
    fake2 = FakeModel(script=[[use("end_turn")]])
    arena2, rt2 = build_arena(tmp_path, "r31-spend", 4, fake2)
    rt2.client.on_post = arena2._spend_sink(arena2.spec.agents[0])
    await arena2.run(resume_state=ckpt)
    # the durable ledger (5) beats the checkpoint (2): the budget remembers
    # everything actually attempted
    assert rt2.client.posts_sent == 5 + 2  # 2 more turns after resume
    assert arena2.spend.counts() == {"0": 7}


# R3-2: an intermediate-version (agent-keyed) llm_posts checkpoint refuses.
async def test_agent_keyed_llm_posts_checkpoint_refused(tmp_path):
    import json

    from civ_arena.canonical import checkpoint_hash

    fake = FakeModel(script=[[use("end_turn")]])
    arena1, _ = build_arena(tmp_path, "r32-keys", 2, fake)
    await arena1.run()
    path = tmp_path / "checkpoints" / "ckpt-turn-0002.json"
    doc = json.loads(path.read_text())
    doc["coordinator_state"]["llm_posts"] = {"roman": 2}  # old keying
    doc["content_hash"] = checkpoint_hash(
        doc["sim_doc"], doc["rng_states"],
        {**doc["coordinator_state"], "_turn": doc["turn"],
         "_match_id": doc["match_id"], "_seq": doc["seq"]})
    path.write_text(json.dumps(doc, sort_keys=True))

    before = len(arena1.log.records())
    arena2, _ = build_arena(tmp_path, "r32-keys", 4, FakeModel(
        script=[[use("end_turn")]]))
    ckpt = CheckpointState.from_doc(json.loads(path.read_text()))
    with pytest.raises(ValueError, match="do not cover"):
        await arena2.run(resume_state=ckpt)
    # R3-3: the refusal must not have touched the log
    assert len(arena2.log.records()) == before


# R3-3 (direct pin): a refused resume leaves the event log untouched.
async def test_refused_resume_never_truncates(tmp_path):
    import json

    fake = FakeModel(script=[[use("end_turn")]])
    arena1, _ = build_arena(tmp_path, "r33-notrunc", 2, fake)
    await arena1.run()
    path = tmp_path / "checkpoints" / "ckpt-turn-0002.json"
    doc = json.loads(path.read_text())
    del doc["coordinator_state"]["llm_posts"]
    from civ_arena.canonical import checkpoint_hash

    doc["content_hash"] = checkpoint_hash(
        doc["sim_doc"], doc["rng_states"],
        {**doc["coordinator_state"], "_turn": doc["turn"],
         "_match_id": doc["match_id"], "_seq": doc["seq"]})
    path.write_text(json.dumps(doc, sort_keys=True))
    before = len((tmp_path / "events.jsonl").read_text().splitlines())

    arena2, _ = build_arena(tmp_path, "r33-notrunc", 4, FakeModel(
        script=[[use("end_turn")]]))
    ckpt = CheckpointState.from_doc(json.loads(path.read_text()))
    with pytest.raises(ValueError, match="predates"):
        await arena2.run(resume_state=ckpt)
    after = len((tmp_path / "events.jsonl").read_text().splitlines())
    assert before == after, "a refusal mutated the run dir"


# R3-4: merged telemetry keeps total_errors == sum of rejected calls.
def test_merge_snapshot_total_errors_parity():
    from civ_arena.arena.telemetry import TelemetryRegistry

    reg = TelemetryRegistry()
    reg.merge_snapshot({"roman": {
        "tool_calls": {"attack": 5}, "tool_errors": {"attack": 1},
        "model_errors": {"llm_malformed_args": 2},
        "total_calls": 5, "total_errors": 1, "total_ms": 0,
        "input_tokens": 0, "output_tokens": 0, "model": "m",
    }})
    doc = reg.snapshot()["roman"]
    assert doc["total_calls"] == 5
    assert doc["total_errors"] == 1, "rejected calls are errors"
    assert doc["model_errors"] == {"llm_malformed_args": 2}


# R3-6: a literal '<' in an error body is not a redaction marker.
def test_snippet_leaves_literal_lt_alone():
    body = "value < expected in payload " + "y" * 400
    out = MiniMaxMessagesClient._snippet(body)
    assert out.startswith("value < expected")
    assert "<redacted>" not in out
    # a cut landing inside a REAL marker still closes it
    marked = "z" * 296 + "<redacted> tail"
    out2 = MiniMaxMessagesClient._snippet(marked)
    assert out2.endswith("<redacted>")


# R3-7: from_log requires game-instance identity within the pair.
def test_from_log_requires_game_instance_identity():
    records = [
        {"kind": "TOOL_CALL", "tool": "write_diary", "player_id": 0,
         "agent_id": "a", "turn": 1, "match_id": "m", "game_instance_id": "g1",
         "args": {"text": "x"}},
        {"kind": "TOOL_RESULT", "tool": "write_diary", "status": "accepted",
         "player_id": 0, "agent_id": "a", "turn": 1, "match_id": "m",
         "game_instance_id": "g2"},
    ]
    assert DiaryStore.from_log(records).get(0) == ""
    records[1]["game_instance_id"] = "g1"
    assert DiaryStore.from_log(records).get(0) == "x"
