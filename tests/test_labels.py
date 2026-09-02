"""M19a — the outcome-label layer: labels.json side artifacts over runs/.

Pins: labeling is deterministic + canonical (byte-identical twice, no
floats), the event log is never touched, claim verdicts are the scoring
module's own (zero re-implementation), and the shared score_differential
agrees with the retired private helper bodies it deduped away.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from civ_arena import labels
from civ_arena.canonical import CanonicalError, canonical
from civ_arena.game.sim.value import DEFAULT_WEIGHTS, score_differential

REPO = Path(__file__).resolve().parents[1]


def _no_floats(node: Any) -> bool:
    if isinstance(node, float):
        return False
    if isinstance(node, dict):
        return all(_no_floats(v) for v in node.values())
    if isinstance(node, list):
        return all(_no_floats(v) for v in node)
    return True


# ------------------------------------------------- a real planner-vs-turtler


def _planner_spec():
    from civ_arena.config import AgentSpec, MatchSpec

    return MatchSpec(
        match_id="planner-match", seed=424242, max_turns=8,
        adapter="simulator", watchdog_mode="flag_and_continue",
        violation_limit=5, checkpoint_every=2,
        agents=[
            AgentSpec(agent_id="planner", player_id=0, policy="planner",
                      seed=7),
            AgentSpec(agent_id="korea", player_id=1, policy="turtler",
                      seed=22),
        ],
    )


@pytest.fixture(scope="module")
def planner_run(tmp_path_factory: Any) -> Path:
    """A real 8-turn planner-vs-turtler match (the test_search pattern),
    with the harness's own trace side artifact written beside it."""
    from civ_arena.agents.runtime import AgentProfile, build_runtime
    from civ_arena.arena.coordinator import Arena
    from civ_arena.planner.runtime import PlannerRuntime

    tmp_path = tmp_path_factory.mktemp("planner-run")
    spec = _planner_spec()
    bot = PlannerRuntime(0, 7, budget=6)
    turtler = build_runtime(AgentProfile(agent_id="korea", player_id=1,
                                         policy="turtler", seed=22))
    arena = Arena(tmp_path / "run", spec, runtimes={0: bot, 1: turtler})
    asyncio.run(arena.run())
    planner_dir = tmp_path / "run" / "planner"
    planner_dir.mkdir(exist_ok=True)
    (planner_dir / "trace.json").write_text(
        json.dumps(bot.trace, indent=2, sort_keys=True) + "\n")
    return tmp_path / "run"


# ------------------------------------------ synthetic claim-bearing run


def _pair(tool: str, args: dict[str, Any], seq: int, pid: int, turn: int,
          agent: str, match_id: str, observed: dict[str, Any] | None = None,
          ) -> list[dict[str, Any]]:
    """The test_recall pattern: a TOOL_CALL/TOOL_RESULT pair with the FULL
    typed namespace, adjacent seqs, and private visibility — plus the
    ``observed`` digest observation results ride."""
    def base(kind: str, **extra: Any) -> dict[str, Any]:
        return {"kind": kind, "seq": seq if kind == "TOOL_CALL" else seq + 1,
                "match_id": match_id, "game_instance_id": "g", "turn": turn,
                "phase_player_id": pid, "player_id": pid, "agent_id": agent,
                "visibility_scope": "private_player", **extra}
    result: dict[str, Any] = base("TOOL_RESULT", tool=tool, status="accepted")
    if observed is not None:
        result["observed"] = observed
    return [base("TOOL_CALL", tool=tool, args=args), result]


