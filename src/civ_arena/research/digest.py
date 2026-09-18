"""Compressed per-match digests for the flash analysis leg.

Deterministic reductions only — never raw events.jsonl. A digest carries the
seating, final score vectors, the per-turn scalar trajectory sampled every 5
turns, and doctrine-switch markers, capped at a character budget so a batch
digest fits one model call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DIGEST_CHAR_BUDGET = 1200
SAMPLE_EVERY = 5


def _seat_line(seats: list[dict[str, Any]]) -> str:
    return " | ".join(
        f"seat{e['player_id']}={e['doctrine']}({e['civ_name']}) "
        f"scalar={e['scalar']}"
        for e in sorted(seats, key=lambda e: e["player_id"]))


def _score_token(comp: dict[str, Any]) -> str:
    return (f"c{comp['cities']}p{comp['population']}t{comp['techs']}"
            f"u{comp['units']}g{comp['gold']}={comp['scalar']}")


def _trajectory_lines(trajectory: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for point in trajectory:
        if point["turn"] % SAMPLE_EVERY and point["turn"] != trajectory[-1]["turn"]:
            continue
        scores = " ".join(
            f"{civ}:{_score_token(comp)}"
            for civ, comp in sorted(point["scores"].items()))
        switches = [
            f"seat{pid}->{doctrine}" for pid, doctrine in
            point.get("doctrines", {}).items() if doctrine
        ]
        suffix = f" SWITCH[{','.join(switches)}]" if switches else ""
        lines.append(f"t{point['turn']} {scores}{suffix}")
    return lines


def digest_match(row: dict[str, Any], run_dir: Path) -> str:
    """One match digest, <= DIGEST_CHAR_BUDGET chars (tail-truncated)."""
    traj_doc = json.loads((run_dir / "trajectory.json").read_text())
    lines = [
        f"MATCH {row['match_id']} seed={row['seed']} rot={row['rotation']}"
        f" turns={row['turns']} winner={row['winner_pid']}"
        f" margin={row['margin']} dirty={row['dirty']}",
        _seat_line(row["seats"]),
    ]
    body = _trajectory_lines(traj_doc["trajectory"])
    header = len("\n".join(lines)) + 1
    kept: list[str] = []
    used = header
    for line in body:
        if used + len(line) + 1 > DIGEST_CHAR_BUDGET:
            break
        kept.append(line)
        used += len(line) + 1
    lines.extend(kept)
    return "\n".join(lines)


def digest_batch(rows: list[dict[str, Any]], runs_root: Path) -> str:
    """All match digests of a batch, joined with blank lines."""
    return "\n\n".join(
        digest_match(row, runs_root / row["match_id"]) for row in rows)
