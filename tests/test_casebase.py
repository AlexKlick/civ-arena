"""M19b: the case base — retrieval-as-evidence prior over past decisions.

Pinned: the signature is the turn/development-dropped projection of the
search root_key, and the RUNTIME's signature equals the projection of the
RECORDED root_key (the seed-math seam — bstate is the same determinized
world root_key comes from); retrieval can only permute (legal-now compile,
prior subset of candidates); per-turn failures degrade to unprimed and the
match completes; the prior rides the TRACE, never the event log; resume
with a case base armed stays bit-identical (counters are journaled
diagnostics that never affect ranking); the artifact loads loudly and
validates at construction; config gates case_base to planner policies
under the configs/ bare-filename charset.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from civ_arena.agents.runtime import AgentProfile, build_runtime
from civ_arena.arena.coordinator import Arena
from civ_arena.config import AgentSpec, ConfigError, MatchSpec, parse_config
from civ_arena.game.sim.layouts import duel_start
from civ_arena.game.sim.state import SimState
from civ_arena.planner.belief import PlannerBelief, build_state_doc
from civ_arena.planner.casebase import (
    CaseBase,
    mine,
    signature_from_key,
    signature_from_state,
    write_artifact,
)
from civ_arena.planner.runtime import (
    PlannerRuntime,
    _compile_case_ranking,
    _merge_prior,
)
from civ_arena.planner.search import abstract_key, search_option
from test_action_dag import FakeFacade, feed_via
from test_planner import playout
from test_planner_resume import runtimes as resume_runtimes
from test_planner_resume import spec_for as resume_spec_for


def _spec(match_id: str, max_turns: int = 8) -> MatchSpec:
    return MatchSpec(
        match_id=match_id, seed=424242, max_turns=max_turns, adapter="simulator",
        watchdog_mode="flag_and_continue", violation_limit=5, checkpoint_every=5,
        agents=[
            AgentSpec(agent_id="planner", player_id=0, policy="planner", seed=7),
            AgentSpec(agent_id="turtler", player_id=1, policy="turtler", seed=22),
        ],
    )


def _runtimes(case_base: Any = None, budget: int = 4) -> dict[int, Any]:
    return {
        0: PlannerRuntime(0, 7, budget=budget, case_base=case_base),
        1: build_runtime(AgentProfile(agent_id="turtler", player_id=1,
                                      policy="turtler", seed=22)),
    }


def _label_doc(trace: list[dict[str, Any]], sign: int | None = 1,
               diff: int | None = 7) -> dict[str, Any]:
    """Hermetic synthetic label doc (the M19a decisions[] row shape)."""
    return {"schema": 1, "decisions": [
        {"root_key": t["root_key"], "candidates": list(t["candidates"]),
         "chosen": t["chosen"], "outcome_sign": sign,
         "match_value_differential": diff} for t in trace]}


def _write_casebase(label_docs: list[dict[str, Any]], path: Path) -> Path:
    mined = mine(label_docs)
    write_artifact(path, {
        "schema": 1,
        "source": {"indexes": [], "runs": mined["runs"]},
        "cases": mined["cases"],
    })
    return path


async def _run(tmp_path: Path, match_id: str, runtimes: dict[int, Any],
               max_turns: int = 8) -> tuple[Arena, dict[str, Any]]:
    arena = Arena(tmp_path / match_id, _spec(match_id, max_turns),
                  runtimes=runtimes)
    return arena, await arena.run()


def _kinds(run_dir: Path) -> Counter:
    return Counter(json.loads(line)["kind"]
                   for line in (run_dir / "events.jsonl").read_text().splitlines())


# ------------------------------------------------------------- signature


def test_case_signature_equivalence() -> None:
    """The projection drops ONLY turn and development: abstract keys that
    differ in exactly those project to the same case; a candidates change
    (the menu itself moved) splits the case."""
    state = playout(21, 4)
    key = abstract_key(state, 0)

    drifted = json.loads(key)
    drifted["turn"] = drifted["turn"] + 99
    for pid in drifted:
        if pid not in ("turn", "candidates"):
            drifted[pid]["development"] = [["c9", "MONUMENT", "WALLS"]]

    assert signature_from_key(json.dumps(drifted)) == signature_from_key(key)

    split = json.loads(key)
    split["candidates"] = list(reversed(split["candidates"]))
    assert signature_from_key(json.dumps(split)) != signature_from_key(key)

    # the two entry points agree on the same world (the runtime's bstate is
    # built by the same build_state_doc the trace root_key comes from)
    assert signature_from_state(state, 0) == signature_from_key(key)


async def test_signature_matches_root_key(tmp_path):
    """THE SEAM PIN: in a live planner match with a case base armed, every
    recorded trace root_key projects to exactly the signature the runtime
    recorded — the runtime computes its signature from the same determinized
    world (bstate, seed=turn) root_key came from, never a differently-seeded
    one."""
    path = _write_casebase([], tmp_path / "empty-casebase.json")
    arena, summary = await _run(tmp_path, "casebase-seam",
                                _runtimes(CaseBase.from_file(path)))
    assert summary["final_turn"] == 8 and summary["violations_total"] == 0
    trace = arena.runtimes[0].trace
    assert trace and all("case_prior" in t for t in trace)
    for entry in trace:
        assert entry["case_prior"]["signature"] \
            == signature_from_key(entry["root_key"])
        assert entry["case_prior"]["turn"] == entry["turn"]
        assert entry["case_prior"]["hit"] == 0  # empty artifact: all misses
    assert arena.runtimes[0].case_misses == len(trace)
    assert arena.runtimes[0].case_hits == 0


# ------------------------------------------------------------------ miner


def test_mine_aggregates_label_docs() -> None:
    key_a = abstract_key(playout(21, 4), 0)
    key_b = abstract_key(playout(23, 2), 0)
    doc1 = {"decisions": [{
        "root_key": key_a,
        "candidates": ["economy", "expand", "tech_race"],
        "chosen": "economy", "outcome_sign": 1, "match_value_differential": 10,
    }]}
    doc2 = {"decisions": [{
        "root_key": key_a,  # same case, another run: stats aggregate
        "candidates": ["economy", "expand", "tech_race"],
        "chosen": "expand", "outcome_sign": -1, "match_value_differential": -4,
    }, {
        "root_key": key_b,
        "candidates": ["rush"], "chosen": "rush",
        "outcome_sign": None, "match_value_differential": None,  # unscored seat
    }]}
    mined = mine([doc1, doc2])
    assert mined["runs"] == 2
    sig_a, sig_b = signature_from_key(key_a), signature_from_key(key_b)
    assert mined["cases"][sig_a] == {
        "economy": {"n": 2, "taken": 1, "wins": 1, "diff_sum": 10},
        "expand": {"n": 2, "taken": 1, "wins": 0, "diff_sum": -4},
        "tech_race": {"n": 2, "taken": 0, "wins": 0, "diff_sum": 0},
    }
    # n and taken still count for an unscored seat; outcome fields never
    # fabricate a 0 win or differential
    assert mined["cases"][sig_b] == {
        "rush": {"n": 1, "taken": 1, "wins": 0, "diff_sum": 0},
    }


def test_rank_orders_by_win_rate_with_canonical_ties() -> None:
    cb = CaseBase({"sig": {
        "alpha": {"n": 5, "taken": 4, "wins": 3, "diff_sum": 30},
        "beta": {"n": 2, "taken": 2, "wins": 1, "diff_sum": 5},
        "gamma": {"n": 2, "taken": 2, "wins": 1, "diff_sum": 9},
        "delta": {"n": 3, "taken": 3, "wins": 0, "diff_sum": -9},
        "eps": {"n": 9, "taken": 0, "wins": 0, "diff_sum": 0},
    }})
    # 0.75, then the 0.5 tie resolved by option_id asc, then 0.0; a
    # never-taken option carries no win-rate evidence and is excluded
    assert cb.rank("sig") == ["alpha", "beta", "gamma", "delta"]
    assert cb.rank("missing") == []
    assert CaseBase({"sig2": {
        "x": {"n": 4, "taken": 0, "wins": 0, "diff_sum": 0},
    }}).rank("sig2") == []


# ------------------------------------------------------------ loud loading


def test_casebase_from_file_refuses_loudly(tmp_path):
    """Construction-time failure (the RecallCorpus precedent): missing,
    corrupt, wrong-schema, and malformed-stat artifacts all raise before
    any match spends a turn."""
    missing = tmp_path / "nope.json"
    with pytest.raises(ValueError, match="does not exist"):
        CaseBase.from_file(missing)

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        CaseBase.from_file(corrupt)

    wrong = tmp_path / "wrong-schema.json"
    wrong.write_text(json.dumps({"schema": 2, "cases": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="expected schema 1"):
        CaseBase.from_file(wrong)

    bad_stats = tmp_path / "bad-stats.json"
    bad_stats.write_text(json.dumps(
        {"schema": 1, "cases": {"sig": {"x": {"n": 1.5, "taken": 0,
                                              "wins": 0, "diff_sum": 0}}}},
    ), encoding="utf-8")
    with pytest.raises(ValueError):
        CaseBase.from_file(bad_stats)

    # happy path: sha pinned to the file bytes, hit finds the mined case
    key = abstract_key(playout(21, 4), 0)
    path = _write_casebase([_label_doc([
        {"root_key": key, "candidates": ["economy"], "chosen": "economy"},
    ])], tmp_path / "ok.json")
    cb = CaseBase.from_file(path)
    assert cb.artifact_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert cb.hit(signature_from_key(key))
    assert cb.rank(signature_from_key(key)) == ["economy"]


# ---------------------------------------------------------------- runtime


async def test_case_prior_is_permutation_only(tmp_path):
    """Self-primed case base, same seed: at the first decision (the same
    world by construction) the menu is identical and the artifact HITS;
    at every decision the prior is a SUBSET of candidates — retrieval
    permutes the search's visit order, it never widens the menu."""
    arena_a, summary_a = await _run(tmp_path, "casebase-harvest", _runtimes())
    assert summary_a["violations_total"] == 0
    path = _write_casebase([_label_doc(arena_a.runtimes[0].trace)],
                           tmp_path / "primed.json")
    cb = CaseBase.from_file(path)

    arena_b, summary_b = await _run(tmp_path, "casebase-primed", _runtimes(cb))
    assert summary_b["violations_total"] == 0
    trace_a = arena_a.runtimes[0].trace
    trace_b = arena_b.runtimes[0].trace
    assert trace_b[0]["candidates"] == trace_a[0]["candidates"]
    assert trace_b[0]["case_prior"]["hit"] == 1
    assert any(t["case_prior"]["hit"] == 1 for t in trace_b), (
        "self-primed artifact never hit — the pin degenerated to a miss-only run")
    for entry in trace_b:
        assert set(entry["prior"]) <= set(entry["candidates"])
        assert set(entry["case_prior"]["ranked"]) <= set(entry["candidates"])

    # search-level pin (the test_prior_orders_first_visits discipline): the
    # SAME belief searched primed vs unprimed yields the IDENTICAL candidate
    # set — the case prior reorders visits and nothing else
    state = SimState.from_doc(duel_start(21))
    facade = FakeFacade(state, 0)
    belief = PlannerBelief(0)
    await feed_via(facade, belief)
    case_ranked = _compile_case_ranking(
        ["economy", "develop", "expand", "rush", "tech_race"],
        SimState.from_doc(build_state_doc(belief, seed=4)), 0)
    assert case_ranked, "fixture lost all option candidacies"
    unprimed = search_option(belief, 0, method="mcts", budget=2, seed=4)
    primed = search_option(belief, 0, method="mcts", budget=2, seed=4,
                           prior=case_ranked)
    assert primed.candidates == unprimed.candidates
    assert primed.prior == case_ranked
    assert set(primed.prior) <= set(primed.candidates)