def _write_claim_run(run_dir: Path, agents: list[Any] | None = None,
                     scores: dict[str, Any] | None = None) -> Path:
    """A finished claim-bearing match: roman set three goals (met, missed,
    self-assess) and two predictions (met, self-assess), observed his own
    cities at the deadline, and wrote one lesson; korea wrote nothing.
    ``agents``/``scores`` let tests exercise roster and scoreless-run
    shapes without touching the default event body."""
    match_id = "claims-run"
    if agents is None:
        agents = [["roman", 0, "llm"], ["korean-turtler", 1, "turtler"]]
    if scores is None:
        scores = {
            "ROME": {"cities": 3, "gold": 60, "player_id": 0,
                     "population": 8, "techs": 1, "units": 5},
            "KOREA": {"cities": 1, "gold": 10, "player_id": 1,
                      "population": 2, "techs": 0, "units": 4},
        }
    records: list[dict[str, Any]] = [
        {"kind": "MATCH_START", "seq": 0, "match_id": match_id,
         "game_instance_id": "g", "turn": 0, "phase_player_id": -1,
         "player_id": None, "agent_id": None, "visibility_scope": "referee",
         "config": {"agents": agents}},
    ]
    records += _pair("get_overview", {}, 1, 0, 1, "roman", match_id,
                     observed={"gold": 60, "techs": 1,
                               "researching": "MINING"})
    records += _pair("set_goal",
                     {"text": "hold 3 cities", "metric": "cities",
                      "target": 3, "by_turn": 5}, 3, 0, 1, "roman", match_id)
    records += _pair("set_goal",
                     {"text": "save 500 gold", "metric": "gold",
                      "target": 500, "by_turn": 5}, 5, 0, 1, "roman",
                     match_id)
    records += _pair("set_goal", {"text": "vague intention"}, 7, 0, 1,
                     "roman", match_id)
    records += _pair("record_prediction",
                     {"text": "3 cities by t5", "review_turn": 5,
                      "metric": "cities", "target": 3}, 9, 0, 1, "roman",
                     match_id)
    records += _pair("record_prediction",
                     {"text": "rival walls soon", "review_turn": 5}, 11, 0, 1,
                     "roman", match_id)
    records += _pair("get_cities", {}, 13, 0, 5, "roman", match_id,
                     observed={"own_cities": 3, "own_population": 8,
                               "foreign_cities": []})
    records += _pair("record_lesson",
                     {"text": "expansion paid", "about": "g1"}, 15, 0, 5,
                     "roman", match_id)
    records.append({"kind": "MATCH_END", "seq": 17, "match_id": match_id,
                    "game_instance_id": "g", "turn": 5,
                    "phase_player_id": -1, "player_id": None,
                    "agent_id": None, "visibility_scope": "referee",
                    "summary": {"final_turn": 5, "scores": {}}})
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records))
    (run_dir / "summary.json").write_text(json.dumps({
        "match_id": match_id, "game_instance_id": "g", "final_turn": 5,
        "aborted": None, "violations_total": 0,
        "final_state_hash": "x" * 64, "telemetry": {},
        "scores": scores,
    }, sort_keys=True))
    return run_dir


@pytest.fixture()
def claims_run(tmp_path: Path) -> Path:
    return _write_claim_run(tmp_path / "claims-run")


# ------------------------------------------------------------------ tests


def test_labels_deterministic_and_canonical(planner_run: Path) -> None:
    labels.write_labels(planner_run, labels.label_run(planner_run))
    first = (planner_run / "labels.json").read_bytes()
    labels.write_labels(planner_run, labels.label_run(planner_run))
    second = (planner_run / "labels.json").read_bytes()
    assert first == second  # byte-identical across two full labelings

    doc = json.loads(first)
    assert doc["match_id"] == "planner-match"
    assert doc["decisions"], "a planner run with a trace must carry decisions"
    assert doc["decisions"][0]["prior"] == []  # no proposer: empty prior
    canonical(doc)  # round-trips: raises no CanonicalError
    assert first.decode() == canonical(doc) + "\n"  # canonical on disk
    assert _no_floats(doc)


def test_labels_leave_event_log_untouched(planner_run: Path) -> None:
    events = planner_run / "events.jsonl"
    before = hashlib.sha256(events.read_bytes()).hexdigest()
    labels.write_labels(planner_run, labels.label_run(planner_run))
    after = hashlib.sha256(events.read_bytes()).hexdigest()
    assert before == after


def test_claim_verdicts_match_scoring_module(claims_run: Path) -> None:
    from civ_arena.graph.project import load_records
    from civ_arena.strategy import scoring
    from civ_arena.strategy.store import StrategyStore

    doc = labels.label_run(claims_run)
    # the ORACLE store is rebuilt separately, the way labels rebuilds its own
    oracle = StrategyStore.from_log(load_records(claims_run / "events.jsonl"))
    final_turn = doc["outcome"]["final_turn"]

    def claim_of(row: dict[str, Any]) -> Any:
        if row["kind"] == "goal":
            return next(g for g in oracle.current_goals(row["seat"])
                        if g.goal_id == row["claim_id"])
        return next(p for p in oracle.current_predictions(row["seat"])
                    if p.prediction_id == row["claim_id"])

    scored = [c for c in doc["claims"] if c["kind"] != "lesson"]
    assert len(scored) == 5  # three goals + two predictions
    by_id = {c["claim_id"]: c for c in doc["claims"]}
    for row in scored:
        claim = claim_of(row)
        assert row["verdict"] == scoring.verdict(
            claim, oracle.facts, row["seat"], final_turn), row["claim_id"]
        assert row["deadline_turn"] == scoring.deadline_turn(claim, final_turn)
        expected_observed = None
        if claim.metric:
            as_of = min(final_turn, row["deadline_turn"]) \
                if row["deadline_turn"] is not None else final_turn
            expected_observed = scoring.metric_value(
                oracle.facts, row["seat"], claim.metric, as_of)
        assert row["observed"] == expected_observed, row["claim_id"]

    assert by_id["g1"]["verdict"] == "met"        # cities 3 >= 3 at t5
    assert by_id["g2"]["verdict"] == "missed"     # gold 60 < 500
    assert by_id["g3"]["verdict"] == "self_assess"  # no metric
    assert by_id["p1"]["verdict"] == "met"
    assert by_id["p2"]["verdict"] == "self_assess"
    assert by_id["l1"]["verdict"] is None         # lessons never carry one
    assert by_id["l1"]["observed"] is None


