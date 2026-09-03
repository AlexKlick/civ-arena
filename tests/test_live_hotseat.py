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

from civ_arena.v2.ledger import verify_ledger_v2

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

    run_dir = runs_root / "live-hotseat-001"
    validation = verify_ledger_v2(run_dir)
    assert validation.termination_reason == "success"
    records = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
    ]
    terminal = records[-1]["payload"]["episode_receipt"]
    assert terminal["termination_reason"] == "success"
    assert terminal["turns_completed"] == 4
    receipts = [
        row["payload"]["turn_receipt"]
        for row in records
        if row["event_type"] == "TurnCompleted"
        and row["payload"]["turn_receipt"]["termination"] == "completed"
    ]
    # ALTERNATION: each round drives every seat once, in seat order.
    assert [(r["turn"], r["player_id"]) for r in receipts] == [
        (1, 0), (1, 1), (2, 0), (2, 1)]
    assert all(receipt["results"] for receipt in receipts)
    assert all(
        result["status"] in {"accepted", "duplicate"}
        for receipt in receipts
        for result in receipt["results"]
    )
    assert {item["policy_kind"] for item in terminal["policies"]} == {
        "planner",
        "scripted",
        "system",
    }
    state_events = [
        row for row in records if row["event_type"] == "PolicyStateRecorded"
    ]
    assert len(state_events) >= 4  # mandatory-decision replans add durable states
    assert {row["correlation_id"] for row in state_events} == {
        "turn-1-p0",
        "turn-1-p1",
        "turn-2-p0",
        "turn-2-p1",
    }
    assert not (run_dir / "summary.json").exists()
    assert not (run_dir / "planner" / "p0-journal.jsonl").exists()
