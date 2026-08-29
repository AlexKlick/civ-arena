"""The M12 graph projection: the event log -> Graphiti-shaped nodes/edges,
as a pure deterministic function (artifacts are the portable truth).
"""

from __future__ import annotations

import copy
import json
import os
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
          turn: int = 1, agent: str = "roman", match_id: str = "m",
          status: str = "accepted") -> list[dict[str, Any]]:
    return [
        _call(tool, args, seq=seq, pid=pid, turn=turn, agent=agent,
              match_id=match_id),
        _result(tool, seq=seq + 1, pid=pid, turn=turn, agent=agent,
                match_id=match_id, status=status),
    ]


def _obs(tool: str, observed: dict[str, Any], *, seq: int, pid: int = 0,
         turn: int = 1, agent: str = "roman",
         match_id: str = "m") -> dict[str, Any]:
    return _result(tool, seq=seq, pid=pid, turn=turn, agent=agent,
                   match_id=match_id, observed=observed)


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


def test_same_turn_amend_reference_resolves_by_seq():
    # a lesson written at t1 AFTER two same-turn amends points at the
    # revision that was authoritative when it was WRITTEN (r3), not the
    # stale r1 — turns cannot express intra-turn order, created_seq can
    log = [
        _start(),
        *_pair("set_goal", {"text": "v1"}, seq=1, pid=0, turn=1),
        *_pair("set_goal", {"text": "v2", "goal_id": "g1"}, seq=3, pid=0,
               turn=1),
        *_pair("set_goal", {"text": "v3", "goal_id": "g1"}, seq=5, pid=0,
               turn=1),
        *_pair("record_lesson", {"text": "about g1", "about": "g1"},
               seq=7, pid=0, turn=1),
        _end(seq=9, final_turn=2),
    ]
    proj = project(log)
    assert ("m:main:claim:p0:l1:r1", "REFERENCES",
            "m:main:claim:p0:g1:r3") in _edges(proj)
    assert proj.report["dropped_references"] == []


def test_same_turn_amended_prediction_closed_by_lesson_resolves_latest():
    # review round-1 extra pin: amend a prediction twice within one turn,
    # then close it with a verdict lesson in a LATER turn — the lesson
    # references the revision it actually judged (r2), never the stale r1
    log = [
        _start(),
        *_pair("record_prediction", {"text": "p v1", "review_turn": 3},
               seq=1, pid=0, turn=1),
        *_pair("record_prediction",
               {"text": "p v2", "prediction_id": "p1", "review_turn": 3},
               seq=3, pid=0, turn=1),
        *_pair("record_lesson", {"text": "p1 verdict: held", "about": "p1"},
               seq=5, pid=0, turn=3),
        _end(seq=7, final_turn=4),
    ]
    proj = project(log)
    assert ("m:main:claim:p0:l1:r1", "REFERENCES",
            "m:main:claim:p0:p1:r2") in _edges(proj)
    assert proj.report["dropped_references"] == []


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
    renumbered = []
    for rec in _rich_log():
        rec = dict(rec)
        rec["seq"] = len(renumbered)  # the strict loader enforces seq==position
        renumbered.append(rec)
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in renumbered))
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


def test_load_records_rejects_midfile_corruption_and_broken_seq(tmp_path: Path):
    # EventLog._load discipline: only the FINAL line may be torn; anything
    # else fails closed instead of truncating to a valid-looking prefix
    path = tmp_path / "events.jsonl"
    good = json.dumps({"kind": "MATCH_START", "seq": 0}) + "\n"
    path.write_text(good + '{"broken\n' + good)
    with pytest.raises(ValueError, match="corrupt event log line 1"):
        load_records(path)
    path.write_text(good + json.dumps({"kind": "TURN_END", "seq": 5}) + "\n")
    with pytest.raises(ValueError, match="seq broken"):
        load_records(path)


def test_unsafe_match_and_agent_ids_fail_loudly():
    # review round-2 P1: an operator-chosen match_id containing ':' could
    # mint a match-scoped uuid identical to another match's SPINE uuid
    # (agent:/match:), and reconciliation would then delete spine data. The
    # charset makes cross-namespace collisions structurally impossible.
    with pytest.raises(ValueError, match="unsafe match_id 'agent:q'"):
        project([_start(match_id="agent:q")])
    with pytest.raises(ValueError, match="unsafe agent_id"):
        project([_start(agents=[["roman:main:player:p0", 0, "llm"]])])
    with pytest.raises(ValueError, match="unsafe match_id"):
        project([_start(match_id="m|PLAYED_IN|match:m")])