def test_no_floats_in_labels(claims_run: Path) -> None:
    doc = labels.label_run(claims_run)
    canonical(doc)  # a real, float-free label doc round-trips cleanly
    assert _no_floats(doc)
    poisoned = copy.deepcopy(doc)
    poisoned["outcome"]["final_turn"] = 5.0
    with pytest.raises(CanonicalError):
        canonical(poisoned)


def test_shared_differential_agrees_with_old_helpers() -> None:
    """The shared score_differential must equal the retired private helper
    bodies wherever they were defined — with ONE deliberate divergence:
    the no-rival case returns the own score (mirroring value_of's
    fallback) where the retired bodies CRASHED on max([]). That
    divergence is intentional and pinned below; the coordinator's
    behavior-preserving claim is corrected in the docs record, not here."""
    scores = {
        "ROME": {"player_id": 0, "cities": 2, "gold": 462, "population": 9,
                 "techs": 8, "units": 17},
        "KOREA": {"player_id": 1, "cities": 3, "gold": 64, "population": 16,
                  "techs": 8, "units": 25},
    }
    # hand-computed with DEFAULT_WEIGHTS: ROME 200+180+240+170+462 = 1252,
    # KOREA 300+320+240+250+64 = 1174
    assert DEFAULT_WEIGHTS == {"cities": 100, "population": 20, "techs": 30,
                               "units": 10, "gold": 1}
    assert (2 * 100 + 9 * 20 + 8 * 30 + 17 * 10 + 462,
            3 * 100 + 16 * 20 + 8 * 30 + 25 * 10 + 64) == (1252, 1174)

    def old_differential(scores: dict, pid: int) -> int:
        # the retired private body (planner_experiment/_differential),
        # restated verbatim as the oracle
        vals = {}
        for entry in scores.values():
            vals[entry["player_id"]] = sum(
                DEFAULT_WEIGHTS[k] * entry[k] for k in DEFAULT_WEIGHTS)
        rivals = [v for p, v in vals.items() if p != pid]
        return vals[pid] - max(rivals)

    # winner, loser, and a tie
    assert score_differential(scores, 0) == old_differential(scores, 0) == 78
    assert score_differential(scores, 1) == old_differential(scores, 1) == -78
    tie = {"A": {"player_id": 0, "cities": 1, "gold": 0, "population": 1,
                 "techs": 1, "units": 1},
           "B": {"player_id": 1, "cities": 1, "gold": 0, "population": 1,
                 "techs": 1, "units": 1}}
    assert score_differential(tie, 0) == old_differential(tie, 0) == 0
    assert score_differential(tie, 1) == old_differential(tie, 1) == 0

    # the no-rival case is the DELIBERATE divergence: the retired bodies
    # raised on max([]); the shared function mirrors value_of and returns
    # the own score instead of crashing a one-player scores dict
    single = {"ROME": {"player_id": 0, "cities": 1, "gold": 2,
                       "population": 3, "techs": 4, "units": 5}}
    assert score_differential(single, 0) == 100 + 3 * 20 + 4 * 30 + 5 * 10 + 2
    with pytest.raises(ValueError):
        old_differential(single, 0)  # the retired body crashed here

    # the dedupe landed: both scripts use the shared function, neither
    # carries the old private copy
    for script in ("scripts/planner_experiment.py",
                   "scripts/option_mining.py"):
        text = (REPO / script).read_text()
        assert "def _differential" not in text, script
        assert "score_differential" in text, script


# --------------------------------------------- Codex review round pins


