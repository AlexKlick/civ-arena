"""RecallCorpus: an agent's own prior-match lessons, deterministic lexical
retrieval, built straight from prior event logs (the trust root).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from civ_arena.recall import LESSON_LIMIT, RecallCorpus

# ---------------------------------------------------------------- helpers


def _write_run(run_dir: Path, match_id: str) -> Path:
    """A finished prior match whose roman agent wrote three lessons; the
    turtler wrote one (scoped out of roman's recall)."""
    records: list[dict[str, Any]] = [
        {"kind": "MATCH_START", "seq": 0, "match_id": match_id,
         "game_instance_id": "g", "turn": 0, "phase_player_id": -1,
         "player_id": None, "agent_id": None, "visibility_scope": "referee",
         "config": {"agents": [["roman", 0, "llm"],
                               ["korean-turtler", 1, "turtler"]]}},
    ]
    records += _pair("record_lesson",
                     {"text": "Axial coordinate neighbors exclude (q+1,r+1) "
                              "— hex grids have six neighbors"},
                     1, 0, 4, "roman", match_id)
    records += _pair("record_lesson",
                     {"text": "Found cities on grassland near fresh water "
                              "for growth"}, 3, 0, 8, "roman", match_id)
    records += _pair("record_lesson",
                     {"text": "Archers behind warriors win fights; stacks "
                              "work"}, 5, 0, 12, "roman", match_id)
    records += _pair("record_lesson",
                     {"text": "Turtle behind walls and never leave home"},
                     7, 1, 15, "korean-turtler", match_id)
    records.append({"kind": "MATCH_END", "seq": 9, "match_id": match_id,
                    "game_instance_id": "g", "turn": 20,
                    "phase_player_id": -1, "player_id": None,
                    "agent_id": None, "visibility_scope": "referee",
                    "summary": {"final_turn": 20, "scores": {}}})
    d = run_dir / match_id
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records))
    return d


def _pair(tool: str, args: dict[str, Any], seq: int, pid: int, turn: int,
          agent: str, match_id: str) -> list[dict[str, Any]]:
    def base(kind: str, **extra: Any) -> dict[str, Any]:
        return {"kind": kind, "seq": seq if kind == "TOOL_CALL" else seq + 1,
                "match_id": match_id, "game_instance_id": "g", "turn": turn,
                "phase_player_id": pid, "player_id": pid, "agent_id": agent,
                "visibility_scope": "private_player", **extra}
    return [
        base("TOOL_CALL", tool=tool, args=args),
        base("TOOL_RESULT", tool=tool, status="accepted"),
    ]


def _corpus(tmp_path: Path, priors: list[str], self_id: str = "self-match",
            **kw: Any) -> RecallCorpus:
    for prior in set(priors) - {"self-match"}:
        _write_run(tmp_path, prior, **kw)
    return RecallCorpus.from_runs(tmp_path, self_id, priors)


# ------------------------------------------------------------------ corpus


def test_corpus_extracts_own_agent_lessons_from_prior_logs(tmp_path: Path):
    corpus = _corpus(tmp_path, ["prior-a"])
    assert corpus.size("roman") == 3
    assert corpus.size("korean-turtler") == 1  # present, but scoped out
    assert corpus.size() == 4


def test_corpus_excludes_self_and_dedupes_prior_ids(tmp_path: Path):
    corpus = _corpus(tmp_path, ["prior-a", "prior-a", "self-match"])
    assert corpus.size() == 4  # prior-a counted once; self-match never read
    assert all(e["match_id"] == "prior-a" for e in corpus.query(
        "roman", "cities grassland"))


def test_corpus_missing_run_fails_loudly(tmp_path: Path):
    with pytest.raises(ValueError, match="missing run 'nope'"):
        RecallCorpus.from_runs(tmp_path, "self", ["nope"])


def test_corpus_rejects_log_without_roster(tmp_path: Path):
    d = tmp_path / "empty-run"
    d.mkdir()
    (d / "events.jsonl").write_text(
        json.dumps({"kind": "TURN_END", "seq": 0, "match_id": "empty-run",
                    "turn": 1}) + "\n")
    with pytest.raises(ValueError, match="no MATCH_START roster"):
        RecallCorpus.from_runs(tmp_path, "self", ["empty-run"])


def test_corpus_requires_intact_seq(tmp_path: Path):
    # the strict loader discipline: a prior log with broken seq fails
    # closed instead of yielding a silently-wrong corpus
    _write_run(tmp_path, "broken")
    path = tmp_path / "broken" / "events.jsonl"
    lines = path.read_text().splitlines()
    rec = json.loads(lines[-1])
    rec["seq"] = 99  # position 10 must carry seq 10 — this log is corrupt
    lines[-1] = json.dumps(rec, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="seq broken"):
        RecallCorpus.from_runs(tmp_path, "self", ["broken"])