def test_pipe_bearing_entity_ids_keep_edge_uuids_distinct():
    # entity ids ride behind a fixed "…:entity:" prefix, so they cannot
    # cross namespaces — but they may contain '|', and the hashed edge
    # uuids stay distinct for any two different edges
    log = [
        _start(),
        _obs("get_units",
             {"own_units": 1, "foreign_units": [
                 {"unit_id": "u1|OBSERVED|x", "type": "WARRIOR",
                  "coord": "0,0", "owner_id": 1, "hp_bucket": 2},
                 {"unit_id": "u2|OBSERVED|x", "type": "ARCHER",
                  "coord": "1,0", "owner_id": 1, "hp_bucket": 2}]},
             seq=1, pid=0, turn=1),
        _end(seq=2, final_turn=1),
    ]
    proj = project(log)
    uuids = [e["uuid"] for e in proj.edges]
    assert len(set(uuids)) == len(uuids)
    assert all(u.startswith("e:") and len(u) == 42 and "|" not in u
               for u in uuids)


def test_foreign_turn_end_never_inflates_the_horizon():
    # review round-2 P2: the fallback horizon is computed over MATCH-BOUND
    # records — a foreign match's TURN_END cannot create premature outcomes
    log = [_start(match_id="m1"),
           {"kind": "TURN_END", "seq": 1, "match_id": "m1", "turn": 3},
           {"kind": "TURN_END", "seq": 2, "match_id": "m2", "turn": 999}]
    proj = project(log)
    assert proj.report["final_turn"] == 3


def test_reference_to_a_future_claim_drops_rather_than_binding_an_entity():
    # review round-2 P2: subject_id naming a claim that does not exist YET
    # must drop — falling through to a same-id entity misbinds silently
    log = [
        _start(),
        _obs("get_units",
             {"own_units": 1, "foreign_units": [
                 {"unit_id": "g1", "type": "WARRIOR", "coord": "0,0",
                  "owner_id": 1, "hp_bucket": 2}]},
             seq=1, pid=0, turn=1),
        *_pair("record_prediction",
               {"text": "about g1", "review_turn": 5, "subject_id": "g1"},
               seq=2, pid=0, turn=1),
        *_pair("set_goal", {"text": "the real g1", "by_turn": 9},
               seq=4, pid=0, turn=2),
        _end(seq=6, final_turn=3),
    ]
    proj = project(log)
    assert not any(e["rel"] == "REFERENCES" for e in proj.edges)
    assert any("'g1'" in d and "subject_id" in d
               for d in proj.report["dropped_references"])


def test_agent_node_carries_no_policy_but_the_edge_does():
    # review round-1 P2: policy is a per-MATCH fact; on the shared Agent
    # node it would make DB contents load-order dependent across matches
    proj = project(_rich_log())
    nodes = _nodes(proj)
    assert "policy" not in nodes["agent:roman"]
    edge = _edges(proj)[("agent:roman", "PLAYED_AS", "m:main:player:p0")]
    assert edge["policy"] == "llm"
    assert nodes["m:main:player:p0"]["policy"] == "llm"  # match-scoped copy


def test_concatenated_or_mismatched_lifecycle_fails_loudly():
    # review round-1 P2: a concatenated log must not mix one match's
    # envelope with another's claims
    with pytest.raises(ValueError, match="disagree on match_id"):
        project([_start(match_id="m1"), _start(match_id="m2")])
    with pytest.raises(ValueError, match="does not match"):
        project([_start(match_id="m1"), _end(seq=1, match_id="m2")])


def test_foreign_match_records_never_leak_into_the_graph():
    log = [_start(match_id="m1")]
    for rec in _pair("set_goal", {"text": "foreign"}, seq=1, pid=0, turn=1):
        rec = dict(rec, match_id="m2")
        log.append(rec)
    log += [_end(seq=3, match_id="m1", final_turn=2)]
    proj = project(log)
    assert proj.report["claims"]["goal_revisions"] == 0
    assert all(":claim:" not in n["uuid"] for n in proj.nodes)


def test_resume_prefix_without_match_end_falls_back_to_turn_end():
    log = [_start(),
           {"kind": "TURN_END", "seq": 1, "match_id": "m", "turn": 3},
           {"kind": "TURN_END", "seq": 2, "match_id": "m", "turn": 7}]
    proj = project(log)
    assert proj.report["final_turn"] == 7


# ------------------------------------------------------------ loader (M12b)


