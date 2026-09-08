"""CAP-02: one validation core, truthful audit fields, no synthetic replay.

The replay certificate and the run validator must apply the SAME declared
structural profile (Exchange-2 F-05: the certificate skipped snapshot
presence and its identical/replayed_events fields implied a comparison
that never ran). A spectate-shaped log with a missing/malformed summary
must never fall through into a synthetic Arena replay. Launch identity is
frozen once; the closeout identity is recorded separately (F-06). Outcome
classes let interrupted/operator-stopped prefixes validate honestly
without fabricating completion.
"""

import json
from pathlib import Path

import pytest

from civ_arena.replay import _spectate_certificate, replay_run

SPECTATE_CONFIG = {
    "match": {"match_id": "cap02-test", "seed": 1, "adapter": "firetuner"},
    "spectate": {"operator": "alexk"},
    "agents": [],
}


def _spectate_spec():
    from civ_arena.config import parse_config
    return parse_config(SPECTATE_CONFIG)


def _write_run(run_dir: Path, rounds: int = 2, *, outcome=None,
               clean=True, failure=None, mutate=None) -> Path:
    """Hand-built spectate run (events + summary), CAP-02 shaped. The
    MATCH_END event carries the same summary dict the phase embeds (the
    validator's event/summary equality check is real, so the fixture must
    be honest about it)."""
    summary = {"phase": "spectate", "clean": clean, "aborted": failure,
               "failure_reason": failure, "final_turn": rounds,
               "completed_rounds": rounds, "requested_rounds": rounds,
               "match_id": "cap02-test", "operator": "alexk",
               "observed_players": [0, 1], "snapshot_scope": "full",
               "violations_total": 0,
               "command_census": {"game_writes": 0, "status_polls": rounds},
               "cleanup": {"status": "disconnect_only_no_game_actions_"
                                     "no_leases"},
               "identity": {"commit": "a" * 40, "tree": "b" * 40,
                            "dirty": False, "config": {},
                            "mod_sha256": "c" * 64}}
    if outcome is not None:
        summary["outcome"] = outcome
    rows = [("MATCH_START", {})]
    for turn in range(1, rounds + 1):
        rows.append(("HUMAN_TURN_START",
                     {"turn": turn, "operator": "alexk"}))
        rows.append(("SPECTATOR_SNAPSHOT", {
            "round": turn, "turn": turn, "phase": "turn_start",
            "digest": {"before": "aaa", "after": "bbb",
                       "consistent": True}}))
        rows.append(("HUMAN_TURN_END",
                     {"turn": turn, "operator": "alexk", "duration_s": 5.0,
                      "human_ambient": [], "digest_after": "ccc",
                      "overrun": False}))
    rows.append(("MATCH_END", {"summary": summary}))
    if mutate is not None:
        rows = mutate(rows) or rows
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "events.jsonl", "w", encoding="utf-8") as fh:
        for i, (kind, fields) in enumerate(rows):
            rec = {"schema": 1, "seq": i, "kind": kind,
                   "ts": "2026-09-07T00:00:00+00:00",
                   "match_id": "cap02-test",
                   "game_instance_id": "cap02-test-x",
                   "turn": fields.get("turn", 0), "phase_player_id": 0,
                   "player_id": None, "agent_id": None,
                   "visibility_scope": "spectator", **fields}
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    (run_dir / "summary.json").write_text(json.dumps(summary, sort_keys=True))
    return run_dir


# -- F-05 counterexample: snapshot deletion + renumbering ----------------------

def test_certificate_rejects_snapshot_deletion_after_sequence_renumbering(
        tmp_path):
    run_dir = _write_run(tmp_path / "run", rounds=2)

    def drop_snapshots(rows):
        kept = [(k, f) for k, f in rows if k != "SPECTATOR_SNAPSHOT"]
        return [(k, {**f, "round": i}) for i, (k, f) in enumerate(kept)]

    def renumber(rows):
        return rows  # re-write below with fresh seqs

    # rewrite with renumbered seqs (the counterexample's resequencing)
    lines = (run_dir / "events.jsonl").read_text().splitlines()
    parsed = [json.loads(row) for row in lines]
    parsed = [p for p in parsed if p["kind"] != "SPECTATOR_SNAPSHOT"]
    for i, p in enumerate(parsed):
        p["seq"] = i
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(p, sort_keys=True) for p in parsed) + "\n")
    result = _spectate_certificate(run_dir)
    assert result["audit_valid"] is False
    assert any("snapshot" in p.lower() for p in result["problems"])


# -- one core, both entrypoints ------------------------------------------------