# ---------------------------------------------------------------- retrieval


def test_query_ranks_by_term_overlap_and_never_fills():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_run(tmp, "prior-a")
        _write_run(tmp, "prior-b")
        corpus = RecallCorpus.from_runs(tmp, "self", ["prior-a", "prior-b"])
        hits = corpus.query("roman", "city placement grassland water")
        assert hits, "expected grassland-city lesson to match"
        assert hits[0]["text"].startswith("Found cities on grassland")
        assert all("match_id" in h and "turn" in h and "lesson_id" in h
                   for h in hits)
        # zero-score queries return nothing — no filler
        assert corpus.query("roman", "diplomacy spaceship") == []
        assert corpus.query("korean-turtler", "grassland") == []  # own-only


def test_query_is_deterministic_and_capped():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for run in ("prior-a", "prior-b"):
            _write_run(tmp, run)
        corpus = RecallCorpus.from_runs(tmp, "self", ["prior-a", "prior-b"])
        first = corpus.query("roman", "neighbors coordinate hex axial")
        second = corpus.query("roman", "neighbors coordinate hex axial")
        assert first == second
        assert len(corpus.query("roman", "lesson warriors archers cities "
                                        "grassland hex stacks")) <= LESSON_LIMIT


# ------------------------------------------------------------ referee tool


def _referee(tmp_path: Path, corpus: RecallCorpus | None):
    from civ_arena.arena.events import EventLog
    from civ_arena.arena.referee import Referee, RefereeConfig
    from civ_arena.arena.telemetry import TelemetryRegistry
    from civ_arena.arena.visibility import VisibilityPolicy
    from civ_arena.game.sim.simulator import SimulatorAdapter
    from civ_arena.session.tools import SessionCtx

    adapter = SimulatorAdapter()

    async def setup() -> tuple:
        await adapter.setup({"seed": 4})
        log = EventLog(tmp_path / "events.jsonl")
        referee = Referee(adapter, VisibilityPolicy(), log,
                          TelemetryRegistry(), "m", "g1", RefereeConfig(),
                          recall=corpus)
        lease = referee.grant_lease(0, "roman", 1)
        await referee.begin_turn(0, "roman", 1)
        ctx = SessionCtx(referee=referee, player_id=0, agent_id="roman",
                         lease=lease, turn=1)
        return referee, log, ctx
    return setup


async def test_recall_accepted_logged_raw_and_lifted(tmp_path: Path):
    from civ_arena.recall import RecallCorpus as RC

    corpus = RC([{"agent_id": "roman", "match_id": "prior-a", "turn": 4,
                  "lesson_id": "l1", "text": "grassland cities grow",
                  "about": ""}])
    referee, log, ctx = await _referee(tmp_path, corpus)()
    doc = await referee.recall_lessons(ctx, "city grassland placement")
    assert doc["status"] == "accepted"
    assert doc["recalled"]["lessons"][0]["lesson_id"] == "l1"
    assert doc["recalled"]["query"] == "city grassland placement"
    records = log.records()
    call = [r for r in records if r["kind"] == "TOOL_CALL"
            and r["tool"] == "recall_lessons"][-1]
    result = [r for r in records if r["kind"] == "TOOL_RESULT"
              and r["tool"] == "recall_lessons"][-1]
    assert call["args"] == {"query": "city grassland placement"}  # RAW
    assert result["recalled"] == doc["recalled"]  # the log carries the feed
    assert result["result_doc"] is None  # payload rides only the lift
    assert "before_state_hash" not in result and "mutations" not in result \
        and "receipts" not in result  # a validated non-action


async def test_recall_requires_the_lease(tmp_path: Path):
    from civ_arena.session.tools import SessionCtx

    referee, log, ctx = await _referee(tmp_path, None)()
    foreign = SessionCtx(referee=referee, player_id=0, agent_id="roman",
                         lease=None, turn=1)
    doc = await referee.recall_lessons(foreign, "anything")
    assert doc["status"] == "rejected"
    assert doc["rejection"] == "lease_foreign"
    assert any(r.get("rejection") == "lease_foreign" for r in log.records()
               if r["kind"] == "TOOL_RESULT")


async def test_recall_without_corpus_is_tool_unavailable(tmp_path: Path):
    referee, log, ctx = await _referee(tmp_path, None)()
    doc = await referee.recall_lessons(ctx, "city placement")
    assert doc["status"] == "rejected"
    assert doc["rejection"] == "tool_unavailable"
    assert "recall_runs" in doc["reason"]
    assert any(r.get("rejection") == "tool_unavailable" for r in log.records()
               if r["kind"] == "TOOL_RESULT")


