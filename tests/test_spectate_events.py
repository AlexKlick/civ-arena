"""Spectate event kinds + replay's spectate short-circuit.

A spectate run has ZERO driven tool calls — replaying it through an Arena
would fabricate a synthetic sim match. Replay therefore returns a
STRUCTURAL CERTIFICATE (envelope + alternation + no-action-kinds + seq
contiguity) before ever constructing the Arena. The three new kinds
(SPECTATOR_SNAPSHOT, HUMAN_TURN_START, HUMAN_TURN_END) are skipped by the
from_log rebuilds and the comparable strip by construction; ambient rows
ride INSIDE these payloads, never as top-level AMBIENT events (replay
compares those against a sim replay — guaranteed divergence).
"""

import asyncio
import json
from pathlib import Path

import pytest

from civ_arena.arena.events import EventLog
from civ_arena.config import parse_config
from civ_arena.replay import _main_async, replay_run


def asyncio_run(coro):
    return asyncio.run(coro)

SPECTATE_CONFIG = {
    "match": {
        "match_id": "spectate-events-test", "seed": 1,
        "adapter": "firetuner", "watchdog_mode": "flag_and_continue",
    },
    "spectate": {"operator": "alexk"},
    "agents": [],
}


def _spectate_spec():
    return parse_config(SPECTATE_CONFIG)


def _write_run(run_dir: Path, rounds: int = 2, *, seqs=None, inject=None,
               drop=None) -> None:
    """Hand-build a minimal spectate run dir (events + summary)."""
    rows = [("MATCH_START", {})]
    for turn in range(1, rounds + 1):
        rows.append(("HUMAN_TURN_START", {"turn": turn, "operator": "alexk"}))
        rows.append(("SPECTATOR_SNAPSHOT", {
            "round": turn, "turn": turn, "phase": "turn_start",
            "digest": {"before": "aaa", "after": "bbb", "consistent": True},
        }))
        rows.append(("HUMAN_TURN_END", {
            "turn": turn, "operator": "alexk", "duration_s": 12.0,
            "human_ambient": [], "digest_after": "ccc", "overrun": False,
        }))
    rows.append(("MATCH_END", {}))
    if drop is not None:
        rows = [r for i, r in enumerate(rows) if i not in drop]
    if inject is not None:
        rows.insert(len(rows) - 1, inject)
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "events.jsonl", "w", encoding="utf-8") as fh:
        for i, (kind, fields) in enumerate(rows):
            seq = seqs[i] if seqs is not None else i
            rec = {"schema": 1, "seq": seq, "kind": kind,
                   "ts": "2026-09-07T00:00:00+00:00",
                   "match_id": "spectate-events-test",
                   "game_instance_id": "spectate-events-test-x",
                   "turn": fields.get("turn", 0), "phase_player_id": 0,
                   "player_id": None, "agent_id": None,
                   "visibility_scope": "spectator", **fields}
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    summary = {"phase": "spectate", "clean": True, "aborted": None,
               "final_turn": rounds, "completed_rounds": rounds,
               "match_id": "spectate-events-test"}
    (run_dir / "summary.json").write_text(json.dumps(summary))


# -- event kinds -----------------------------------------------------------

def test_event_log_accepts_spectate_kinds(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    for kind in ("SPECTATOR_SNAPSHOT", "HUMAN_TURN_START", "HUMAN_TURN_END"):
        log.write(kind, match_id="m", game_instance_id="g", turn=1,
                  phase_player_id=0, player_id=None, agent_id=None,
                  visibility_scope="spectator")
    assert len(log) == 3


def test_event_log_still_rejects_misspelled_kinds(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    with pytest.raises(ValueError, match="unknown event kind"):
        log.write("SPECTATOR_SNAPSHOOT", match_id="m", game_instance_id="g",
                  turn=1, phase_player_id=0, player_id=None, agent_id=None,
                  visibility_scope="spectator")


# -- replay short-circuit --------------------------------------------------

def test_replay_returns_spectate_certificate_without_arena(tmp_path: Path) -> None:
    run_dir = tmp_path / "spectate-run"
    _write_run(run_dir, rounds=3)
    replay_dir = tmp_path / "spectate-run-replay"
    result = asyncio_run(replay_run(run_dir, _spectate_spec(), replay_dir))
    assert result["mode"] == "spectate"
    assert result["identical"] is True
    assert result["problems"] == []
    # the short-circuit fires BEFORE Arena construction: no replay dir
    # is ever created
    assert not replay_dir.exists()


def test_replay_spectate_detects_broken_seq(tmp_path: Path) -> None:
    run_dir = tmp_path / "spectate-run"
    _write_run(run_dir, rounds=2, seqs=[0, 1, 2, 4, 5, 6, 7, 8])
    result = asyncio_run(replay_run(run_dir, _spectate_spec(),
                                    tmp_path / "r"))
    assert result["identical"] is False
    assert any("seq" in p for p in result["problems"])


def test_replay_spectate_detects_injected_tool_call(tmp_path: Path) -> None:
    run_dir = tmp_path / "spectate-run"
    _write_run(run_dir, rounds=1, inject=("TOOL_CALL", {"tool": "move_unit"}))
    result = asyncio_run(replay_run(run_dir, _spectate_spec(),
                                    tmp_path / "r"))
    assert result["identical"] is False
    assert any("TOOL_CALL" in p for p in result["problems"])


def test_replay_spectate_detects_broken_alternation(tmp_path: Path) -> None:
    run_dir = tmp_path / "spectate-run"
    # rows: MATCH_START, START, SNAP, END, START, SNAP, END, MATCH_END —
    # drop index 6 (the final HUMAN_TURN_END) leaving START unpaired
    _write_run(run_dir, rounds=2, drop=[6])
    result = asyncio_run(replay_run(run_dir, _spectate_spec(),
                                    tmp_path / "r"))
    assert result["identical"] is False
    assert any("alternat" in p for p in result["problems"])


def test_replay_cli_spectate_certificate_exit_codes(tmp_path, capsys) -> None:
    config = tmp_path / "config.yaml"
    import yaml
    config.write_text(yaml.safe_dump(SPECTATE_CONFIG))
    run_dir = tmp_path / "spectate-run"
    _write_run(run_dir, rounds=2)
    rc = asyncio_run(_main_async([
        str(run_dir), "--config", str(config),
        "--replay-dir", str(tmp_path / "replay")]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "SPECTATE" in out

    _write_run(run_dir, rounds=2, inject=("VIOLATION", {"watchdog": {}}))
    rc = asyncio_run(_main_async([
        str(run_dir), "--config", str(config),
        "--replay-dir", str(tmp_path / "replay2")]))
    out = capsys.readouterr().out
    assert rc == 4
    assert "SPECTATE" in out


# -- dashboard -------------------------------------------------------------

def test_dashboard_renders_spectate_log_without_warnings(tmp_path: Path) -> None:
    from civ_arena.dashboard import project_events
    rd = tmp_path / "spectate-run"
    _write_run(rd, rounds=2)
    events = [json.loads(line)
              for line in (rd / "events.jsonl").read_text().splitlines()]
    warnings: list[str] = []

    class _NoOpRedactor:
        @staticmethod
        def text(v):
            return v

        @staticmethod
        def clean(v):
            return v

    payload = project_events(events, warnings, _NoOpRedactor())
    assert warnings == []
    assert payload["metrics"]["spectator_snapshots"] == 2
    assert payload["metrics"]["human_turns"] == 2
