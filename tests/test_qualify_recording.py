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
import os
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
    """An admitted spectator world carrying every key world_capture.package
    emits unconditionally (Codex r2 finding 2: the fixture previously omitted
    `owned_tiles_columns` and `palette_confirmed`, so it could not pin the
    completeness the audit now requires)."""
    return {"schema": 1, "after_seat": after_seat,
            "contexts": {"roster": "gamecore", "tiles": "gamecore",
                         "palette": "ingame"},
            "grid": {"w": 4, "h": 4}, "game_era": "ERA_FAKE",
            "roster": [{"player_id": 0, "kind": "major"},
                       {"player_id": 1, "kind": "major"}],
            "players": [{"player_id": 0}, {"player_id": 1}], "cities": [],
            "owned_tiles_columns": {},
            "fog_audit": {"requested": 0, "engine_visible": 0,
                          "engine_not_visible": 0, "unavailable": 0,
                          "disagree_coords": []},
            "palette_confirmed": False,
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


# -- r1 fix lane: world payload / seat / pipeline contract (finding 1) ----------


def _edit_capture(run_dir: Path, after_seat: int, edit) -> None:
    """Mutate one spectator_world row on disk (seqs renumbered after)."""
    rows = _read_rows(run_dir)
    row = next(row for row in rows
               if row.get("audit") == "spectator_world"
               and row["after_seat"] == after_seat)
    edit(row)
    _rewrite(run_dir, rows)


def test_clean_fixture_rejects_no_captures(tmp_path):
    run_dir = _write_qual_run(tmp_path, 1)
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake")
    assert code == 0, result.get("failures")
    assert result["audit"]["spectator_world_rejected"] == []
    assert result["audit"]["after_seat_sequence"] == [-1, 0, 1]


def test_worldless_spectator_capture_is_rejected_and_breaks_the_row_contract(tmp_path):
    run_dir = _write_qual_run(tmp_path, 1)
    _edit_capture(run_dir, 1, lambda row: row.pop("world"))
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake")
    assert code == 1
    assert result["verdict"] == "FAIL"
    rejected = result["audit"]["spectator_world_rejected"]
    assert len(rejected) == 1 and rejected[0]["after_seat"] == 1
    assert "world payload" in rejected[0]["reason"]
    assert any("does not count toward the row contract" in failure
               for failure in result["failures"])
    # the rejected capture cannot satisfy the row contract either
    assert result["audit"]["after_seat_sequence"] == [-1, 0]
    assert result["audit"]["spectator_world_rows"] == 2
    assert result["audit"]["expected_rows"] == 3
    assert any("row contract" in failure for failure in result["failures"])


@pytest.mark.parametrize("op,token", [
    ("not_a_dict", "not a dict"),
    ("schema", "schema is not 1"),
    ("roster", "roster is not a list"),
    ("read_ms_absent", "read_ms"),
    ("read_ms_type", "read_ms"),
    ("unknown_key", "outside the world package key set"),
])
def test_corrupt_world_envelopes_are_rejected(tmp_path, op, token):
    run_dir = _write_qual_run(tmp_path, 1)

    def corrupt(row):
        if op == "not_a_dict":
            row["world"] = "garbage"
        else:
            world = row["world"]
            if op == "schema":
                world["schema"] = 2
            elif op == "roster":
                world["roster"] = {"0": "major"}
            elif op == "read_ms_absent":
                del world["read_ms"]
            elif op == "read_ms_type":
                world["read_ms"] = "fast"
            elif op == "unknown_key":
                world["unexpected_key"] = 1

    _edit_capture(run_dir, 1, corrupt)
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake")
    assert code == 1
    assert result["verdict"] == "FAIL"
    rejected = result["audit"]["spectator_world_rejected"]
    assert len(rejected) == 1, result["failures"]
    assert token in rejected[0]["reason"], rejected[0]["reason"]
    assert result["audit"]["after_seat_sequence"] == [-1, 0]
    assert any("row contract" in failure for failure in result["failures"])


def test_inner_after_seat_mismatch_is_rejected(tmp_path):
    run_dir = _write_qual_run(tmp_path, 1)
    _edit_capture(run_dir, 1,
                  lambda row: row["world"].update({"after_seat": 2}))
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake")
    assert code == 1
    reason = result["audit"]["spectator_world_rejected"][0]["reason"]
    assert "after_seat 2" in reason and "event after_seat 1" in reason
    assert result["audit"]["after_seat_sequence"] == [-1, 0]


def test_capture_turn_regression_is_rejected(tmp_path):
    run_dir = _write_qual_run(tmp_path, 1)
    _edit_capture(run_dir, 1, lambda row: row.update({"turn": 0}))
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake")
    assert code == 1
    reason = result["audit"]["spectator_world_rejected"][0]["reason"]
    assert "precedes the previous capture's turn 1" in reason


def test_capture_without_a_completed_seat_turn_is_rejected(tmp_path):
    """A spliced capture claiming seat 1 completed turn 1 — before seat 1 ever
    ran — must be rejected even though summary.per_turn names that pair.

    The three genuine captures still hold up, so the row-contract SEQUENCE is
    intact; the run fails on the rejection itself (a forged capture is never
    laundered into evidence by the surviving contract).
    """
    run_dir = _write_qual_run(tmp_path, 1)
    rows = _read_rows(run_dir)
    baseline = next(index for index, row in enumerate(rows)
                    if row.get("audit") == "spectator_world")
    rows.insert(baseline + 1,
                {"schema": 1, "kind": "HEARTBEAT", "ts": TS, "match_id": MATCH_ID,
                 "game_instance_id": INSTANCE, "turn": 1, "phase_player_id": -1,
                 "player_id": None, "agent_id": None,
                 "visibility_scope": "spectator", "audit": "spectator_world",
                 "after_seat": 1, "world": _world(1)})
    _rewrite(run_dir, rows)
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake")
    assert code == 1
    rejected = result["audit"]["spectator_world_rejected"]
    assert len(rejected) == 1 and rejected[0]["after_seat"] == 1
    assert "completed_seat_turn" in rejected[0]["reason"]
    assert "after this capture" in rejected[0]["reason"]
    assert result["audit"]["after_seat_sequence"] == [-1, 0, 1]
    assert any("does not count toward the row contract" in failure
               for failure in result["failures"])


def test_capture_placed_before_its_seat_turn_completes_is_rejected(tmp_path):
    """The baseline capture sits before every seat turn; retargeting it at seat
    0 cannot be laundered by the per-turn summary row that comes later."""
    run_dir = _write_qual_run(tmp_path, 1)
    _edit_capture(run_dir, -1, lambda row: row.update({"after_seat": 0}))
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake")
    assert code == 1
    reason = result["audit"]["spectator_world_rejected"][0]["reason"]
    assert "at seq" in reason and "after this capture" in reason
    assert result["audit"]["after_seat_sequence"] == [0, 1]


def test_novel_roster_kind_fails_the_verdict_but_still_counts(tmp_path):
    run_dir = _write_qual_run(tmp_path, 1)
    _edit_capture(run_dir, 0, lambda row: row["world"]["roster"].append(
        {"player_id": 9, "kind": "rebels"}))
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake")
    assert code == 1
    assert result["verdict"] == "FAIL"
    # the census keeps counting the unknown kind ...
    assert result["audit"]["roster_kinds_observed"] == {"major": 6, "rebels": 1}
    # ... but a failure names the kind and the pid
    assert any("'rebels'" in failure and "pid 9" in failure
               for failure in result["failures"])


# -- r1 fix lane: output safety (findings 2, 5, 6, 8) ---------------------------


@pytest.mark.parametrize("artifact,flag", [("events.jsonl", "--json"),
                                           ("summary.json", "--report")])
def test_output_hardlinked_to_run_evidence_is_refused(tmp_path, capsys,
                                                      artifact, flag):
    run_dir = _write_qual_run(tmp_path, 1)
    out = tmp_path / "qual-out"
    out.mkdir()
    linked = out / f"linked-{artifact}"
    os.link(run_dir / artifact, linked)
    before = (run_dir / artifact).read_bytes()
    code, result, json_path, report = _run(run_dir, tmp_path, "--rounds", "1",
                                           "--allow-fake", flag, str(linked))
    assert code == 2 and result == {}
    assert "aliases run evidence" in capsys.readouterr().err
    assert (run_dir / artifact).read_bytes() == before
    assert not json_path.exists() and not report.exists()


def test_colliding_output_paths_are_refused(tmp_path, capsys):
    run_dir = _write_qual_run(tmp_path, 1)
    out = tmp_path / "collide"
    out.mkdir()
    same = out / "same.out"
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake",
                              "--json", str(same), "--report", str(same))
    assert code == 2 and result == {}
    assert "same path" in capsys.readouterr().err
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake",
                              "--export-out", str(out / "s.jsonl"),
                              "--json", str(out / "s.jsonl.manifest.json"))
    assert code == 2 and result == {}
    assert "manifest sibling" in capsys.readouterr().err
    assert not (out / "s.jsonl").exists()


