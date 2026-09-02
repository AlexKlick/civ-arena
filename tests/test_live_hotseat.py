"""M18: the hotseat 1v1 rehearsed through the live dispatch surface.

Both seats arena-driven over the FakeTunerServer mini-engine: the engine
AI is out of the game entirely (the class of engine-AI hangs that ends
long games dies with it). The rehearsal proves the ALTERNATION — each
round drives every seat once, in seat order, through the real
PlayerSession/Referee/tool surface — which is the readiness gate for the
first live hotseat dispatch.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "configs" / "live-hotseat-001.yaml"


def test_hotseat_dispatch_rehearsal_alternates_seats(tmp_path):
    runs_root = tmp_path / "runs"
    cmd = [sys.executable, "-m", "civ_arena.game.civ6.live_driver",
           str(CONFIG), "--phase", "dispatch-hotseat", "--fake",
           "--turns", "2", "--runs-root", str(runs_root)]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                          timeout=300.0)
    assert proc.returncode == 0, proc.stderr + proc.stdout

    summary = json.loads(
        (runs_root / "live-hotseat-001" / "summary.json").read_text())
    assert summary["phase"] == "dispatch-hotseat"
    assert summary["clean"] is True
    assert summary["violations_total"] == 0
    rows = summary["per_turn"]
    # ALTERNATION: each round drives every seat once, in seat order
    assert [(r["turn"], r["player"]) for r in rows] == [
        (1, 0), (1, 1), (2, 0), (2, 1)]
    agents = {r["player"]: r["agent"] for r in rows}
    assert agents[0] == "hotseat-planner"
    assert agents[1] == "hotseat-turtler"
    # both seats actually played (commanded effects landed for each)
    assert sum(r["allowed_mutations"] for r in rows if r["player"] == 0) > 0
    assert sum(r["allowed_mutations"] for r in rows if r["player"] == 1) > 0

    records = [json.loads(line) for line in
               (runs_root / "live-hotseat-001" / "events.jsonl")
               .read_text().splitlines()]
    # every tool call has its result pair (the log's replay contract)
    results = [r for r in records if r["kind"] == "TOOL_RESULT"]
    assert len(results) == len([r for r in records
                                if r["kind"] == "TOOL_CALL"])
    # both seats' calls are in the log
    players_called = {r["player_id"] for r in records
                      if r["kind"] == "TOOL_CALL"}
    assert players_called == {0, 1}
    # the planner seat's belief journal landed per seat
    assert (runs_root / "live-hotseat-001" / "planner"
            / "p0-journal.jsonl").exists()