async def test_recall_non_string_query_returns_empty(tmp_path: Path):
    # a DIRECT caller smuggling a non-str: logged canonically, no crash,
    # no matches — the runtime's schema enforces str for model calls
    from civ_arena.recall import RecallCorpus as RC

    corpus = RC([{"agent_id": "roman", "match_id": "prior-a", "turn": 4,
                  "lesson_id": "l1", "text": "grassland cities grow",
                  "about": ""}])
    referee, _log, ctx = await _referee(tmp_path, corpus)()
    doc = await referee.recall_lessons(ctx, 42)  # type: ignore[arg-type]
    assert doc["status"] == "accepted"
    assert doc["recalled"]["lessons"] == []


def test_recall_schema_bounds_the_query():
    from civ_arena.agents.llm.tool_schemas import TOOL_SCHEMAS

    entry = next(s for s in TOOL_SCHEMAS if s["name"] == "recall_lessons")
    query = entry["input_schema"]["properties"]["query"]
    assert query["maxLength"] == 280
    assert entry["input_schema"]["required"] == ["query"]


# ------------------------------------------------- integration + replay


async def test_match_with_recall_replays_model_free(tmp_path: Path):
    from civ_arena.agents.llm.runtime import LLMAgentRuntime
    from civ_arena.agents.runtime import AgentProfile
    from civ_arena.arena.coordinator import Arena
    from civ_arena.config import AgentSpec, MatchSpec
    from civ_arena.replay import replay_run
    from fakes import FakeModel, use
    from test_llm_runtime import FAKE_LLM

    runs = tmp_path / "runs"
    _write_run(runs, "prior-a")
    ms = MatchSpec(
        match_id="recall-match", seed=424242, max_turns=1,
        adapter="simulator", watchdog_mode="flag_and_continue",
        violation_limit=5, checkpoint_every=1,
        agents=[
            AgentSpec(agent_id="roman", player_id=0, policy="llm", seed=11,
                      llm=FAKE_LLM),
            AgentSpec(agent_id="korea", player_id=1, policy="turtler",
                      seed=22),
        ],
        recall_runs=["prior-a"],
    )
    fake = FakeModel(script=[[use("recall_lessons",
                                  {"query": "grassland city growth"})],
                             [use("end_turn")]])
    profile = AgentProfile(agent_id="roman", player_id=0, policy="llm",
                           seed=11, llm=FAKE_LLM)
    rt = LLMAgentRuntime.build(profile, client=fake)
    arena = Arena(runs / "recall-match", ms, runtimes={0: rt})
    rt.telemetry = arena.telemetry
    rt.diary = arena.diary
    rt.strategy = arena.referee.strategy
    await arena.run()

    records = arena.log.records()
    recall_results = [r for r in records if r["kind"] == "TOOL_RESULT"
                      and r["tool"] == "recall_lessons"]
    assert len(recall_results) == 1
    assert recall_results[0]["status"] == "accepted"
    assert recall_results[0]["recalled"]["lessons"][0]["match_id"] == "prior-a"
    # the model saw exactly what the log recorded: the second request's
    # final message is the tool_result batch carrying the stringified doc
    fed = fake.requests[1]["messages"][-1]["content"]
    lesson_text = recall_results[0]["recalled"]["lessons"][0]["text"]
    assert any(lesson_text in str(b) for b in fed
               if isinstance(b, dict))

    # model-free replay: the replay Arena rebuilds the SAME corpus from the
    # config, so the recorded accepted recall replays identically — the
    # `recalled` field is invisible to the comparison by construction
    out = await replay_run(runs / "recall-match", ms,
                           runs / "recall-match-replay")
    assert out["identical"], out.get("first_divergence")


# ------------------------------------------------- review round 1 pins


def test_strip_compares_recalled_payloads():
    # round-1 P1: observations re-derive from replayed state, but a recall
    # re-queries an EXTERNAL corpus — a changed corpus must break replay,
    # not stay green while the model would have been fed different lessons
    from civ_arena.replay import _strip

    base = {"kind": "TOOL_RESULT", "turn": 1, "player_id": 0,
            "agent_id": "roman", "tool": "recall_lessons",
            "status": "accepted"}
    a = _strip([dict(base, recalled={"lessons": ["l1"]})])
    b = _strip([dict(base, recalled={"lessons": ["l2"]})])
    assert a != b
    # absence still compares equal — pre-M13 logs replay unchanged
    assert _strip([dict(base)]) == _strip([dict(base)])


def test_corpus_rejects_unfinished_prior(tmp_path: Path):
    # round-1 P2: a run still being written could feed a crash-resume
    # rebuild lessons that did not exist when the match started
    _write_run(tmp_path, "wip")
    path = tmp_path / "wip" / "events.jsonl"
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    recs = [json.loads(ln) for ln in lines]
    recs = [r for r in recs if r["kind"] != "MATCH_END"]  # still running
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n"
                            for r in recs))
    with pytest.raises(ValueError, match="not a finished match"):
        RecallCorpus.from_runs(tmp_path, "self", ["wip"])