def test_hardlinked_output_collision_is_refused(tmp_path, capsys):
    run_dir = _write_qual_run(tmp_path, 1)
    out = tmp_path / "collide2"
    out.mkdir()
    first, second = out / "a.md", out / "b.md"
    first.write_text("keep me\n")
    os.link(first, second)
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake",
                              "--json", str(first), "--report", str(second))
    assert code == 2 and result == {}
    assert "same file" in capsys.readouterr().err
    assert first.read_text() == "keep me\n"


def test_unreadable_events_jsonl_is_exit_2_without_a_traceback(tmp_path, capsys):
    run_dir = _write_qual_run(tmp_path, 1)
    (run_dir / "events.jsonl").write_text('{"schema": 1, "seq": 0,\n{"broken"\n')
    out = tmp_path / "qual-out"
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake")
    assert code == 2 and result == {}
    err = capsys.readouterr().err
    assert "not valid JSON" in err and "Traceback" not in err
    assert not (out / "qualification.json").exists()
    assert not (out / "report.md").exists()
    assert not (out / "samples.jsonl").exists()


def test_an_unpublishable_destination_publishes_nothing(tmp_path, capsys):
    """A destination that cannot receive its member is refused BEFORE any
    member is published (r2 findings 3+4 replaced the round-2 write-then-
    unlink model with invocation-owned staging), so the tree is untouched."""
    run_dir = _write_qual_run(tmp_path, 1)
    out = tmp_path / "boom"
    (out / "report.md").mkdir(parents=True)  # a DIRECTORY where the report goes
    code, result, json_path, _ = _run(run_dir, tmp_path, "--rounds", "1",
                                      "--allow-fake", "--export-out",
                                      str(out / "samples.jsonl"),
                                      "--report", str(out / "report.md"))
    assert code == 2 and result == {}
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "existing directory" in err and "nothing was published" in err
    assert not json_path.exists()
    assert not (out / "samples.jsonl").exists()
    assert not (out / "samples.jsonl.manifest.json").exists()


