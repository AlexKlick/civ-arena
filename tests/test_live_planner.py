"""M17a: the CivGraph planner rehearsed through the LIVE dispatch phase.

The M14d discipline applied to the planner policy: over the FakeTunerServer
mini-engine, through the real PlayerSession/Referee/tool surface, the
planner must complete its observe -> believe -> compile -> execute cycle
using exactly the tools the live wire implements — and its belief journal
lands in the run dir like the Arena wires it. This is the readiness gate
for the first real-engine planner dispatch (M17c).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "configs" / "live-planner-001.yaml"


def test_planner_dispatch_rehearsal_end_to_end(tmp_path):
    runs_root = tmp_path / "runs"
    cmd = [sys.executable, "-m", "civ_arena.game.civ6.live_driver",
           str(CONFIG), "--phase", "dispatch", "--fake", "--turns", "3",
           "--runs-root", str(runs_root)]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                          timeout=240.0)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    run_dir = runs_root / "live-planner-001"
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["phase"] == "dispatch"
    assert summary["clean"] is True
    assert summary["violations_total"] == 0
    assert len(summary["per_turn"]) == 3
    # the planner actually played: commanded effects landed
    assert sum(t["allowed_mutations"] for t in summary["per_turn"]) > 0

    records = [json.loads(line) for line in
               (run_dir / "events.jsonl").read_text().splitlines()]
    tools = [r["tool"] for r in records if r["kind"] == "TOOL_CALL"]
    # the planner's observe set rides the live surface verbatim
    for expected in ("get_overview", "get_units", "get_cities",
                     "get_visible_map", "get_available_research", "end_turn"):
        assert expected in tools, f"{expected} missing: {sorted(set(tools))}"
    # the sim-to-real vocabulary seam WORKED: research was substituted to
    # an offered id and ACCEPTED (the sim-table pick, ANIMAL_HUSBANDRY, is
    # not in the wire's 4-tech vocabulary)
    assert "set_research" in tools
    assert "ANIMAL_HUSBANDRY" not in [
        r["args"].get("tech_id") for r in records
        if r["kind"] == "TOOL_CALL" and r["tool"] == "set_research"]
    assert not [r for r in records
                if r["kind"] == "TOOL_RESULT" and r.get("status") == "rejected"]

    # the belief journal landed where the Arena puts it, and replay pairing
    journal = run_dir / "planner" / "p0-journal.jsonl"
    assert journal.exists()
    entries = [json.loads(x) for x in journal.read_text().splitlines() if x.strip()]
    assert [e["turn"] for e in entries] == sorted(e["turn"] for e in entries)
    assert all("belief" in e["doc"] and "active" in e["doc"] for e in entries)
    results = [r for r in records if r["kind"] == "TOOL_RESULT"]
    assert len(results) == len([r for r in records
                                if r["kind"] == "TOOL_CALL"])
