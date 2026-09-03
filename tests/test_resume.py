"""V2 CLI custody: fresh episodes and immutable terminal-parent children."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

from civ_arena.v2.contracts import EpisodeReceiptV2
from civ_arena.v2.ledger import load_events_v2, verify_ledger_v2

REPO = Path(__file__).resolve().parents[1]

CONFIG_TEMPLATE = """\
schema: 2
match:
  match_id: {match_id}
  seed: 313131
  max_turns: {max_turns}
  checkpoint_every: 1
  adapter: simulator
  watchdog_mode: flag_and_continue
  violation_limit: 5
  execution_mode: dag_tx
  scored: false
  max_graph_actions: 1024
  max_replans_per_turn: 2

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


def _write_config(tmp_path: Path, match_id: str, max_turns: int) -> Path:
    path = tmp_path / f"{match_id}.yaml"
    path.write_text(
        CONFIG_TEMPLATE.format(match_id=match_id, max_turns=max_turns),
        encoding="utf-8",
    )
    return path


def _run(config: Path, run_root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "civ_arena.match",
            str(config),
            "--run-dir",
            str(run_root),
            *extra,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_cli_resume_creates_child_and_never_rewrites_parent(tmp_path: Path) -> None:
    config = _write_config(tmp_path, "resume-duel", 1)
    run_root = tmp_path / "runs"

    first = _run(config, run_root)
    assert first.returncode == 0, first.stderr[-800:]
    parent = run_root / "resume-duel"
    parent_bytes = (parent / "events.jsonl").read_bytes()
    parent_sha = hashlib.sha256(parent_bytes).hexdigest()
    verified = verify_ledger_v2(parent)
    assert not (parent / "summary.json").exists()
    assert not (parent / "checkpoints").exists()

    # Increasing the configured horizon resumes from the last completed
    # phase, but V2 writes the continuation to a new hash-bound episode.
    _write_config(tmp_path, "resume-duel", 2)
    resumed = _run(config, run_root, "--resume")
    assert resumed.returncode == 0, resumed.stderr[-800:]
    child_id = f"resume-duel-child-{verified.terminal_event_hash[:12]}"
    child = run_root / child_id
    assert child.is_dir()
    assert child_id in resumed.stdout
    assert hashlib.sha256((parent / "events.jsonl").read_bytes()).hexdigest() == parent_sha

    terminal = load_events_v2(child / "events.jsonl")[-1].payload_value
    assert isinstance(terminal, EpisodeReceiptV2)
    assert terminal.episode_id == child_id
    assert terminal.parent_episode_id == "resume-duel"
    assert terminal.parent_terminal_event_hash == verified.terminal_event_hash
    assert terminal.turns_completed == 2
    assert verify_ledger_v2(child).termination_reason == "success"

    duplicate = _run(config, run_root, "--resume")
    assert duplicate.returncode != 0
    assert (child / "events.jsonl").exists()


def test_cli_refuses_v1_config_and_retired_in_place_crash_flag(tmp_path: Path) -> None:
    run_root = tmp_path / "runs"
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text(
        "match:\n  match_id: old\n  seed: 1\nagents:\n"
        "  - {agent_id: a, player_id: 0, policy: turtler}\n",
        encoding="utf-8",
    )
    old = _run(legacy, run_root)
    assert old.returncode != 0
    assert "top-level schema: 2" in old.stderr

    v2 = _write_config(tmp_path, "no-in-place-crash", 1)
    crash = _run(v2, run_root, "--crash-after-turn", "1")
    assert crash.returncode != 0
    assert "in-place V1 checkpoint path" in crash.stderr
    assert not (run_root / "no-in-place-crash" / "events.jsonl").exists()
