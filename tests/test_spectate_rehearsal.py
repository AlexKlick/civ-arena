"""The spectator-capture rehearsal through the real CLI entrypoint.

Same shape as the hotseat rehearsal: `--fake` runs the true driver path
against the in-process FakeTunerServer (FakeMod's spectate timeline),
then the run must PASS validate_spectate, FAIL on a coordinated tamper,
and the replay CLI must issue the SPECTATE structural certificate.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "configs" / "live-spectator-human-001.yaml"


def test_spectator_config_parses() -> None:
    from civ_arena.config import load_config
    spec = load_config(CONFIG)
    assert spec.spectate is not None
    assert spec.spectate.operator == "alexk"
    assert spec.spectate.observed_players == (0, 1)
    assert spec.agents == []


def test_spectate_rehearsal_end_to_end(tmp_path):
    runs_root = tmp_path / "runs"
    cmd = [sys.executable, "-m", "civ_arena.game.civ6.live_driver",
           str(CONFIG), "--phase", "spectate", "--fake",
           "--turns", "3", "--runs-root", str(runs_root)]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                          timeout=300.0)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "SPECTATE CLEAN" in proc.stdout

    run_dir = runs_root / "live-spectator-human-001"
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["phase"] == "spectate"
    assert summary["clean"] is True
    assert summary["completed_rounds"] == 3
    assert summary["violations_total"] == 0
    assert summary["command_census"]["game_writes"] == 0
    assert summary["operator"] == "alexk"
    assert len(summary["per_round"]) == 3
    assert summary["attach"]["attached_mid_turn"] is True

    records = [json.loads(line) for line in
               (run_dir / "events.jsonl").read_text().splitlines()]
    assert records[0]["kind"] == "MATCH_START"
    assert records[-1]["kind"] == "MATCH_END"
    round_seq = [r["kind"] for r in records if r["kind"] in (
        "HUMAN_TURN_START", "SPECTATOR_SNAPSHOT", "HUMAN_TURN_END")]
    assert round_seq == ["HUMAN_TURN_START", "SPECTATOR_SNAPSHOT",
                         "HUMAN_TURN_END"] * 3
    assert not any(r["kind"] in ("TOOL_CALL", "TOOL_RESULT", "LEASE_GRANT",
                                 "LEASE_RELEASE", "VIOLATION",
                                 "UNAUTHORIZED_TOOL_CALL")
                   for r in records)

    # -- validate: PASS on the honest run
    from civ_arena.game.civ6.validate_run import validate
    result = validate(run_dir, 3, require_live=False)
    assert result["status"] == "PASS", result["errors"]

    # -- coordinated tamper: drop a round from BOTH summary and MATCH_END
    summary["per_round"].pop()
    summary["completed_rounds"] = 2
    records[-1]["summary"] = summary
    (run_dir / "summary.json").write_text(json.dumps(summary))
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n")
    result = validate(run_dir, 3, require_live=False)
    assert result["status"] == "FAIL"
    # summary claims 2 rounds; the events still carry 3 — the summary/
    # events round-count mismatch is caught
    assert any("round counts" in e for e in result["errors"])

    # -- replay CLI: the SPECTATE structural certificate
    replay_dir = tmp_path / "replay"
    proc = subprocess.run(
        [sys.executable, "-m", "civ_arena.replay", str(run_dir),
         "--config", str(CONFIG), "--replay-dir", str(replay_dir)],
        cwd=REPO, capture_output=True, text=True, timeout=120.0)
    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert "SPECTATE" in proc.stdout
    assert "structural certificate FAILED" in proc.stdout  # tampered above