async def test_case_prior_fails_soft(tmp_path):
    """A per-turn failure inside the case chain (rank poisoned to raise)
    degrades to unprimed — the match completes and is hash-identical to the
    same-seed match with NO case base; the counters stay untouched because
    the chain died before hit() ran."""
    path = _write_casebase([], tmp_path / "empty.json")
    cb = CaseBase.from_file(path)

    def boom(sig: str) -> list[str]:
        raise RuntimeError("poisoned rank")

    cb.rank = boom  # type: ignore[method-assign]

    arena, summary = await _run(tmp_path, "casebase-poisoned",
                                _runtimes(cb, budget=2), max_turns=6)
    assert summary["final_turn"] == 6 and summary["violations_total"] == 0
    trace = arena.runtimes[0].trace
    assert trace and all(
        t["case_prior"] == {"turn": t["turn"], "error": "case_prior_failed"}
        for t in trace)
    assert all(t["prior"] == [] for t in trace)
    assert arena.runtimes[0].case_hits == 0
    assert arena.runtimes[0].case_misses == 0

    bare, bare_summary = await _run(tmp_path, "casebase-bare",
                                    _runtimes(budget=2), max_turns=6)
    assert bare_summary["final_state_hash"] == summary["final_state_hash"]
    assert all("case_prior" not in t for t in bare.runtimes[0].trace)