def test_report_cells_escape_pipes(tmp_path):
    """The viewer refuses a piped run id, but the harness still PUBLISHES that
    run's report — the document the operator reads to see why it failed. A raw
    `|` in the Run id cell shifts every column to its right."""
    run_dir = _write_qual_run(tmp_path, 1, name="qual|run")
    code, result, _, report = _run(run_dir, tmp_path, "--rounds", "1",
                                   "--allow-fake")
    assert code == 1
    assert any("viewer load failed" in failure
               for failure in result["failures"]), result["failures"]
    lines = report.read_text().splitlines()
    assert "| Run id | `qual\\|run` |" in lines
    table = [line for line in lines[lines.index("| Field | Value |")::]
             if line.startswith("|")]
    widths = {len(line.replace("\\|", "").split("|")) for line in table}
    assert widths == {4}, widths  # header, separator and every row: 2 cells


# -- r1 fix lane: the warning baseline is code-owned (finding 4) ----------------


def test_supplied_baseline_cannot_whitelist_real_warnings(tmp_path, capsys):
    from civ_arena import dashboard_compare as dc

    run_dir = _write_qual_run(tmp_path, 1)
    baseline = json.loads(BASELINE_PATH.read_text())
    bad = tmp_path / "baseline.json"
    bad.write_text(json.dumps(baseline[:2] + [dc.WORLD_INVALID,
                                              dc.PACKET_UNBOUND]))
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake",
                              "--baseline-warnings", str(bad))
    assert code == 2 and result == {}
    err = capsys.readouterr().err
    assert "outside the code-owned benign-cap allowlist" in err
    assert dc.WORLD_INVALID in err and dc.PACKET_UNBOUND in err


# -- r2 fix lane: capture binding (finding 1) ----------------------------------