def test_node_batches_group_validate_and_chunk():
    from civ_arena.graph.load import node_batches
    nodes = [
        {"uuid": "a", "group_id": "g", "labels": ["Claim", "Goal"],
         "name": "a", "text": "x", "current": True},
        {"uuid": "b", "group_id": "g", "labels": ["Claim", "Goal"],
         "name": "b", "text": "y", "current": False},
        {"uuid": "c", "group_id": "g", "labels": ["Entity", "City"],
         "name": "c", "kind": "city"},
    ]
    batches = list(node_batches(nodes, batch=1))
    # grouped by label tuple, sorted, chunked at 1
    assert [labels for labels, _ in batches] == [
        ("Claim", "Goal"), ("Claim", "Goal"), ("Entity", "City")]
    assert [r["uuid"] for r in batches[0][1]] == ["a"]
    # uuid rides inside props so the replacing SET keeps the MERGE key
    assert batches[0][1][0]["props"] == {
        "group_id": "g", "name": "a", "text": "x", "current": True,
        "uuid": "a"}


def test_node_batches_rejects_unsafe_labels():
    from civ_arena.graph.load import node_batches
    nodes = [{"uuid": "a", "group_id": "g", "labels": ["Claim); MATCH (x)"],
              "name": "a"}]
    with pytest.raises(Exception, match="unsafe label"):
        list(node_batches(nodes))


def test_edge_batches_group_by_rel_and_validate():
    from civ_arena.graph.load import edge_batches
    edges = [
        {"uuid": "a|AUTHORED|b", "source": "a", "target": "b",
         "rel": "AUTHORED"},
        {"uuid": "b|SUPERSEDES|c", "source": "b", "target": "c",
         "rel": "SUPERSEDES", "amend_turn": 3},
    ]
    batches = list(edge_batches(edges))
    assert [(rel, len(rows)) for rel, rows in batches] == \
        [("AUTHORED", 1), ("SUPERSEDES", 1)]
    assert batches[1][1][0]["props"] == {"amend_turn": 3,
                                         "uuid": "b|SUPERSEDES|c"}
    with pytest.raises(Exception, match="unsafe relationship"):
        list(edge_batches([{"uuid": "x", "source": "a", "target": "b",
                            "rel": "REL) DETACH DELETE n"}]))


# ------------------------------------------------------------ queries (M12c)


class _FakeSession:
    """Duck-typed session: run() returns canned mappings."""

    def __init__(self, rows_by_fragment: dict[str, list[dict]]):
        self.rows_by_fragment = rows_by_fragment
        self.queries: list[str] = []

    def run(self, cypher: str, **params: Any):
        self.queries.append(cypher)
        for fragment, rows in self.rows_by_fragment.items():
            if fragment in cypher:
                return list(rows)
        return []


def test_match_of_strips_group_suffix():
    from civ_arena.graph.query import match_of
    assert match_of("llm-vs-turtler-002:main") == "llm-vs-turtler-002"
    assert match_of("plain") == "plain"


def test_lessons_query_joins_agent_through_players():
    from civ_arena.graph.query import lessons
    session = _FakeSession({"-[:AUTHORED]->(l:Claim:Lesson)": [
        {"group_id": "m1:main", "lesson_id": "l1", "text": "g6 MET",
         "about": "g6", "turn": 12, "about_id": "g6",
         "about_verdict": "met"},
        {"group_id": "m2:main", "lesson_id": "l1", "text": "note",
         "about": "", "turn": 3, "about_id": None, "about_verdict": None},
    ]})
    rows = lessons(session, "roman")
    assert len(rows) == 2 and rows[0]["about_verdict"] == "met"
    assert "$agent" in session.queries[0]  # parameterized, never interpolated


def test_goal_outcomes_filter_and_tally():
    from civ_arena.graph.query import tally
    rows = [
        {"metric": "cities", "verdict": "met", "status": "active"},
        {"metric": "cities", "verdict": "missed", "status": "active"},
        {"metric": "cities", "verdict": None, "status": "done"},
        {"metric": "", "verdict": None, "status": "dropped"},
    ]
    assert tally(rows) == {
        "cities": {"met": 1, "missed": 1, "done": 1},
        "(none)": {"dropped": 1},
    }


def test_chains_prefix_pins_the_claim_not_its_neighbors():
    from civ_arena.graph.query import chains
    session = _FakeSession({})
    chains(session, "m", 0, "g1")
    # STARTS WITH $prefix with the trailing colon: g1's revisions, never g10's
    assert "$prefix" in session.queries[0]
    assert "m:main:claim:p0:g1:" not in session.queries[0].split("$prefix")[0]