async def test_case_prior_rides_trace_not_events(tmp_path):
    """An armed-but-missing case base adds ZERO event surface: the event
    kind multiset is identical to the same-seed unarmed match (behavior is
    unprimed on a miss), while every decision carries its case_prior on the
    TRACE side artifact."""
    path = _write_casebase([], tmp_path / "empty.json")

    armed, armed_summary = await _run(tmp_path, "casebase-armed",
                                      _runtimes(CaseBase.from_file(path)))
    unarmed, unarmed_summary = await _run(tmp_path, "casebase-unarmed",
                                          _runtimes())

    assert _kinds(tmp_path / "casebase-armed") == _kinds(tmp_path / "casebase-unarmed")
    assert armed_summary["final_state_hash"] == unarmed_summary["final_state_hash"]
    log_text = (tmp_path / "casebase-armed" / "events.jsonl").read_text()
    assert "case_prior" not in log_text and "case_stats" not in log_text
    assert all("case" not in kind
               for kind in _kinds(tmp_path / "casebase-armed"))
    trace = armed.runtimes[0].trace
    assert trace and all(t["case_prior"]["hit"] == 0 and
                         t["case_prior"]["ranked"] == [] for t in trace)
    assert all("case_prior" not in t for t in unarmed.runtimes[0].trace)