def test_later_round_captures_cannot_reuse_earlier_completed_turns(tmp_path):
    """Codex r2 finding 1: retarget both round-2 captures onto turn 1. Each
    still finds AN earlier completed_seat_turn row and the turns stay
    non-decreasing, so an existence test PASSes a run that never captured
    turn 2. A completed seat turn backs exactly one capture, bound in order."""
    run_dir = _write_qual_run(tmp_path, 2)
    rows = _read_rows(run_dir)
    retargeted = 0
    for row in rows:
        if row.get("audit") == "spectator_world" and row["turn"] == 2:
            row["turn"] = 1
            retargeted += 1
    assert retargeted == 2, "fixture shape changed"
    _rewrite(run_dir, rows)
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "2", "--allow-fake")
    assert code == 1
    rejected = result["audit"]["spectator_world_rejected"]
    assert [entry["after_seat"] for entry in rejected] == [0, 1]
    assert all("completed_seat_turn" in entry["reason"] for entry in rejected)
    assert all("turn 2" in entry["reason"] for entry in rejected)
    # the two round-2 captures no longer count, so the contract fails too
    assert result["audit"]["after_seat_sequence"] == [-1, 0, 1]
    assert any("row contract" in failure for failure in result["failures"])


def test_a_completed_seat_turn_backs_exactly_one_capture(tmp_path):
    """A second capture inside the same completed-turn interval cannot bind
    to a row an earlier capture already consumed."""
    run_dir = _write_qual_run(tmp_path, 1)
    rows = _read_rows(run_dir)
    index = next(i for i, row in enumerate(rows)
                 if row.get("audit") == "spectator_world" and row["after_seat"] == 0)
    rows.insert(index + 1, json.loads(json.dumps(rows[index])))
    _rewrite(run_dir, rows)
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake")
    assert code == 1
    rejected = result["audit"]["spectator_world_rejected"]
    assert len(rejected) == 1 and rejected[0]["after_seat"] == 0
    assert "already bound" in rejected[0]["reason"]
    assert result["audit"]["after_seat_sequence"] == [-1, 0, 1]


# -- r2 fix lane: viewer-independent world validation (finding 2) --------------


def test_referee_scoped_capture_cannot_satisfy_the_row_contract(tmp_path):
    """Codex r2 finding 2: dashboard_compare.spectator_world_records() skips
    non-spectator events, so a referee-routed capture is never viewer-checked.
    The audit must not count a world the viewer would not even inspect."""
    run_dir = _write_qual_run(tmp_path, 1)
    _edit_capture(run_dir, 1,
                  lambda row: row.update({"visibility_scope": "referee"}))
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake")
    assert code == 1
    rejected = result["audit"]["spectator_world_rejected"]
    assert len(rejected) == 1 and rejected[0]["after_seat"] == 1
    assert "visibility_scope" in rejected[0]["reason"]
    assert "'referee'" in rejected[0]["reason"]
    assert result["audit"]["after_seat_sequence"] == [-1, 0]


def test_codex_r2_near_empty_referee_world_is_rejected(tmp_path):
    """The exact r2 counterexample: a four-key world routed to the referee."""
    run_dir = _write_qual_run(tmp_path, 1)

    def gut(row):
        row["visibility_scope"] = "referee"
        row["world"] = {"schema": 1, "after_seat": 1, "roster": [], "read_ms": 0}

    _edit_capture(run_dir, 1, gut)
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake")
    assert code == 1
    rejected = result["audit"]["spectator_world_rejected"]
    assert len(rejected) == 1 and rejected[0]["after_seat"] == 1
    reason = rejected[0]["reason"]
    assert "visibility_scope" in reason
    for missing in ("contexts", "grid", "players", "cities", "fog_audit",
                    "palette_confirmed", "truncated", "owned_tiles_columns"):
        assert missing in reason, missing
    assert result["audit"]["after_seat_sequence"] == [-1, 0]
    assert any("row contract" in failure for failure in result["failures"])


@pytest.mark.parametrize("dropped", ["contexts", "grid", "players", "cities",
                                     "owned_tiles_columns", "fog_audit",
                                     "palette_confirmed", "truncated"])
def test_every_unconditional_package_key_is_required(tmp_path, dropped):
    run_dir = _write_qual_run(tmp_path, 1)
    _edit_capture(run_dir, 1, lambda row: row["world"].pop(dropped))
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake")
    assert code == 1
    rejected = result["audit"]["spectator_world_rejected"]
    assert len(rejected) == 1, result["failures"]
    assert dropped in rejected[0]["reason"]
    assert result["audit"]["after_seat_sequence"] == [-1, 0]