def test_both_validation_entrypoints_apply_same_declared_profile(tmp_path):
    from civ_arena.spectate_audit import structural_problems
    run_dir = _write_run(tmp_path / "run", rounds=2)

    def break_turn_ids(rows):
        out = []
        for kind, fields in rows:
            if kind == "SPECTATOR_SNAPSHOT" and fields.get("turn") == 2:
                fields = {**fields, "turn": 9}  # mismatched participant/turn id
            out.append((kind, fields))
        return out

    run_dir = _write_run(tmp_path / "run", rounds=2, mutate=break_turn_ids)
    cert = _spectate_certificate(run_dir)
    problems_core = structural_problems(
        [json.loads(row) for row in
         (run_dir / "events.jsonl").read_text().splitlines()])
    assert cert["profile"] == "structural"
    assert cert["problems"] == problems_core  # SAME core, same verdict
    assert cert["audit_valid"] is False
    assert any("turn" in p for p in cert["problems"])


def test_mismatched_turn_or_participant_ids_fail_even_with_valid_counts(
        tmp_path):
    def swap_operator(rows):
        out = []
        for kind, fields in rows:
            if kind == "HUMAN_TURN_START" and fields.get("turn") == 2:
                fields = {**fields, "operator": "someone-else"}
            out.append((kind, fields))
        return out

    _write_run(tmp_path / "run", rounds=2, mutate=swap_operator)
    result = _spectate_certificate(tmp_path / "run")
    assert result["audit_valid"] is False
    assert any("operator" in p for p in result["problems"])


# -- truthful fields -----------------------------------------------------------

def test_spectator_audit_reports_no_reexecution_or_state_comparison(tmp_path):
    run_dir = _write_run(tmp_path / "run", rounds=2)
    result = _spectate_certificate(run_dir)
    assert result["audit_valid"] is True
    assert result["reexecution_performed"] is False
    assert result["comparison_result"] == "not_performed"
    assert result["capture_complete_for_declared_scope"] is None
    # legacy keys remain, derived + documented, never silently reinterpreted
    assert result["identical"] is True
    assert "schema_note" in result and "legacy" in result["schema_note"]


# -- no synthetic fallback -----------------------------------------------------

def test_missing_summary_does_not_generate_synthetic_spectate_replay(tmp_path):
    run_dir = _write_run(tmp_path / "run", rounds=2)
    (run_dir / "summary.json").unlink()
    import asyncio
    with pytest.raises(ValueError, match="refusing synthetic replay"):
        asyncio.run(replay_run(run_dir, _spectate_spec(),
                               tmp_path / "replay"))
    # malformed summary: same refusal
    run_dir2 = _write_run(tmp_path / "run2", rounds=2)
    (run_dir2 / "summary.json").write_text("{not json")
    with pytest.raises(ValueError, match="refusing synthetic replay"):
        asyncio.run(replay_run(run_dir2, _spectate_spec(),
                               tmp_path / "replay2"))


# -- outcome classes -----------------------------------------------------------

def test_operator_stop_midturn_preserves_valid_prefix_without_fake_end(
        tmp_path):
    run_dir = _write_run(
        tmp_path / "run", rounds=2, outcome="operator_stopped",
        clean=False, failure="cancelled",
        # a trailing unpaired START: the operator quit mid-turn
        mutate=lambda rows: rows[:-1] + [
            ("HUMAN_TURN_START", {"turn": 3, "operator": "alexk"}),
            rows[-1]])
    result = _spectate_certificate(run_dir)
    assert result["audit_valid"] is True  # honest prefix, no fabricated end
    assert result["outcome"] == "operator_stopped"
    assert result["final_interval"] == "open"

    from civ_arena.game.civ6.validate_run import validate
    verdict = validate(run_dir, rounds=2, require_live=False)
    assert verdict["status"] == "PASS", verdict["errors"]
    assert verdict["outcome"] == "operator_stopped"


def test_inconsistent_snapshot_can_be_structural_but_not_decision_eligible(
        tmp_path):
    def flip_consistency(rows):
        out = []
        for kind, fields in rows:
            if kind == "SPECTATOR_SNAPSHOT" and fields.get("turn") == 1:
                fields = {**fields,
                          "digest": {"before": "aaa", "after": "bbb",
                                     "consistent": False}}
            out.append((kind, fields))
        return out

    run_dir = _write_run(tmp_path / "run", rounds=2, mutate=flip_consistency)
    cert = _spectate_certificate(run_dir)
    assert cert["audit_valid"] is True  # structurally fine
    from civ_arena.game.civ6.validate_run import validate
    verdict = validate(run_dir, rounds=2, require_live=False)
    assert verdict["status"] == "PASS"  # structural + outcome hold
    assert verdict["decision_eligible"] is False
    assert any("consistent=false" in e for e in verdict["eligibility_notes"])


# -- launch identity freeze (F-06) ---------------------------------------------