def test_merged_prior_deterministic(tmp_path):
    """Proposer FIRST (the live signal), then unseen case ids; duplicates
    keep first occurrence; both empty stays None."""
    assert _merge_prior(["economy", "develop"], ["expand", "economy"]) \
        == ["economy", "develop", "expand"]
    assert _merge_prior(None, ["expand", "economy"]) == ["expand", "economy"]
    assert _merge_prior(["economy"], []) == ["economy"]
    assert _merge_prior(None, None) is None

    # the case ranking narrows EXACTLY like compile_proposal: non-str,
    # unknown, duplicate ids drop (first kept), only initiation-true survives
    state = playout(21, 4)
    initiated = {oid for oid in ("expand", "economy", "rush")
                 if _option_initiation(oid, state)}
    compiled = _compile_case_ranking(
        ["rush", "expand", "rush", "no_such_option", 7, "economy"], state, 0)
    assert compiled == [oid for oid in ("rush", "expand", "economy")
                        if oid in initiated]
    assert set(compiled) <= initiated


def _option_initiation(oid: str, state) -> bool:
    from civ_arena.planner.options import OPTIONS

    return OPTIONS[oid].initiation(state, 0)


async def test_merged_prior_rides_search_with_proposer(tmp_path):
    """Full seam: a scripted proposer plus a stubbed case base — the trace's
    recorded prior is exactly merge(proposal, case) decision-for-decision."""
    from fakes import FakeModel

    def reply(ranked):
        return [{"type": "text", "text": json.dumps({"ranked": ranked})}]

    model = FakeModel(script=[reply(["economy", "develop", "expand"])])

    class StubCaseBase:
        artifact_sha256 = "0" * 64

        def rank(self, sig: str) -> list[str]:
            return ["expand", "economy", "rush"]

        def hit(self, sig: str) -> bool:
            return True

    runtimes = {
        0: PlannerRuntime(0, 7, budget=4, proposer=model,
                          case_base=StubCaseBase()),
        1: build_runtime(AgentProfile(agent_id="turtler", player_id=1,
                                      policy="turtler", seed=22)),
    }
    arena, summary = await _run(tmp_path, "casebase-merged", runtimes)
    assert summary["violations_total"] == 0
    trace = arena.runtimes[0].trace
    assert trace and model.posts_sent > 0
    for entry in trace:
        proposal = entry["proposal"]["ranked"]
        case = entry["case_prior"]["ranked"]
        assert entry["prior"] == _merge_prior(proposal or None, case or None)
        assert entry["prior"][:len(proposal)] == proposal  # proposer first