def test_owned_tiles_columns_may_be_absent_only_when_the_world_truncated(tmp_path):
    """world_capture._bounded_json drops the territory block under the byte
    cap and RECORDS it — absent-with-truncation is admissible, absent without
    it is not (pinned by the parametrized test above)."""
    run_dir = _write_qual_run(tmp_path, 1)

    def cap(row):
        row["world"].pop("owned_tiles_columns")
        row["world"]["truncated"] = {"tiles": False, "world": True}

    _edit_capture(run_dir, 1, cap)
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake")
    assert code == 0, result.get("failures")
    assert result["audit"]["spectator_world_rejected"] == []
    assert result["audit"]["after_seat_sequence"] == [-1, 0, 1]


def test_non_heartbeat_event_kind_is_rejected():
    """The producer writes the capture as a HEARTBEAT (live_driver.py:831)."""
    record = {"kind": "MATCH_END", "seq": 9, "turn": 1, "after_seat": -1,
              "visibility_scope": "spectator", "world": _world(-1)}
    reasons = qual.world_envelope_reasons(record, None)
    assert any("kind" in reason and "'MATCH_END'" in reason for reason in reasons)


# -- r2 fix lane: malformed roster rows (finding 5) ----------------------------


def test_malformed_roster_pid_is_recorded_not_a_crash(tmp_path, capsys):
    """Codex r2 finding 5: an unhashable player_id used to raise TypeError out
    of the census, past the QualificationError-only boundary, as a traceback."""
    run_dir = _write_qual_run(tmp_path, 0 + 1)
    _edit_capture(run_dir, 0, lambda row: row["world"]["roster"].append(
        {"player_id": [], "kind": "rebels"}))
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake")
    assert code == 1
    assert "Traceback" not in capsys.readouterr().err
    rejected = result["audit"]["spectator_world_rejected"]
    assert len(rejected) == 1 and rejected[0]["after_seat"] == 0
    assert "player_id" in rejected[0]["reason"]
    # the census still names the novel kind without crashing on the pid
    assert any("'rebels'" in failure for failure in result["failures"])
    assert result["audit"]["roster_kinds_observed"]["rebels"] == 1
    assert result["audit"]["after_seat_sequence"] == [-1, 1]


def test_unexpected_stage_error_is_exit_2_without_a_traceback(tmp_path, capsys,
                                                              monkeypatch):
    """Artifact-parsing exceptions normalize at the stage boundary."""
    run_dir = _write_qual_run(tmp_path, 1)

    def boom(*_args, **_kwargs):
        raise TypeError("unhashable type: 'list'")

    monkeypatch.setattr(qual, "stage_viewer", boom)
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake")
    assert code == 2 and result == {}
    err = capsys.readouterr().err
    assert "TypeError" in err and "Traceback" not in err


# -- r2 fix lane: publication ownership (findings 3 + 4) -----------------------


def test_failed_publication_preserves_a_pre_existing_verdict_document(tmp_path,
                                                                      capsys):
    """Codex r2 finding 3: a failed publication must leave NO fresh PASS
    document — least of all on top of a previous generation it destroyed."""
    run_dir = _write_qual_run(tmp_path, 1)
    out = tmp_path / "pub"
    (out / "report.md").mkdir(parents=True)  # a DIRECTORY where the report goes
    sentinel = '{"verdict": "FAIL", "generation": "previous"}\n'
    (out / "qualification.json").write_text(sentinel)
    code, _, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake",
                         "--json", str(out / "qualification.json"),
                         "--report", str(out / "report.md"),
                         "--export-out", str(out / "samples.jsonl"))
    assert code == 2
    assert "Traceback" not in capsys.readouterr().err
    # the previous generation survives byte-for-byte ...
    assert (out / "qualification.json").read_text() == sentinel
    # ... and not one member of the abandoned publication reached the tree
    assert not (out / "samples.jsonl").exists()
    assert not (out / "samples.jsonl.manifest.json").exists()