def test_corpus_rejects_internal_identity_mismatch(tmp_path: Path):
    # round-1 P2/F5: a replay twin (dir <id>-replay carrying internal id
    # <id>) must not slip into the corpus as a "prior"
    _write_run(tmp_path, "m-replay")
    path = tmp_path / "m-replay" / "events.jsonl"
    text = path.read_text().replace('"match_id": "m-replay"',
                                    '"match_id": "m"')
    path.write_text(text)
    with pytest.raises(ValueError, match="different match_id internally"):
        RecallCorpus.from_runs(tmp_path, "self", ["m-replay"])


def test_corpus_rejects_malformed_roster(tmp_path: Path):
    # round-1 P2: duplicate player ids would attribute one agent's lessons
    # to another agent as its "own" memory
    _write_run(tmp_path, "dup")
    path = tmp_path / "dup" / "events.jsonl"
    text = path.read_text().replace(
        '"agents": [["roman", 0, "llm"], ["korean-turtler", 1, "turtler"]]',
        '"agents": [["roman", 0, "llm"], ["korea", 0, "turtler"]]')
    path.write_text(text)
    with pytest.raises(ValueError, match="malformed roster"):
        RecallCorpus.from_runs(tmp_path, "self", ["dup"])


def _config_doc(recall_runs: list, max_result_chars: int = 8000) -> dict:
    return {
        "match": {"match_id": "m", "seed": 1, "max_turns": 2,
                  "adapter": "simulator",
                  "watchdog_mode": "flag_and_continue",
                  "checkpoint_every": 1, "recall_runs": recall_runs},
        "agents": [
            {"agent_id": "roman", "player_id": 0, "policy": "llm",
             "seed": 11,
             "llm": {"base_url": "http://x", "api_key_env": "K",
                     "model_id": "mm", "max_result_chars": max_result_chars}},
            {"agent_id": "korea", "player_id": 1, "policy": "turtler",
             "seed": 22},
        ],
    }


def test_config_rejects_path_like_recall_ids():
    from civ_arena.config import ConfigError, parse_config

    for bad in ("./m", "../runs/x", "/abs/run", ".hidden"):
        with pytest.raises(ConfigError, match="bare match ids"):
            parse_config(_config_doc([bad]))
    parse_config(_config_doc(["llm-vs-turtler-002"]))  # bare ids pass


def test_config_enforces_result_cap_floor_for_recall():
    from civ_arena.config import ConfigError, parse_config

    with pytest.raises(ConfigError, match="max_result_chars"):
        parse_config(_config_doc(["prior-a"], max_result_chars=80))
    # without recall, a small cap stays legal (pre-existing behavior)
    parse_config(_config_doc([], max_result_chars=80))


async def test_replay_with_custom_dir_resolves_corpus_from_source_root(
        tmp_path: Path):
    # round-1 P3: the corpus lives beside the SOURCE run; a custom
    # --replay-dir elsewhere must not move or shadow it
    from civ_arena.agents.llm.runtime import LLMAgentRuntime
    from civ_arena.agents.runtime import AgentProfile
    from civ_arena.arena.coordinator import Arena
    from civ_arena.config import AgentSpec, MatchSpec
    from civ_arena.replay import replay_run
    from fakes import FakeModel, use
    from test_llm_runtime import FAKE_LLM

    runs = tmp_path / "runs"
    _write_run(runs, "prior-a")
    ms = MatchSpec(
        match_id="recall-match2", seed=424242, max_turns=1,
        adapter="simulator", watchdog_mode="flag_and_continue",
        violation_limit=5, checkpoint_every=1,
        agents=[
            AgentSpec(agent_id="roman", player_id=0, policy="llm", seed=11,
                      llm=FAKE_LLM),
            AgentSpec(agent_id="korea", player_id=1, policy="turtler",
                      seed=22),
        ],
        recall_runs=["prior-a"],
    )
    fake = FakeModel(script=[[use("recall_lessons",
                                  {"query": "grassland growth"})],
                             [use("end_turn")]])
    profile = AgentProfile(agent_id="roman", player_id=0, policy="llm",
                           seed=11, llm=FAKE_LLM)
    rt = LLMAgentRuntime.build(profile, client=fake)
    arena = Arena(runs / "recall-match2", ms, runtimes={0: rt})
    rt.telemetry = arena.telemetry
    rt.diary = arena.diary
    rt.strategy = arena.referee.strategy
    await arena.run()

    elsewhere = tmp_path / "elsewhere" / "replay-out"
    out = await replay_run(runs / "recall-match2", ms, elsewhere)
    assert out["identical"], out.get("first_divergence")