def test_index_keyed_by_run_dir_and_duplicate_match_ids_both_kept(
        tmp_path: Path, monkeypatch: pytest.Monkeypatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "runs"
    _write_claim_run(root / "a")
    _write_claim_run(root / "b")  # SAME match_id, different directory
    index_out = tmp_path / "index.json"
    monkeypatch.setattr(sys, "argv", [
        "civ-arena-labels", "--corpus", str(root / "*"),
        "--index-out", str(index_out)])
    labels.main()
    index = json.loads(index_out.read_text())
    # keyed by run directory: both same-id runs survive as entries
    assert set(index) == {str(root / "a"), str(root / "b")}
    assert all(entry["match_id"] == "claims-run"
               for entry in index.values())
    assert index[str(root / "a")]["labels_sha256"] \
        == index[str(root / "b")]["labels_sha256"]  # identical input bytes
    # the per-run print lines survive, one per labeled run
    assert capsys.readouterr().out.count(
        "claims-run: decisions=0 claims=6") == 2


def test_index_out_cannot_target_protected_artifacts(tmp_path: Path) -> None:
    run_dir = _write_claim_run(tmp_path / "run")
    run_dirs = [run_dir]
    for rel in labels.PROTECTED_ARTIFACTS:
        with pytest.raises(SystemExit, match="would overwrite"):
            labels._assert_index_safe(run_dir / rel, run_dirs)
    # a symlink to a protected artifact is caught through resolve()
    link = tmp_path / "sneaky.json"
    link.symlink_to(run_dir / "summary.json")
    with pytest.raises(SystemExit, match="would overwrite"):
        labels._assert_index_safe(link, run_dirs)
    # a genuinely separate path passes
    labels._assert_index_safe(tmp_path / "index.json", run_dirs)


def test_roster_fail_closed_on_duplicate_seats_and_multiple_match_starts(
        tmp_path: Path) -> None:
    dup_pid = _write_claim_run(
        tmp_path / "dup-pid",
        agents=[["roman", 0, "llm"], ["other", 0, "turtler"]])
    with pytest.raises(ValueError, match="duplicate seat identity"):
        labels.label_run(dup_pid)
    dup_agent = _write_claim_run(
        tmp_path / "dup-agent",
        agents=[["roman", 0, "llm"], ["roman", 1, "turtler"]])
    with pytest.raises(ValueError, match="duplicate seat identity"):
        labels.label_run(dup_agent)

    two = _write_claim_run(tmp_path / "two-starts")
    path = two / "events.jsonl"
    recs = [json.loads(ln) for ln in path.read_text().splitlines()
            if ln.strip()]
    recs.append({**recs[0], "seq": recs[-1]["seq"] + 1})  # second MATCH_START
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n"
                            for r in recs))
    with pytest.raises(ValueError, match="exactly one MATCH_START"):
        labels.label_run(two)


def test_claims_outside_roster_fail_closed(tmp_path: Path) -> None:
    run_dir = _write_claim_run(tmp_path / "run")
    path = run_dir / "events.jsonl"
    recs = [json.loads(ln) for ln in path.read_text().splitlines()
            if ln.strip()]
    # an accepted claim pair authored by unrostered player 2 (agent
    # "intruder"): from_log would land it in the store, but the label doc
    # enumerates per roster seat — refusing beats silently omitting it
    recs += _pair("set_goal", {"text": "foreign goal"}, recs[-1]["seq"] + 1,
                  2, 3, "intruder", "claims-run")
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n"
                            for r in recs))
    with pytest.raises(ValueError, match="outside the MATCH_START roster"):
        labels.label_run(run_dir)


def test_scoresless_run_labels_with_null_differential(tmp_path: Path) -> None:
    # real shape of runs/live-exclusive-004/005: aborted legs whose
    # summary carries scores: {} — labelable, with unknown (null)
    # differentials, never fabricated zeros
    run_dir = _write_claim_run(tmp_path / "run", scores={})
    doc = labels.label_run(run_dir)
    assert doc["outcome"]["score_vectors"] == {}
    assert doc["outcome"]["value_differential"] == {"0": None, "1": None}
    assert doc["outcome"]["outcome_sign"] == {"0": None, "1": None}
    assert doc["claims"], "claims do not depend on score vectors"
    labels.write_labels(run_dir, doc)  # nulls are canonical — writes clean
    assert json.loads((run_dir / "labels.json").read_text()) == doc


def test_numeric_match_id_refused(tmp_path: Path) -> None:
    run_dir = _write_claim_run(tmp_path / "run")
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary["match_id"] = 42  # a numeric id must not be str()-laundered
    summary_path.write_text(json.dumps(summary, sort_keys=True))
    with pytest.raises(ValueError, match="non-empty str"):
        labels.label_run(run_dir)