def test_cleanup_never_deletes_another_invocations_artifacts(tmp_path,
                                                             monkeypatch):
    """Codex r2 finding 4: 'absent at start' is not ownership. A snapshots four
    absent paths, B publishes to them, then A fails having created nothing —
    A must not delete B's artifacts."""
    shared = tmp_path / "shared"
    shared.mkdir()
    run_b = _write_qual_run(tmp_path, 1, name="run-b")
    run_a = _write_qual_run(tmp_path, 1, name="run-a")
    argv = ["--json", str(shared / "qualification.json"),
            "--report", str(shared / "report.md"),
            "--export-out", str(shared / "samples.jsonl")]
    real_audit, state, published = qual.stage_audit, {"done": False}, {}

    def audit_then_publish_b(*args, **kwargs):
        if state["done"]:
            return real_audit(*args, **kwargs)
        state["done"] = True
        # invocation B publishes into the paths A has already snapshotted
        with contextlib.redirect_stdout(io.StringIO()):
            assert qual.main([str(run_b), "--rounds", "1", "--allow-fake",
                              *argv]) == 0
        published.update({path.name: path.read_bytes()
                          for path in sorted(shared.iterdir())})
        raise qual.QualificationError("events.jsonl line 1 is not valid JSON: x")

    monkeypatch.setattr(qual, "stage_audit", audit_then_publish_b)
    with contextlib.redirect_stdout(io.StringIO()):
        code = qual.main([str(run_a), "--rounds", "1", "--allow-fake", *argv])
    assert code == 2
    assert set(published) == {"qualification.json", "report.md", "samples.jsonl",
                              "samples.jsonl.manifest.json"}, published
    assert {path.name: path.read_bytes()
            for path in sorted(shared.iterdir())} == published


def test_no_artifact_is_published_until_every_one_is_staged(tmp_path):
    """The verdict document is written LAST, after every other member."""
    run_dir = _write_qual_run(tmp_path, 1)
    out = tmp_path / "order"
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake",
                              "--json", str(out / "qualification.json"),
                              "--report", str(out / "report.md"),
                              "--export-out", str(out / "samples.jsonl"))
    assert code == 0, result.get("failures")
    members = ["samples.jsonl", "samples.jsonl.manifest.json", "report.md",
               "qualification.json"]
    assert sorted(path.name for path in out.iterdir()) == sorted(members)
    mtimes = [(out / name).stat().st_mtime_ns for name in members]
    assert mtimes == sorted(mtimes), dict(zip(members, mtimes, strict=True))
    # nothing of the staging area survives next to the published artifacts
    assert not any(path.name.endswith(".tmp") for path in out.iterdir())


# -- r2 fix lane: the coverage input is a protected input (finding 6) ----------


def test_coverage_input_aliased_by_an_output_is_refused(tmp_path, capsys):
    """Codex r2 finding 6: coverage_section() hashes the matrix, then
    publication overwrote it — the PASS document's matrix_sha256 no longer
    described matrix_path."""
    run_dir = _write_qual_run(tmp_path, 1)
    matrix = tmp_path / "matrix.tsv"
    matrix.write_text("sample\tcovered\na\t1\n")
    before = matrix.read_bytes()
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake",
                              "--coverage", str(matrix), "--json", str(matrix))
    assert code == 2 and result == {}
    assert "aliases the --coverage input" in capsys.readouterr().err
    assert matrix.read_bytes() == before


def test_coverage_input_hardlinked_by_an_output_is_refused(tmp_path, capsys):
    run_dir = _write_qual_run(tmp_path, 1)
    matrix = tmp_path / "matrix.tsv"
    matrix.write_text("sample\tcovered\na\t1\n")
    linked = tmp_path / "linked-matrix.tsv"
    os.link(matrix, linked)
    before = matrix.read_bytes()
    code, result, _, _ = _run(run_dir, tmp_path, "--rounds", "1", "--allow-fake",
                              "--coverage", str(matrix), "--report", str(linked))
    assert code == 2 and result == {}
    assert "aliases the --coverage input" in capsys.readouterr().err
    assert matrix.read_bytes() == before


def test_baseline_allowlist_is_code_owned_and_matches_the_shipped_json():
    from civ_arena import dashboard_compare as dc

    allowed = set(qual.ALLOWED_BASELINE_WARNINGS)
    assert allowed == set(json.loads(BASELINE_PATH.read_text()))
    dashboard_source = (REPO / "src" / "civ_arena" / "dashboard.py").read_text()
    for warning in qual.DASHBOARD_CAP_WARNINGS:
        assert warning in dashboard_source, warning
    assert len(qual.DASHBOARD_CAP_WARNINGS) == 6
    assert len(allowed) == 16
    for real_problem in (dc.WORLD_INVALID, dc.PACKET_UNBOUND,
                         dc.STRATEGY_MALFORMED, "WORLD_INVALID"):
        assert real_problem not in allowed
