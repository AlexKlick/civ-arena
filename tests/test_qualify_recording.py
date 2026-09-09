"""Recording qualification harness (lane A): one captured run, one verdict.

The fixture builder writes a driven dispatch-hotseat run in the shape the
harness qualifies — per turn and agent a boundary, the tool pairs and the
completed-seat audit row, PLUS one HEARTBEAT audit="spectator_world" per
contract position (after_seat == [-1] + [0, 1] * rounds). Tests drive the
harness through main([...]) so verdicts come back as real exit codes.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "qualify_recording.py"
BASELINE_PATH = REPO / "configs" / "qual-warning-baseline.json"
TS = "2026-09-08T00:00:00+00:00"
MATCH_ID = "qual-match"
INSTANCE = "qual-match-i1"
AGENTS = ({"agent_id": "agent-0", "player_id": 0, "policy": "planner", "seed": 7},
          {"agent_id": "agent-1", "player_id": 1, "policy": "turtler", "seed": 11})
LIMITS = {"agent_turn": 600.0, "match": 3600.0, "sweeps": 3, "recovery": 60.0,
          "startup": 120.0, "spectator": 8.0}


def _harness():
    spec = importlib.util.spec_from_file_location("qualify_recording", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qual = _harness()


def _world(after_seat: int) -> dict:
    """Minimal admitted spectator world (validate_world closed shape)."""
    return {"schema": 1, "after_seat": after_seat,
            "contexts": {"roster": "gamecore", "tiles": "gamecore",
                         "palette": "ingame"},
            "grid": {"w": 4, "h": 4}, "game_era": "ERA_FAKE",
            "roster": [{"player_id": 0, "kind": "major"},
                       {"player_id": 1, "kind": "major"}],
            "players": [{"player_id": 0}, {"player_id": 1}], "cities": [],
            "fog_audit": {"requested": 0, "engine_visible": 0,
                          "engine_not_visible": 0, "unavailable": 0,
                          "disagree_coords": []},
            "truncated": {"tiles": False, "world": False}, "read_ms": 1.0}


def _write_qual_run(tmp_path: Path, rounds: int, name: str = "qual-run") -> Path:
    run_dir = tmp_path / name
    run_dir.mkdir(parents=True)
    identity = {"commit": "0123456789abcdef0123456789abcdef01234567",
                "tree": "f" * 40, "dirty": False, "mod_sha256": "a" * 64,
                "config": {"match_id": MATCH_ID, "spectator_capture": True,
                           "agents": [dict(a) for a in AGENTS]}}
    rows: list[dict] = []

    def add(kind: str, **fields):
        rows.append({"schema": 1, "seq": len(rows), "kind": kind, "ts": TS,
                     "match_id": MATCH_ID, "game_instance_id": INSTANCE,
                     "turn": fields.pop("turn", 0),
                     "phase_player_id": fields.pop("phase_player_id", 0),
                     "player_id": fields.pop("player_id", None),
                     "agent_id": fields.pop("agent_id", None),
                     "visibility_scope": fields.pop("visibility_scope", "referee"),
                     **fields})
        return rows[-1]

    def spectator(after_seat: int, turn: int):
        add("HEARTBEAT", turn=turn, phase_player_id=-1, player_id=None, agent_id=None,
            visibility_scope="spectator", audit="spectator_world",
            after_seat=after_seat, world=_world(after_seat))

    per_turn: list[dict] = []
    add("MATCH_START", config={"match_id": MATCH_ID, "spectator_capture": True,
                               "agents": [dict(a) for a in AGENTS]})
    add("HEARTBEAT", audit="run_identity", identity=identity, fake=True,
        limits=dict(LIMITS), movement_allowance=False)
    spectator(-1, 1)  # baseline capture after the initial attach, turn 1
    for turn in range(1, rounds + 1):
        for agent in AGENTS:
            pid, aid = agent["player_id"], agent["agent_id"]
            add("HEARTBEAT", turn=turn, player_id=pid, agent_id=aid,
                audit="decision_boundary", decision_id=f"d{turn}-{pid}",
                directive_id=f"dir{turn}-{pid}", provider_requests=0, source="model")
            add("LEASE_GRANT", turn=turn, player_id=pid, agent_id=aid,
                lease_id=f"l{turn}-{pid}")
            add("TOOL_CALL", turn=turn, player_id=pid, agent_id=aid, tool="get_units",
                args={}, args_digest="x")
            add("TOOL_RESULT", turn=turn, player_id=pid, agent_id=aid,
                tool="get_units", status="accepted", observed={"own_units": 2})
            add("TOOL_CALL", turn=turn, player_id=pid, agent_id=aid, tool="move_unit",
                args={"unit_id": f"u{pid}:1", "dest": "1,1"}, args_digest="y")
            add("TOOL_RESULT", turn=turn, player_id=pid, agent_id=aid,
                tool="move_unit", status="accepted",
                mutations=[{"kind": "unit.moved", "entity_type": "unit",
                            "entity_id": f"u{pid}:1", "attr": "pos",
                            "before": "0,0", "after": "1,1"}],
                receipts=[{"kind": "unit.moved"}])
            add("TOOL_CALL", turn=turn, player_id=pid, agent_id=aid, tool="end_turn",
                args={}, args_digest="z")
            add("TOOL_RESULT", turn=turn, player_id=pid, agent_id=aid, tool="end_turn",
                status="accepted")
            add("TURN_END", turn=turn, player_id=pid, agent_id=aid)
            row = {"turn": turn, "player": pid, "agent": aid, "lease_released": True,
                   "elapsed_s": 2.5, "requests": 0, "calls": 3, "allowed_mutations": 1,
                   "violations": 0}
            per_turn.append(row)
            add("HEARTBEAT", turn=turn, player_id=pid, agent_id=aid,
                audit="completed_seat_turn", row=dict(row))
            spectator(pid, turn)  # capture after the seat's completed turn
            add("LEASE_RELEASE", turn=turn, player_id=pid, agent_id=aid,
                lease_id=f"l{turn}-{pid}")
    add("HEARTBEAT", audit="play_complete", elapsed_s=5.0, turn=rounds)
    summary = {"match_id": MATCH_ID, "game_instance_id": INSTANCE,
               "phase": "dispatch-hotseat", "clean": True, "aborted": None,
               "failure_reason": None, "completed_rounds": rounds,
               "requested_rounds": rounds, "final_turn": rounds,
               "violations_total": 0, "cleanup": {"status": "completed"},
               "identity": identity, "per_turn": per_turn,
               "movement_allowance": False, "limits": dict(LIMITS)}
    add("MATCH_END", turn=rounds, summary=summary)
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n")
    (run_dir / "summary.json").write_text(json.dumps(summary, sort_keys=True))
    return run_dir


def _rewrite(run_dir: Path, rows: list[dict]) -> None:
    """Renumber seqs (the exporter and viewer require contiguity) and rewrite."""
    for index, row in enumerate(rows):
        row["seq"] = index
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n")


def _read_rows(run_dir: Path) -> list[dict]:
    return [json.loads(line) for line
            in (run_dir / "events.jsonl").read_text().splitlines() if line.strip()]


def _run(run_dir: Path, tmp_path: Path, *extra: str) -> tuple[int, dict, Path, Path]:
    out = tmp_path / "qual-out"
    argv = [str(run_dir), *extra]
    if "--json" not in extra:
        argv += ["--json", str(out / "qualification.json")]
    if "--report" not in extra:
        argv += ["--report", str(out / "report.md")]
    if "--export-out" not in extra:
        argv += ["--export-out", str(out / "samples.jsonl")]
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = qual.main(argv)
    json_path = out / "qualification.json"
    result = json.loads(json_path.read_text()) if json_path.exists() else {}
    return code, result, json_path, out / "report.md"


# -- required case 1: positive --------------------------------------------------


def test_two_round_fixture_qualifies_pass(tmp_path):
    run_dir = _write_qual_run(tmp_path, 2)
    code, result, json_path, report = _run(run_dir, tmp_path, "--rounds", "2",
                                           "--allow-fake")
    assert code == 0, result.get("failures")
    assert json_path.exists() and report.exists()
    assert result["schema"] == 1
    assert result["verdict"] == "PASS" and result["failures"] == []
    assert result["rounds"] == 2 and result["allow_fake"] is True
    assert result["run_dir"] == str(run_dir)
    assert result["audit"]["after_seat_sequence"] == [-1, 0, 1, 0, 1]
    assert result["audit"]["spectator_world_rows"] == \
        result["audit"]["expected_rows"] == 5
    assert result["audit"]["spectator_world_failed"] == 0
    assert result["audit"]["roster_kinds_observed"] == {"major": 10}
    assert result["audit"]["entity_class_source"] == \
        "roster PLAYERROW kind — named, never inferred"
    assert result["audit"]["identity"] == {
        "commit": "0123456789abcdef0123456789abcdef01234567", "dirty": False,
        "mod_sha256": "a" * 64, "fake": True}
    assert result["audit"]["validate"]["status"] == "PASS"
    assert result["viewer"]["status"] == "completed"
    assert result["viewer"]["new_warnings"] == []
    assert result["viewer"]["check"] == "in-process DashboardStore.load"
    assert result["export"]["sample_counts"] == {"controlled_decision": 4}
    assert result["export"]["ref_integrity"]["refs_checked"] == 16
    assert result["export"]["ref_integrity"]["failures"] == 0
    assert result["export"]["flag_histogram"] == \
        {"controlled_decision": {"pre_ledger_run": 4}}
    assert result["coverage"] is None
    exported = Path(result["export"]["output"])
    assert hashlib.sha256(exported.read_bytes()).hexdigest() == \
        result["export"]["output_sha256"]


def test_report_skeleton_keeps_human_cells_pending(tmp_path):
    run_dir = _write_qual_run(tmp_path, 1)
    code, result, _, report = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake")
    assert code == 0
    text = report.read_text()
    assert "| Run id | `qual-run` |" in text
    assert "| Mod version | *(pending operator fill)* |" in text
    assert "| Allowed uses for THIS recording | *(pending operator fill)* |" in text
    assert "| Ref-integrity failures | 0 (of 8 refs checked) |" in text
    assert '"pre_ledger_run": 2' in text  # machine-filled flag histogram
    assert "Verdict: **PASS**" in text


# -- required case 2: spectator_world_failed splice -----------------------------


def test_spliced_spectator_world_failed_fails_the_verdict(tmp_path):
    run_dir = _write_qual_run(tmp_path, 1)
    rows = _read_rows(run_dir)
    anchor = next(i for i, row in enumerate(rows)
                  if row.get("audit") == "completed_seat_turn")
    rows.insert(anchor + 1, {"schema": 1, "kind": "HEARTBEAT", "ts": TS,
                             "match_id": MATCH_ID, "game_instance_id": INSTANCE,
                             "turn": 1, "phase_player_id": -1, "player_id": None,
                             "agent_id": None, "visibility_scope": "spectator",
                             "audit": "spectator_world_failed", "after_seat": 0,
                             "error": "RuntimeError: capture boom"})
    _rewrite(run_dir, rows)
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake")
    assert code == 1
    assert result["verdict"] == "FAIL"
    assert result["audit"]["spectator_world_failed"] == 1
    assert any("spectator_world_failed" in failure for failure in result["failures"])


# -- required case 3: dropped spectator_world row -------------------------------


def test_dropped_spectator_world_row_breaks_the_row_contract(tmp_path):
    run_dir = _write_qual_run(tmp_path, 1)
    rows = _read_rows(run_dir)
    dropped = next(row for row in rows
                   if row.get("audit") == "spectator_world" and row["after_seat"] == 1)
    rows.remove(dropped)
    _rewrite(run_dir, rows)
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake")
    assert code == 1
    assert result["verdict"] == "FAIL"
    assert result["audit"]["spectator_world_rows"] == 2
    assert result["audit"]["expected_rows"] == 3
    assert result["audit"]["after_seat_sequence"] == [-1, 0]
    assert any("row contract" in failure for failure in result["failures"])


# -- required case 4: corrupted exported reference ------------------------------


def test_corrupted_exported_ref_fails_integrity(tmp_path):
    run_dir = _write_qual_run(tmp_path, 1)
    export_out = tmp_path / "samples.jsonl"
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake",
                              "--export-out", str(export_out))
    assert code == 0
    samples = [json.loads(line) for line in export_out.read_text().splitlines()]
    victim = next(sample for sample in samples if sample["observation_refs"])
    victim["observation_refs"][0]["seq"] = 99999
    export_out.write_text(
        "\n".join(json.dumps(sample, sort_keys=True) for sample in samples) + "\n")
    integrity = qual.check_ref_integrity(export_out,
                                         (run_dir / "events.jsonl").read_bytes())
    assert integrity["refs_checked"] == 8
    assert integrity["failures"] == 1
    assert integrity["failed_refs"] and "99999" in integrity["failed_refs"][0]
    # the stage's own section was clean before the corruption
    assert result["export"]["ref_integrity"]["failures"] == 0


# -- required case 5: warning baseline diff -------------------------------------


def test_warning_baseline_diff():
    baseline = json.loads(BASELINE_PATH.read_text())
    capped = "Event log exceeds the read limit; this view is incomplete."
    assert qual.diff_warnings([capped], []) == ([], [capped])
    matched, new = qual.diff_warnings(baseline[:3] + ["Something real broke."],
                                      baseline)
    assert matched == baseline[:3]
    assert new == ["Something real broke."]
    assert qual.diff_warnings(baseline, baseline) == (baseline, [])


def test_baseline_matches_source_cap_constants_and_excludes_real_warnings():
    from civ_arena import dashboard_compare as dc

    baseline = json.loads(BASELINE_PATH.read_text())
    source = {dc.WORLD_RECORDS_CAPPED: "WORLD_RECORDS_CAPPED",
              dc.WORLD_PLAYERS_CAPPED: "WORLD_PLAYERS_CAPPED",
              dc.RESEARCH_CAPPED: "RESEARCH_CAPPED",
              dc.HISTORY_CAPPED: "HISTORY_CAPPED",
              dc.OPTIONS_CAPPED: "OPTIONS_CAPPED",
              dc.ROWS_CAPPED: "ROWS_CAPPED",
              dc.MARKS_CAPPED: "MARKS_CAPPED",
              dc.ECONOMY_CAPPED: "ECONOMY_CAPPED",
              dc.ROLES_CAPPED: "ROLES_CAPPED",
              dc.DIRECTIVE_CAPPED: "DIRECTIVE_CAPPED"}
    for value, name in source.items():
        assert value in baseline, f"baseline drifted from dashboard_compare.{name}"
    for dashboard_only in ("Event log exceeds the read limit; this view is incomplete.",
                           "Event count limit reached; this view is incomplete.",
                           "Per-turn display limit reached; older items omitted.",
                           "Turn display limit reached; older turns omitted.",
                           # coordinator ruling 2026-09-09 on lane-A finding 3:
                           # dashboard.py:622's comparison-rows cap string is the
                           # same benign size-cap family as the turns variant
                           "Response size limit reached; older turns omitted.",
                           "Response size limit reached; older comparison rows omitted."):
        assert dashboard_only in baseline
    for real_problem in (dc.WORLD_INVALID,
                         "Partial trailing JSON ignored while log is written.",
                         dc.PACKET_UNBOUND, dc.STRATEGY_MALFORMED):
        assert real_problem not in baseline
    assert len(baseline) == len(set(baseline)) == 16


# -- CLI surface ----------------------------------------------------------------


def test_help_documents_exit_codes(capsys):
    with pytest.raises(SystemExit) as excinfo:
        qual.main(["--help"])
    assert excinfo.value.code == 0
    text = capsys.readouterr().out
    assert "Exit codes: 0 verdict PASS" in text
    assert "1 any stage failure" in text
    assert "2 usage/IO error" in text


def test_missing_run_dir_and_bad_rounds_are_usage_errors(tmp_path, capsys):
    assert qual.main([str(tmp_path / "nope"), "--rounds", "1"]) == 2
    assert "run dir does not exist" in capsys.readouterr().err
    assert qual.main([str(_write_qual_run(tmp_path, 1)), "--rounds", "0"]) == 2
    assert "--rounds must be >= 1" in capsys.readouterr().err


def test_malformed_baseline_file_is_a_usage_error(tmp_path, capsys):
    run_dir = _write_qual_run(tmp_path, 1)
    bad = tmp_path / "baseline.json"
    bad.write_text('{"not": "an array"}')
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake",
                              "--baseline-warnings", str(bad))
    assert code == 2 and result == {}
    assert "JSON array of strings" in capsys.readouterr().err


def test_outputs_inside_the_run_dir_are_refused(tmp_path, capsys):
    run_dir = _write_qual_run(tmp_path, 1)
    code, _, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake",
                         "--json", str(run_dir / "qualification.json"))
    assert code == 2
    assert "never writes into the run" in capsys.readouterr().err


def test_default_outputs_land_in_a_sibling_qual_directory(tmp_path):
    run_dir = _write_qual_run(tmp_path, 1)
    before = sorted(path.name for path in run_dir.iterdir())
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = qual.main([str(run_dir), "--rounds", "1", "--allow-fake"])
    assert code == 0
    default_dir = tmp_path / "qual-run.qual"
    for name in ("samples.jsonl", "samples.jsonl.manifest.json",
                 "qualification.json", "qualification-report.md"):
        assert (default_dir / name).exists(), name
    result = json.loads((default_dir / "qualification.json").read_text())
    assert result["export"]["output"] == str(default_dir / "samples.jsonl")
    assert "qual-run.qual" in stdout.getvalue()
    # the run dir itself is untouched
    assert sorted(path.name for path in run_dir.iterdir()) == before == \
        ["events.jsonl", "summary.json"]


def test_coverage_matrix_is_recorded_not_imported(tmp_path):
    run_dir = _write_qual_run(tmp_path, 1)
    matrix = tmp_path / "matrix.tsv"
    matrix.write_text("sample\tcovered\na\t1\n\nb\t0\n")
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake",
                              "--coverage", str(matrix))
    assert code == 0
    assert result["coverage"] == {"matrix_path": str(matrix),
                                  "matrix_sha256":
                                  hashlib.sha256(matrix.read_bytes()).hexdigest(),
                                  "matrix_rows": 3}


def test_dirty_tree_identity_fails(tmp_path):
    run_dir = _write_qual_run(tmp_path, 1)
    summary = json.loads((run_dir / "summary.json").read_text())
    summary["identity"]["dirty"] = True
    rows = _read_rows(run_dir)
    for row in rows:
        if row.get("audit") == "run_identity":
            row["identity"] = summary["identity"]
        if row["kind"] == "MATCH_END":
            row["summary"] = summary
    (run_dir / "summary.json").write_text(json.dumps(summary, sort_keys=True))
    _rewrite(run_dir, rows)
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake")
    assert code == 1
    assert "run recorded on a dirty tree" in result["failures"]