# ----------------------------------------------------------- resume pin


async def test_casebase_resume_bit_identical(tmp_path):
    """The 40-turn resume pattern with a case base armed (self-primed from
    an 8-turn harvest of the same seed, so early decisions HIT and steer
    the sub-candidate-budget search): resumed-from-checkpoint twin is
    bit-identical — final hash, per-turn tool-call sequence, AND the
    journaled hit/miss counters continue instead of resetting."""
    from civ_arena.arena.checkpoints import CheckpointState

    harvest, _ = await _run(tmp_path, "resume-harvest", resume_runtimes(),
                            max_turns=8)
    path = _write_casebase(
        [_label_doc(harvest.runtimes[0].trace)], tmp_path / "resume-cb.json")
    cb = CaseBase.from_file(path)

    spec = resume_spec_for("casebase-resume-id", max_turns=40)
    arena = Arena(tmp_path / "run", spec, runtimes=resume_runtimes(case_base=cb))
    summary = await arena.run()
    assert summary["final_turn"] == 40 and summary["violations_total"] == 0
    first_log = (tmp_path / "run" / "events.jsonl").read_text().splitlines()
    tail_calls = [json.loads(x) for x in first_log
                  if json.loads(x)["kind"] == "TOOL_CALL"
                  and json.loads(x)["turn"] >= 21
                  and json.loads(x)["player_id"] == 0]
    assert any(t["case_prior"]["hit"] == 1
               for t in arena.runtimes[0].trace)

    ckpt = CheckpointState.from_doc(json.loads(
        (tmp_path / "run" / "checkpoints" / "ckpt-turn-0020.json").read_text()))

    resumed = Arena(tmp_path / "run", spec,
                    runtimes=resume_runtimes(case_base=cb))
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
        "resumed planner with a case base diverged from its uninterrupted twin")

    # journaled diagnostics continue across the leg (never reset, never
    # affecting ranking): the resumed twin ends on the same counters
    assert resumed.runtimes[0].case_hits == arena.runtimes[0].case_hits
    assert resumed.runtimes[0].case_misses == arena.runtimes[0].case_misses
    assert (resumed.runtimes[0].case_hits
            + resumed.runtimes[0].case_misses) > 0


# ------------------------------------------------------------ config gate


def _cfg_doc(policy: str = "planner",
             case_base: Any = None) -> dict[str, Any]:
    agent: dict[str, Any] = {
        "agent_id": "a0", "player_id": 0, "policy": policy, "seed": 7,
    }
    if case_base is not None:
        agent["case_base"] = case_base
    return {"match": {"match_id": "m", "seed": 1}, "agents": [agent]}


def test_config_case_base_gating() -> None:
    spec = parse_config(_cfg_doc(case_base={"path": "casebase-exp3.json"}))
    assert spec.agents[0].case_base is not None
    assert spec.agents[0].case_base.path == "casebase-exp3.json"
    # default unchanged for existing configs
    assert parse_config(_cfg_doc()).agents[0].case_base is None

    for bad in ("sub/dir/cb.json", "./cb.json", "", 5, "cb.json/x", ".hidden"):
        with pytest.raises(ConfigError):
            parse_config(_cfg_doc(case_base={"path": bad}))

    # planner-only surface, mirroring the proposer gate
    with pytest.raises(ConfigError, match="case_base: block on policy"):
        parse_config(_cfg_doc(policy="turtler",
                              case_base={"path": "cb.json"}))

    with pytest.raises(ConfigError, match="case_base block must be a mapping"):
        parse_config(_cfg_doc(case_base="cb.json"))
