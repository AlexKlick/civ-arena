"""Research game runner: one planned game → Arena run with trajectory capture.

Mirrors scripts/league.py's spend discipline: refuse a non-empty run dir
BEFORE any spend, write the effective config next to the artifacts for
replay, integers-first result rows. Per-turn score trajectories ride the
coordinator's keyword-only ``on_turn_end`` observation hook — the event log
and state hashes are untouched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from civ_arena.arena.coordinator import Arena
from civ_arena.config import parse_config
from civ_arena.game.sim.value import scalarize
from civ_arena.research.matrix import GamePlan


def config_doc(plan: GamePlan) -> dict[str, Any]:
    """The match config dict for one planned game (parse_config-validated)."""
    agents = []
    for player_id, spec in plan.seats:
        agent: dict[str, Any] = {
            "agent_id": f"{spec.label}-{player_id}",
            "player_id": player_id,
            "policy": spec.policy,
            # config default (seed*10 + i) would double-offset by index; pin
            # the same shape explicitly so rows can cite agent seeds
            "seed": plan.seed * 10 + player_id,
        }
        if spec.adaptive is not None:
            agent["adaptive"] = spec.adaptive
        agents.append(agent)
    return {
        "match": {
            "match_id": plan.match_id,
            "seed": plan.seed,
            "max_turns": plan.max_turns,
            "adapter": "simulator",
            "watchdog_mode": "flag_and_continue",
            "violation_limit": 5,
            "checkpoint_every": 5,
            "player_count": len(plan.seats),
        },
        "agents": agents,
    }


def _row_from_summary(plan: GamePlan, summary: dict[str, Any],
                      seats: tuple[tuple[int, Any], ...]) -> dict[str, Any]:
    """Integer-first per-match result row (ranks are derived in aggregate)."""
    by_pid = {entry["player_id"]: (civ, entry)
              for civ, entry in summary["scores"].items()}
    seats_doc = []
    for player_id, spec in seats:
        label = spec.label if hasattr(spec, "label") else spec
        civ, comp = by_pid[player_id]
        seats_doc.append({
            "player_id": player_id,
            "agent_id": f"{label}-{player_id}",
            "doctrine": label,
            "civ_name": civ,
            "scalar": scalarize(comp),
        })
    ordered = sorted(seats_doc, key=lambda e: (-e["scalar"], e["player_id"]))
    unique_top = (len(ordered) > 1
                  and ordered[0]["scalar"] > ordered[1]["scalar"])
    return {
        "match_id": plan.match_id,
        "seed": plan.seed,
        "rotation": plan.rotation,
        "player_count": len(plan.seats),
        "turns": summary["final_turn"],
        "violations": summary["violations_total"],
        "aborted": summary["aborted"],
        "dirty": (summary["violations_total"] > 0
                  or summary["aborted"] is not None),
        "seats": seats_doc,
        "winner_pid": ordered[0]["player_id"] if unique_top else None,
        "margin": (ordered[0]["scalar"] - ordered[1]["scalar"])
        if unique_top else 0,
    }


def summarize_existing(plan: GamePlan, run_dir: Path) -> dict[str, Any]:
    """Rebuild the result row for an already-completed run dir."""
    summary = json.loads((run_dir / "summary.json").read_text())
    return _row_from_summary(plan, summary, plan.seats)


async def run_game(plan: GamePlan, runs_root: Path) -> dict[str, Any]:
    """Run one planned game; refuse to touch a non-empty run dir."""
    run_dir = runs_root / plan.match_id
    if run_dir.exists() and any(run_dir.iterdir()):
        raise FileExistsError(
            f"refusing non-empty run dir (spend discipline): {run_dir}")
    run_dir.mkdir(parents=True)

    doc = config_doc(plan)
    spec = parse_config(doc)  # full config validation, incl. seat checks
    (run_dir / "config.json").write_text(
        json.dumps(doc, sort_keys=True, indent=2))

    trajectory: list[dict[str, Any]] = []
    arena = Arena(run_dir, spec)

    def on_turn_end(turn: int, scores: dict[str, dict[str, int]]) -> None:
        trajectory.append({
            "turn": turn,
            "scores": {
                civ: {**comp, "scalar": scalarize(comp)}
                for civ, comp in scores.items()
            },
            # static doctrines: None; adaptive seats: the switcher's
            # current doctrine that turn (pivot moments are visible here)
            "doctrines": {
                str(pid): getattr(rt, "current_doctrine", None)
                for pid, rt in arena.runtimes.items()
            },
        })

    summary = await arena.run(on_turn_end=on_turn_end)
    (run_dir / "trajectory.json").write_text(json.dumps(
        {"match_id": plan.match_id, "trajectory": trajectory},
        sort_keys=True, indent=1))
    return _row_from_summary(plan, summary, plan.seats)