def test_launch_identity_survives_later_config_commit(tmp_path):
    from civ_arena.config import parse_config
    from civ_arena.game.civ6 import live_driver as ld
    from civ_arena.game.civ6.firetuner import FireTunerAdapter

    spec = parse_config(SPECTATE_CONFIG)
    driver = ld.LiveDriver(spec, FireTunerAdapter("127.0.0.1", 1),
                           tmp_path, "f06-i1")
    launch = {"commit": "A" * 40, "tree": "t", "dirty": True,
              "config": {}, "mod_sha256": "m"}
    closeout = {"commit": "B" * 40, "tree": "t2", "dirty": False,
                "config": {}, "mod_sha256": "m"}
    captured = []
    original = ld.implementation_identity

    def fake_identity(s, mod_lua):
        # first call = launch capture; later calls = closeout recomputes
        if not captured:
            captured.append(True)
            return dict(launch)
        return dict(closeout)

    ld.implementation_identity = fake_identity
    try:
        import asyncio
        assert driver.capture_launch_identity("lua") == launch
        assert driver.capture_launch_identity("lua") == launch  # frozen once
        asyncio.run(driver.match_end(2, {
            "phase": "spectate", "identity": dict(closeout),
            "clean": True, "outcome": "completed"}))
    finally:
        ld.implementation_identity = original
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["launch_identity"] == launch          # frozen truth
    assert summary["closeout_identity"] == closeout      # separate record
    assert summary["identity"] == launch                 # legacy alias
    # a later commit (closeout B) never rewrites what launched (A)
    assert summary["launch_identity"]["commit"] == "A" * 40
    assert (tmp_path / "manifest.json").exists()  # closeout trust root


def test_payload_change_detected_against_retained_manifest(tmp_path):
    from civ_arena.spectate_audit import build_manifest, check_manifest

    run_dir = _write_run(tmp_path / "run", rounds=2)
    raw = (run_dir / "events.jsonl").read_bytes()
    records = [json.loads(row) for row in
               raw.decode().splitlines() if row.strip()]
    (run_dir / "manifest.json").write_text(
        json.dumps(build_manifest(records, raw), sort_keys=True))
    assert check_manifest(run_dir) == []
    # same-length payload change: seq numbers untouched, digest catches it
    text = raw.decode()
    needle = '"operator": "alexk"'
    assert needle in text
    text = text.replace(needle, '"operator": "a1exk"', 1)
    assert len(text.encode()) == len(raw)  # length preserved
    (run_dir / "events.jsonl").write_text(text)
    problems = check_manifest(run_dir)
    assert any("sha256" in p for p in problems)


def test_final_ai_interval_is_closed_or_marked_unobserved_on_stop(tmp_path):
    from civ_arena.game.civ6.validate_run import validate
    # a completed run closes its final interval
    run_dir = _write_run(tmp_path / "done", rounds=2)
    verdict = validate(run_dir, rounds=2, require_live=False)
    assert verdict["final_interval"] == "closed"
    assert verdict["status"] == "PASS"
    # an interrupted run REPORTS the open interval — never papers over it
    run_dir = _write_run(
        tmp_path / "stopped", rounds=2, outcome="interrupted",
        clean=False, failure="ConnectionError: tuner gone",
        mutate=lambda rows: rows[:-1] + [
            ("HUMAN_TURN_START", {"turn": 3, "operator": "alexk"}),
            rows[-1]])
    verdict = validate(run_dir, rounds=2, require_live=False)
    assert verdict["status"] == "PASS"
    assert verdict["final_interval"] == "open"
    assert any("open" in n for n in verdict["eligibility_notes"])
    assert verdict["decision_eligible"] is False


# -- the real closed run: honest classification, no rewriting -------------------

REAL_RUN = Path("/home/alexk/civ-arena-spectator-capture-20260907/runs/"
                "live-spectator-human-001-20260907T203439Z")


def test_legacy_dirty_launch_remains_honestly_classified(tmp_path):
    import hashlib
    import shutil

    from civ_arena.game.civ6.validate_run import validate
    from civ_arena.spectate_audit import derive_provenance

    if not (REAL_RUN / "events.jsonl").exists():
        pytest.skip("real closed run not present on this host")
    copy = tmp_path / "real-run"
    copy.mkdir()
    for name in ("events.jsonl", "summary.json"):
        shutil.copy2(REAL_RUN / name, copy / name)
    before = hashlib.sha256((REAL_RUN / "events.jsonl").read_bytes()).hexdigest()

    verdict = validate(copy, rounds=65, require_live=False)
    assert verdict["outcome"] == "interrupted"
    assert verdict["status"] == "PASS", verdict["errors"]  # honest prefix
    assert verdict["final_interval"] == "open"
    assert verdict["decision_eligible"] is False
    assert any("trace_gaps" in n for n in verdict["eligibility_notes"])
    # the ring-wrap gap signature: at least one interval spans two turns
    assert any("capture-gap signature" in n
               for n in verdict["eligibility_notes"])

    prov = derive_provenance(copy)
    launch = prov["launch_identity_from_event"]
    assert launch and launch["commit"].startswith("b511346")
    assert launch["dirty"] is True                      # launch truth
    assert prov["identity_divergence"] is True          # summary says 5d5221b
    assert prov["outcome"] == "interrupted"
    # derived assessment only — the original is byte-identical
    after = hashlib.sha256((REAL_RUN / "events.jsonl").read_bytes()).hexdigest()
    assert before == after
