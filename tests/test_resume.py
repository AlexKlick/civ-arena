"""Crash-resume: SIGKILL mid-match, resume from checkpoint, identical outcome."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

CONFIG_TEMPLATE = """
match:
  match_id: {match_id}
  seed: 313131
  max_turns: {max_turns}
  checkpoint_every: 5
  adapter: simulator
  watchdog_mode: flag_and_continue
  violation_limit: 5

agents:
  - agent_id: roman
    player_id: 0
    policy: expansionist
    seed: 11
  - agent_id: korea
    player_id: 1
    policy: turtler
    seed: 22

chaos: []
"""


def write_config(tmp_path: Path, match_id: str, max_turns: int) -> Path:
    path = tmp_path / f"{match_id}.yaml"
    path.write_text(CONFIG_TEMPLATE.format(match_id=match_id, max_turns=max_turns))
    return path


def run_match(config: Path, run_root: Path, *extra: str,
              timeout: float = 180.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "civ_arena.match", str(config),
         "--run-dir", str(run_root), *extra],
        cwd=REPO, capture_output=True, text=True, timeout=timeout,
    )


def summary_of(run_root: Path, match_id: str) -> dict:
    return json.loads((run_root / match_id / "summary.json").read_text())


def test_kill9_resume_identical_state(tmp_path):
    config = write_config(tmp_path, "resume-duel", 30)
    run_root = tmp_path / "runs"

    # 1. crashing child: deterministic self-SIGKILL just after the turn-11 lease
    crashed = run_match(config, run_root, "--crash-after-turn", "10")
    assert crashed.returncode == -signal.SIGKILL, (
        f"expected SIGKILL, got {crashed.returncode}: {crashed.stderr[-500:]}"
    )
    ckpt = run_root / "resume-duel" / "checkpoints" / "ckpt-turn-0010.json"
    assert ckpt.exists(), "turn-10 checkpoint must exist before the crash point"
    events = run_root / "resume-duel" / "events.jsonl"
    n_events_after_crash = len(events.read_text().splitlines())
    assert n_events_after_crash > 200, "post-checkpoint events were written before dying"

    # 2. resume on the same run dir
    resumed = run_match(config, run_root, "--resume")
    assert resumed.returncode == 0, resumed.stderr[-800:]
    resumed_summary = summary_of(run_root, "resume-duel")

    # 3. clean uninterrupted run, same seed, separate dir
    clean = run_match(config, tmp_path / "clean")
    assert clean.returncode == 0, clean.stderr[-800:]
    clean_summary = summary_of(tmp_path / "clean", "resume-duel")

    assert resumed_summary["final_state_hash"] == clean_summary["final_state_hash"], (
        "resumed match must end in the identical state as a clean run"
    )
    assert resumed_summary["violations_total"] == 0
    assert clean_summary["violations_total"] == 0
    assert resumed_summary["final_turn"] == clean_summary["final_turn"] == 30

    # 4. log-prefix identity: the resumed log's checkpointed prefix equals the
    #    clean run's prefix (already enforced by verify_log_prefix, proven here)
    ckpt_doc = json.loads(ckpt.read_text())
    from civ_arena.canonical import log_prefix_hash

    resumed_records = [
        json.loads(line)
        for line in (run_root / "resume-duel" / "events.jsonl").read_text().splitlines()
    ]
    clean_records = [
        json.loads(line)
        for line in (tmp_path / "clean" / "resume-duel" / "events.jsonl").read_text().splitlines()
    ]
    assert log_prefix_hash(resumed_records[: ckpt_doc["seq"]]) == ckpt_doc["log_prefix_sha256"]
    assert log_prefix_hash(clean_records[: ckpt_doc["seq"]]) == ckpt_doc["log_prefix_sha256"]


def test_external_sigkill_resume(tmp_path):
    """Kill from OUTSIDE at an arbitrary point (heartbeat-polled), then resume."""
    config = write_config(tmp_path, "external-kill", 30)
    run_root = tmp_path / "runs"
    proc = subprocess.Popen(
        [sys.executable, "-m", "civ_arena.match", str(config),
         "--run-dir", str(run_root)],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    heartbeat = run_root / "external-kill" / "heartbeat.json"
    deadline = time.monotonic() + 90
    killed_at_turn = None
    while time.monotonic() < deadline:
        if heartbeat.exists():
            try:
                doc = json.loads(heartbeat.read_text())
            except json.JSONDecodeError:
                doc = {}
            if doc.get("turn", 0) >= 12:
                proc.send_signal(signal.SIGKILL)
                killed_at_turn = doc["turn"]
                break
        if proc.poll() is not None:
            raise AssertionError("child finished before we could kill it")
        time.sleep(0.05)
    assert killed_at_turn is not None, "never reached turn 12"
    proc.wait(timeout=30)
    assert proc.returncode == -signal.SIGKILL

    resumed = run_match(config, run_root, "--resume")
    assert resumed.returncode == 0, resumed.stderr[-800:]
    clean = run_match(config, tmp_path / "clean")
    assert clean.returncode == 0, clean.stderr[-800:]
    assert (summary_of(run_root, "external-kill")["final_state_hash"]
            == summary_of(tmp_path / "clean", "external-kill")["final_state_hash"])


def test_corrupt_tail_recovered(tmp_path):
    """A torn trailing line (kill -9 mid-write) survives resume."""
    config = write_config(tmp_path, "torn-tail", 30)
    run_root = tmp_path / "runs"
    crashed = run_match(config, run_root, "--crash-after-turn", "10")
    assert crashed.returncode == -signal.SIGKILL

    events = run_root / "torn-tail" / "events.jsonl"
    with open(events, "a", encoding="utf-8") as fh:
        fh.write('{"schema":1,"seq":999,"kind":"TOOL_RESU')
    with open(events, "rb") as fh:
        assert fh.seek(0, os.SEEK_END) > 0

    resumed = run_match(config, run_root, "--resume")
    assert resumed.returncode == 0, resumed.stderr[-800:]
    assert run_match(config, tmp_path / "clean").returncode == 0
    assert (summary_of(run_root, "torn-tail")["final_state_hash"]
            == summary_of(tmp_path / "clean", "torn-tail")["final_state_hash"])


@pytest.mark.parametrize("seed", [5, 6])
def test_two_subprocesses_same_seed_same_hash(tmp_path, seed):
    """Determinism across processes: identical checkpoint hashes at the end."""
    config_a = write_config(tmp_path, f"det-a-{seed}", 20)
    config_b = write_config(tmp_path, f"det-b-{seed}", 20)
    doc_a = {
        "match": {"match_id": f"det-a-{seed}", "seed": seed, "max_turns": 20,
                  "checkpoint_every": 10},
        "agents": [
            {"agent_id": "roman", "player_id": 0, "policy": "expansionist", "seed": 11},
            {"agent_id": "korea", "player_id": 1, "policy": "turtler", "seed": 22},
        ],
    }
    import yaml

    for cid, name in ((config_a, f"det-a-{seed}"), (config_b, f"det-b-{seed}")):
        doc = dict(doc_a)
        doc["match"] = dict(doc_a["match"], match_id=name)
        cid.write_text(yaml.safe_dump(doc))
    r1 = run_match(config_a, tmp_path / "ra")
    r2 = run_match(config_b, tmp_path / "rb")
    assert r1.returncode == 0 and r2.returncode == 0

    ck1 = json.loads((tmp_path / "ra" / f"det-a-{seed}" / "checkpoints"
                      / "ckpt-turn-0020.json").read_text())
    ck2 = json.loads((tmp_path / "rb" / f"det-b-{seed}" / "checkpoints"
                      / "ckpt-turn-0020.json").read_text())
    # sim + rng + coordinator must match; only instance ids may differ
    assert ck1["sim_doc"] == ck2["sim_doc"]
    assert ck1["rng_states"] == ck2["rng_states"]
    assert ck1["coordinator_state"] == ck2["coordinator_state"]