def test_query_cli_requires_subcommand():
    from civ_arena.graph import query as query_mod
    with pytest.raises(SystemExit):
        query_mod.main([])


def test_token_rejects_trailing_newline():
    # review round-1 P3: Python's `$` matches before a final newline —
    # fullmatch closes it
    from civ_arena.graph.load import LoadError, _token
    with pytest.raises(LoadError, match="unsafe label"):
        _token("label", "Safe\n")
    assert _token("label", "Safe") == "Safe"


def test_node_query_replaces_props_and_removes_stale_labels():
    from civ_arena.graph.load import node_query
    query = node_query(("Entity", "City"))
    assert "SET n = row.props" in query  # replace: omitted props clear
    assert " SET n :City" in query
    assert " REMOVE n:Unit" in query and " REMOVE n:Claim" in query
    assert " REMOVE n:City" not in query and " REMOVE n:Entity" not in query


class _RecordingSession:
    """Session double: counts transactions, records queries/params."""

    def __init__(self):
        self.calls = 0
        self.queries: list[tuple[str, dict]] = []

    def execute_write(self, work):
        self.calls += 1
        session = self

        class _Tx:
            def run(self, query, **params):
                session.queries.append((query, params))
                return self

            def consume(self):
                return None
        work(_Tx())


class _ExplodingSession:
    def execute_write(self, work):  # pragma: no cover - must never run
        raise AssertionError("write attempted before prevalidation")


def test_load_plan_is_one_transaction_with_reconcile():
    from civ_arena.graph.load import load_into_db
    nodes = [
        {"uuid": "agent:roman", "group_id": "cross_match",
         "labels": ["Agent"], "name": "roman", "agent_id": "roman"},
        {"uuid": "m:main:player:p0", "group_id": "m:main",
         "labels": ["Player"], "name": "p0", "player_id": 0},
    ]
    edges = [
        {"uuid": "e:1", "source": "agent:roman", "target": "m:main:player:p0",
         "rel": "PLAYED_AS", "group_id": "m:main", "policy": "llm"},
    ]
    session = _RecordingSession()
    counts = load_into_db(nodes, edges, session=session)
    # legacy cleanup, agent nodes, player nodes, PLAYED_AS,
    # reconcile nodes, reconcile edges
    assert counts["queries"] == 6
    assert session.calls == 1  # ONE transaction: all-or-nothing
    # the legacy pipe-uuid sweep runs FIRST, before any other statement
    assert "CONTAINS '|'" in session.queries[0][0]
    assert any("DETACH DELETE" in q for q, _ in session.queries)
    reconcile = [p for q, p in session.queries if "DETACH DELETE" in q][0]
    assert reconcile["group"] == "m:main"
    assert reconcile["keep_nodes"] == [n["uuid"] for n in nodes]
    assert reconcile["keep_edges"] == ["e:1"]


def test_unsafe_tokens_fail_before_any_write():
    from civ_arena.graph.load import load_into_db
    bad = [{"uuid": "x", "group_id": "g", "labels": ["X); DETACH DELETE n"],
            "name": "x"}]
    with pytest.raises(Exception, match="unsafe label"):
        load_into_db(bad, [], session=_ExplodingSession())


def test_loader_module_imports_without_driver():
    # the core must stay importable when the optional graph group is absent
    import civ_arena.graph.load as loader  # noqa: F401  (import is the test)
    assert loader.config_from_env()["uri"].startswith("bolt://")


def test_load_error_mentions_remedies():
    from civ_arena.graph.load import LoadError, load_into_db
    with pytest.raises(LoadError) as excinfo:
        load_into_db([{"uuid": "a", "labels": ["X"], "group_id": "g",
                       "name": "a"}], [], uri="bolt://127.0.0.1:1")
    # driver missing OR dead port — either way the message must carry the fix
    assert ("uv sync --group graph" in str(excinfo.value)
            or "docker compose" in str(excinfo.value))


def test_load_artifacts_missing(tmp_path: Path):
    from civ_arena.graph.load import LoadError, load_artifacts_into_db
    with pytest.raises(LoadError, match="no graph artifacts"):
        load_artifacts_into_db(tmp_path)


