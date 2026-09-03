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

from civ_arena.v2.ledger import verify_ledger_v2

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
    validation = verify_ledger_v2(run_dir)
    assert validation.termination_reason == "success"
    records = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
    ]
    terminal = records[-1]["payload"]["episode_receipt"]
    assert terminal["termination_reason"] == "success"
    assert terminal["turns_completed"] == 3
    assert {item["policy_kind"] for item in terminal["policies"]} == {
        "planner",
        "system",
    }

    actions: dict[str, dict] = {}
    for row in records:
        if row["event_type"] != "ActionGraphCompiled":
            continue
        digest = row["payload"]["artifact"]["digest"]
        graph = json.loads(
            (
                run_dir
                / "objects"
                / "sha256"
                / digest[:2]
                / f"{digest}.json"
            ).read_text()
        )
        actions.update(
            {node["action"]["action_id"]: node["action"] for node in graph["nodes"]}
        )
    receipts = [
        row["payload"]["turn_receipt"]
        for row in records
        if row["event_type"] == "TurnCompleted"
    ]
    results = [result for receipt in receipts for result in receipt["results"]]
    executed = [actions[result["action_id"]] for result in results]
    assert any(action["action_kind"] == "end_turn" for action in executed)
    research = [
        action for action in executed if action["action_kind"] == "set_research"
    ]
    assert research
    assert all(action["parameters"]["tech_id"] != "ANIMAL_HUSBANDRY" for action in research)
    assert all(result["status"] in {"accepted", "duplicate"} for result in results)

    # V2 policy state is content-addressed and referenced by the trust root;
    # the schema-1 side journal is deliberately no longer written.
    assert len(
        [row for row in records if row["event_type"] == "PolicyStateRecorded"]
    ) >= 3
    assert not (run_dir / "summary.json").exists()
    assert not (run_dir / "planner" / "p0-journal.jsonl").exists()
