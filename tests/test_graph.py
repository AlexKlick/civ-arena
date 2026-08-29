"""The M12 graph projection: the event log -> Graphiti-shaped nodes/edges,
as a pure deterministic function (artifacts are the portable truth).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from civ_arena.graph import project as project_cli
from civ_arena.graph.artifacts import load_artifacts, write_artifacts
from civ_arena.graph.project import load_records
from civ_arena.graph.projection import Projection, project

# ---------------------------------------------------------------- helpers


def _start(seq: int = 0, match_id: str = "m",
           agents: list[list[Any]] | None = None) -> dict[str, Any]:
    return {
        "kind": "MATCH_START", "seq": seq, "match_id": match_id,
        "game_instance_id": "g1", "turn": 0, "phase_player_id": -1,
        "player_id": None, "agent_id": None, "visibility_scope": "referee",
        "schema": 1, "ts": "2026-01-01T00:00:00+00:00",
        "config": {
            "agents": agents if agents is not None
            else [["roman", 0, "llm"], ["cart", 1, "turtler"]],
            "max_turns": 40, "seed": 7, "watchdog_mode": "flag_and_continue",
        },
        "initial_state_hash": "x" * 64,
    }


def _end(seq: int, match_id: str = "m", final_turn: int = 8,
         scores: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "kind": "MATCH_END", "seq": seq, "match_id": match_id,
        "game_instance_id": "g1", "turn": final_turn, "phase_player_id": -1,
        "player_id": None, "agent_id": None, "visibility_scope": "referee",
        "schema": 1, "ts": "2026-01-01T01:00:00+00:00",
        "summary": {
            "aborted": None, "final_turn": final_turn,
            "final_state_hash": "y" * 64, "match_id": match_id,
            "scores": scores if scores is not None else {
                "ROME": {"cities": 4, "gold": 509, "player_id": 0,
                         "population": 21, "techs": 8, "units": 24},
                "KOREA": {"cities": 2, "gold": 81, "player_id": 1,
                          "population": 15, "techs": 8, "units": 20},
            },
        },
    }


def _call(tool: str, args: dict[str, Any], *, seq: int, pid: int = 0,
          turn: int = 1, agent: str = "roman", match_id: str = "m",
          ) -> dict[str, Any]:
    return {
        "kind": "TOOL_CALL", "tool": tool, "args": args, "seq": seq,
        "player_id": pid, "agent_id": agent, "turn": turn,
        "match_id": match_id, "game_instance_id": "g1",
        "phase_player_id": pid, "visibility_scope": "private_player",
        "schema": 1, "ts": "2026-01-01T00:00:01+00:00",
    }


def _result(tool: str, *, seq: int, pid: int = 0, turn: int = 1,
            agent: str = "roman", match_id: str = "m",
            status: str = "accepted", **extra: Any) -> dict[str, Any]:
    return {
        "kind": "TOOL_RESULT", "tool": tool, "seq": seq, "status": status,
        "player_id": pid, "agent_id": agent, "turn": turn,
        "match_id": match_id, "game_instance_id": "g1",
        "phase_player_id": pid, "visibility_scope": "private_player",
        "schema": 1, "ts": "2026-01-01T00:00:01+00:00", **extra,
    }


def _pair(tool: str, args: dict[str, Any], *, seq: int, pid: int = 0,
          turn: int = 1, agent: str = "roman",
          status: str = "accepted") -> list[dict[str, Any]]:
    return [
        _call(tool, args, seq=seq, pid=pid, turn=turn, agent=agent),
        _result(tool, seq=seq + 1, pid=pid, turn=turn, agent=agent,
                status=status),
    ]


def _obs(tool: str, observed: dict[str, Any], *, seq: int, pid: int = 0,
         turn: int = 1, agent: str = "roman") -> dict[str, Any]:
    return _result(tool, seq=seq, pid=pid, turn=turn, agent=agent,
                   observed=observed)


def _rich_log() -> list[dict[str, Any]]:
    recs = [_start()]
    recs += _pair("set_goal",
                  {"text": "hold 3 cities", "by_turn": 4, "metric": "cities",
                   "target": 3},
                  seq=1, pid=0, turn=1)
    recs += [_obs("get_cities",
                  {"own_cities": 3, "own_population": 7,
                   "foreign_cities": [{"city_id": "c9", "name": "SEOUL",
                                       "coord": "3,4", "owner_id": 1,
                                       "hp": 100, "population": 5}]},
                  seq=9, pid=0, turn=4)]
    recs += [_obs("get_overview", {"gold": 200, "techs": 2},
                  seq=10, pid=0, turn=4)]
    recs += _pair("set_goal",
                  {"text": "hold 4 cities", "goal_id": "g1", "by_turn": 8,
                   "metric": "cities", "target": 4},
                  seq=11, pid=0, turn=3)
    recs += _pair("record_prediction",
                  {"text": "their city falls", "review_turn": 6,
                   "subject_id": "c9"},
                  seq=13, pid=0, turn=4)
    recs += _pair("record_lesson",
                  {"text": "g1 MET at t4: held 3", "about": "g1"},
                  seq=15, pid=0, turn=6)
    recs += [_end(seq=20, final_turn=8)]
    return recs


def _nodes(proj: Projection) -> dict[str, dict[str, Any]]:
    return {n["uuid"]: n for n in proj.nodes}


def _edges(proj: Projection) -> dict[tuple[str, str, str], dict[str, Any]]:
    return {(e["source"], e["rel"], e["target"]): e for e in proj.edges}


# ------------------------------------------------------------ determinism


def test_projection_is_pure_and_sorted():
    log = _rich_log()
    first, second = project(log), project(log)
    assert first.nodes == second.nodes
    assert first.edges == second.edges
    assert first.report == second.report
    assert [n["uuid"] for n in first.nodes] == sorted(n["uuid"] for n in
                                                      first.nodes)
    assert [e["uuid"] for e in first.edges] == sorted(e["uuid"] for e in
                                                      first.edges)


def test_projection_never_reads_wall_clock():
    # ts envelope fields must not influence the projection
    log = copy.deepcopy(_rich_log())
    for rec in log:
        rec["ts"] = "1999-12-31T00:00:00+00:00"
    assert project(log) == project(_rich_log())


def test_artifacts_are_byte_stable(tmp_path: Path):
    for _ in range(2):
        proj = project(_rich_log())
        paths = write_artifacts(tmp_path, proj)
    first = (paths["nodes"].read_text(), paths["edges"].read_text(),
             paths["report"].read_text())
    write_artifacts(tmp_path, project(_rich_log()))
    assert (paths["nodes"].read_text(), paths["edges"].read_text(),
            paths["report"].read_text()) == first
    assert json.loads(paths["report"].read_text())["match_id"] == "m"


def test_artifacts_round_trip(tmp_path: Path):
    proj = project(_rich_log())
    write_artifacts(tmp_path, proj)
    nodes, edges = load_artifacts(tmp_path)
    assert nodes == proj.nodes
    assert edges == proj.edges


# ------------------------------------------------------------ shape: spine


def test_spine_nodes_and_cross_match_group():
    proj = project(_rich_log())
    nodes = _nodes(proj)
    assert "match:m" in nodes and "agent:roman" in nodes and "agent:cart" in nodes
    spine = [nodes[u] for u in ("match:m", "agent:roman", "agent:cart")]
    assert all(n["group_id"] == "cross_match" for n in spine)
    assert spine[0]["seed"] == 7 and spine[0]["final_turn"] == 8
    assert json.loads(spine[0]["scores_json"])["ROME"]["player_id"] == 0
    edges = _edges(proj)
    assert ("agent:roman", "PLAYED_IN", "match:m") in edges
    assert ("agent:cart", "PLAYED_IN", "match:m") in edges
    # player node named by civ, wired to its agent
    p0 = nodes["m:main:player:p0"]
    assert p0["name"] == "ROME" and p0["agent_id"] == "roman" \
        and p0["policy"] == "llm"
    assert ("agent:roman", "PLAYED_AS", "m:main:player:p0") in edges
    assert nodes["m:main:player:p1"]["name"] == "KOREA"


def test_pre_m11_log_projects_spine_only():
    proj = project([_start(), _end(seq=1, final_turn=3)])
    assert proj.report["claims"] == {
        "goal_ids": 0, "goal_revisions": 0,
        "prediction_ids": 0, "prediction_revisions": 0, "lessons": 0}
    assert proj.report["nodes"]["total"] == 5  # match + 2 agents + 2 players
    assert proj.report["edges"]["by_rel"] == {
        "PLAYED_AS": 2, "PLAYED_IN": 2}
    assert all(":claim:" not in n["uuid"] and ":entity:" not in n["uuid"]
               for n in proj.nodes)


def test_no_match_start_raises():
    with pytest.raises(ValueError, match="MATCH_START"):
        project([_end(seq=0, final_turn=1)])


# ------------------------------------------------------------ shape: claims


def test_claim_nodes_revisions_and_supersedes():
    proj = project(_rich_log())
    nodes = _nodes(proj)
    g1r1 = nodes["m:main:claim:p0:g1:r1"]
    g1r2 = nodes["m:main:claim:p0:g1:r2"]
    assert g1r1["labels"] == ["Claim", "Goal"]
    assert g1r1["current"] is False and g1r2["current"] is True
    assert g1r1["valid_to_turn"] == 2 and g1r2["valid_from_turn"] == 3
    edges = _edges(proj)
    sup = edges[("m:main:claim:p0:g1:r2", "SUPERSEDES",
                 "m:main:claim:p0:g1:r1")]
    assert sup["amend_turn"] == 3
    # every revision carries an AUTHORED edge from its player
    for rev in ("r1", "r2"):
        assert ("m:main:player:p0", "AUTHORED",
                f"m:main:claim:p0:g1:{rev}") in edges
    # lessons project as one open revision with their about text
    lesson = nodes["m:main:claim:p0:l1:r1"]
    assert lesson["kind"] == "lesson" and lesson["about"] == "g1" \
        and lesson["valid_to_turn"] == 0


def test_references_resolve_to_revision_valid_at_referencing_turn():
    # l1 was written at t6, AFTER g1's amend at t3 -> points at r2
    proj = project(_rich_log())
    edges = _edges(proj)
    assert ("m:main:claim:p0:l1:r1", "REFERENCES",
            "m:main:claim:p0:g1:r2") in edges
    # a lesson written BEFORE the amend points at the revision it cited
    log = [
        _start(),
        *_pair("set_goal", {"text": "expand", "by_turn": 9}, seq=1,
               pid=0, turn=1),
        *_pair("record_lesson", {"text": "on track", "about": "g1"},
               seq=3, pid=0, turn=2),
        *_pair("set_goal", {"text": "expand more", "goal_id": "g1",
                            "by_turn": 9},
               seq=5, pid=0, turn=3),
        _end(seq=9, final_turn=4),
    ]
    edges = _edges(project(log))
    assert ("m:main:claim:p0:l1:r1", "REFERENCES",
            "m:main:claim:p0:g1:r1") in edges


def test_verdict_lesson_reference_survives_the_closure_it_causes():
    # a lesson about a DUE prediction closes it (valid_to = lesson_turn - 1);
    # the closure must not orphan the reference the verdict embodies — the
    # LESSON's REFERENCES edge still points at the prediction it judged.
    # Found on the real 002 run: l4/l9 -> p1/p3 were "dropped" before the
    # _revision_at fallback existed.
    log = [
        _start(),
        _obs("get_cities", {"own_cities": 2, "own_population": 4,
                            "foreign_cities": []}, seq=1, pid=0, turn=1),
        *_pair("record_prediction",
               {"text": "second city by t3", "review_turn": 3,
                "metric": "cities", "target": 3},
               seq=2, pid=0, turn=1),
        *_pair("record_lesson",
               {"text": "p1 MET: third city landed", "about": "p1"},
               seq=4, pid=0, turn=3),
        _end(seq=9, final_turn=4),
    ]
    proj = project(log)
    edges = _edges(proj)
    assert ("m:main:claim:p0:l1:r1", "REFERENCES",
            "m:main:claim:p0:p1:r1") in edges
    assert proj.report["dropped_references"] == []
    # the closed prediction is not "open at horizon" -> no arena verdict on
    # it; the lesson IS its verdict
    assert all(":outcome:p0:p1:" not in n["uuid"] for n in proj.nodes)


def test_references_to_entities_and_dangling_refs():
    log = _rich_log()
    # a prediction about a foreign city seen in the digest -> entity target
    proj = project(log)
    edges = _edges(proj)
    assert ("m:main:claim:p0:p1:r1", "REFERENCES",
            "m:main:entity:c9") in edges
    assert edges[("m:main:claim:p0:p1:r1", "REFERENCES",
                  "m:main:entity:c9")]["via"] == "subject_id"
    # a subject that matches nothing is dropped and reported, never guessed
    # (MATCH_END may sit mid-list — _meta takes the last one regardless)
    log += _pair("record_prediction",
                 {"text": "??", "review_turn": 7, "subject_id": "g9"},
                 seq=30, pid=0, turn=6)
    proj2 = project(log)
    assert any("g9" in d for d in proj2.report["dropped_references"])
    assert not any(e["rel"] == "REFERENCES" and "g9" in e["target"]
                   for e in proj2.edges)


def test_rejected_claims_never_project():
    log = [
        _start(),
        *_pair("set_goal", {"text": "nope", "confidence": 101}, seq=1,
               pid=0, turn=1, status="rejected"),
        _end(seq=4, final_turn=2),
    ]
    proj = project(log)
    assert proj.report["claims"]["goal_revisions"] == 0
    assert all(":claim:" not in n["uuid"] for n in proj.nodes)


# ------------------------------------------------------------ shape: verdicts


def test_verdict_outcomes_sticky_as_of_deadline():
    proj = project(_rich_log())
    nodes = _nodes(proj)
    edges = _edges(proj)
    # g1 r2: by t8, target 4 cities, only 3 ever observed -> MISSED with the
    # deadline-time value, not a later one
    outcome = nodes["m:main:outcome:p0:g1:r2"]
    assert outcome["verdict"] == "missed" and outcome["value"] == 3
    assert outcome["as_of_turn"] == 8 and outcome["due_turn"] == 8
    assert ("m:main:claim:p0:g1:r2", "VERDICT",
            "m:main:outcome:p0:g1:r2") in edges
    # p1 is due with no metric -> SELF_ASSESS, no value prop
    self_out = nodes["m:main:outcome:p0:p1:r1"]
    assert self_out["verdict"] == "self_assess" and "value" not in self_out


def test_not_due_claims_get_no_outcome():
    log = [
        _start(),
        *_pair("set_goal", {"text": "long game", "by_turn": 40,
                            "metric": "gold", "target": 1000},
               seq=1, pid=0, turn=1),
        *_pair("set_goal", {"text": "no deadline note"}, seq=3, pid=0,
               turn=1),
        _end(seq=6, final_turn=5),
    ]
    proj = project(log)
    assert all(":outcome:" not in n["uuid"] for n in proj.nodes)
    assert "VERDICT" not in proj.report["edges"]["by_rel"]


# ------------------------------------------------------------ shape: beliefs


def test_observed_edges_carry_the_last_seen_snapshot():
    proj = project(_rich_log())
    nodes = _nodes(proj)
    entity = nodes["m:main:entity:c9"]
    assert entity["labels"] == ["Entity", "City"] and entity["kind"] == "city"
    edge = _edges(proj)[("m:main:player:p0", "OBSERVED",
                         "m:main:entity:c9")]
    assert edge["last_seen_turn"] == 4 and edge["last_seen_seq"] == 9
    fields = json.loads(edge["fields_json"])
    assert fields["city_id"] == "c9" and fields["name"] == "SEOUL"


def test_entity_kind_conflict_settled_in_player_order():
    log = [
        _start(),
        # p0 sees c9 as a city...
        _obs("get_cities", {"own_cities": 1, "own_population": 2,
                            "foreign_cities": [{"city_id": "c9",
                                                "name": "X", "coord": "0,0",
                                                "owner_id": 1, "hp": 100,
                                                "population": 1}]},
             seq=1, pid=0, turn=1),
        # ...p1 sees an id-equal entry as a unit
        _obs("get_units", {"own_units": 1,
                           "foreign_units": [{"unit_id": "c9",
                                              "type": "WARRIOR",
                                              "coord": "0,0", "owner_id": 0,
                                              "hp_bucket": 2}]},
             seq=2, pid=1, turn=1, agent="cart"),
        _end(seq=3, final_turn=2),
    ]
    proj = project(log)
    entity = _nodes(proj)["m:main:entity:c9"]
    assert entity["labels"] == ["Entity", "City"]
    assert proj.report["entity_kind_conflicts"] == \
        ["c9: kept city, saw ['unit']"]


# ------------------------------------------------------------ CLI


def test_project_cli_writes_artifacts(tmp_path: Path, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in _rich_log()))
    project_cli.main([str(run_dir)])
    out = capsys.readouterr().out
    assert "match m" in out and "nodes 12" in out
    gdir = run_dir / "graph"
    assert (gdir / "nodes.jsonl").exists()
    assert (gdir / "edges.jsonl").exists()
    assert (gdir / "projection.json").exists()


def test_load_records_tolerates_torn_tail(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    good = json.dumps(_start(), sort_keys=True) + "\n"
    path.write_text(good + '{"kind":"TOOL_RESULT","seq":1')
    assert [r["kind"] for r in load_records(path)] == ["MATCH_START"]