def test_live_db_round_trip_idempotent():
    """Opt-in live test: set CIV_ARENA_NEO4J_TEST=1 with the container up and
    the graph group synced (`uv run --group graph pytest ...`). Skipped in
    every normal gate — local tests never require a running DB. This is the
    only place cypher actually executes — it caught the GraphNode
    anchor-label bug, so fixtures go through project(), never hand-built."""
    pytest.importorskip("neo4j")
    if os.environ.get("CIV_ARENA_NEO4J_TEST") != "1":
        pytest.skip("live DB test: set CIV_ARENA_NEO4J_TEST=1 to enable")
    from neo4j import GraphDatabase

    from civ_arena.graph.load import config_from_env, load_into_db

    def _live_log(with_goal: bool = True) -> list[dict[str, Any]]:
        recs = [_start(match_id="live-1",
                       agents=[["roman-live", 0, "llm"],
                               ["cart-live", 1, "turtler"]])]
        if with_goal:
            recs += _pair("set_goal", {"text": "3 cities", "by_turn": 2,
                                       "metric": "cities", "target": 3},
                          seq=1, pid=0, turn=1, agent="roman-live",
                          match_id="live-1")
        recs += [_obs("get_cities",
                      {"own_cities": 2, "own_population": 4,
                       "foreign_cities": [{"city_id": "c9", "name": "Y",
                                           "coord": "0,0", "owner_id": 1,
                                           "hp": 100, "population": 1}]},
                      seq=3, pid=0, turn=1, agent="roman-live",
                      match_id="live-1")]
        recs += [_end(seq=9, match_id="live-1", final_turn=2)]
        return recs

    proj = project(_live_log())
    first = load_into_db(proj.nodes, proj.edges)
    second = load_into_db(proj.nodes, proj.edges)  # MERGE by uuid: a no-op
    assert first == second

    cfg = config_from_env()
    driver = GraphDatabase.driver(
        cfg["uri"], auth=(cfg["user"], cfg["password"]) if cfg["password"]
        else None)

    def _count(session: Any, query: str) -> int:
        return session.run(query).single()["c"]

    try:
        with driver.session() as session:
            # seed a LEGACY pipe-uuid, group-less edge between two live
            # nodes — the next load's migration sweep must remove it
            session.run(
                "MATCH (a {uuid: 'agent:roman-live'}) "
                "MATCH (b {uuid: 'match:live-1'}) "
                "MERGE (a)-[r:LEGACY {uuid: 'agent:roman-live|LEGACY|"
                "match:live-1'}]->(b)")
            legacy_before = _count(
                session, "MATCH ()-[r]->() WHERE r.uuid CONTAINS '|' "
                         "AND (r.uuid STARTS WITH 'agent:roman-live') "
                         "RETURN count(r) AS c")
            assert legacy_before == 1

        # review round-1 P2 pin: re-loading a CHANGED projection reconciles
        # — the goal, its outcome and every match-scoped edge disappear, the
        # spine survives. Round-2 P1 pin: the legacy edge is swept too.
        slim = project(_live_log(with_goal=False))
        load_into_db(slim.nodes, slim.edges)

        with driver.session() as session:
            group_nodes = _count(
                session, "MATCH (n) WHERE n.group_id = 'live-1:main' "
                         "RETURN count(n) AS c")
            group_edges = _count(
                session, "MATCH ()-[r]->() WHERE r.group_id = 'live-1:main' "
                         "RETURN count(r) AS c")
            legacy_after = _count(
                session, "MATCH ()-[r]->() WHERE r.uuid CONTAINS '|' "
                         "AND (r.uuid STARTS WITH 'agent:roman-live') "
                         "RETURN count(r) AS c")
            agent = _count(session, "MATCH (n:Agent "
                                    "{uuid: 'agent:roman-live'}) "
                                    "RETURN count(n) AS c")
            played_in = _count(
                session, "MATCH ()-[r:PLAYED_IN]->(:Match "
                         "{uuid: 'match:live-1'}) RETURN count(r) AS c")
            dupes = session.run(
                "MATCH (n) WITH n.uuid AS u, count(*) AS c WHERE c > 1 "
                "RETURN count(*) AS c").single()["c"]
            session.run(
                "MATCH (n) WHERE n.uuid STARTS WITH 'live-1:' "
                "OR n.uuid IN ['agent:roman-live', 'agent:cart-live', "
                "'match:live-1'] DETACH DELETE n")
    finally:
        driver.close()
    # slim projection keeps the players, the observed entity and their
    # edges — the goal/claim/outcome are reconciled away
    assert group_nodes == 3  # 2 players + entity c9
    assert group_edges == 3  # 2 PLAYED_AS + 1 OBSERVED
    assert legacy_after == 0  # migrated away
    # spine intact: the agent exists exactly once, and BOTH roster agents
    # still play in the match — exactly one PLAYED_IN edge each
    assert agent == 1 and played_in == 2
    assert dupes == 0  # no duplicate uuids anywhere in the DB
