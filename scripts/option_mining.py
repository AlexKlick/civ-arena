"""M16d — option mining over recorded planner trajectories.

Reads every planner match under the given runs roots (Experiment-3 corpus
shape: runs/<root>/planner-*/{planner/trace.json,summary.json}) and
aggregates per-option evidence: how often each option was a candidate,
was chosen, held across reselections, and what the match's value
differential was when it was the LAST option active. Output: a printed
table plus a canonical JSON beside nothing (stdout redirect is the
caller's choice) — evidence for promote/demote decisions over compiler
parameters. Read-only over artifacts; no strategy is changed here.

    uv run python scripts/option_mining.py runs/exp3/b32 runs/exp3/b16
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from civ_arena.game.sim.value import score_differential


def mine_root(root: Path) -> list[dict]:
    rows = []
    for run_dir in sorted(root.glob("planner-*")):
        trace_p = run_dir / "planner" / "trace.json"
        summary_p = run_dir / "summary.json"
        if not trace_p.exists() or not summary_p.exists():
            continue
        trace = json.loads(trace_p.read_text())
        summary = json.loads(summary_p.read_text())
        pid = next(a_pid for a_pid, s in _planner_seats(summary, run_dir))
        diff = score_differential(summary["scores"], pid)
        rows.append({
            "match": run_dir.name, "budget": _budget_of(run_dir.name),
            "diff": diff, "turns": summary["final_turn"],
            "decisions": trace,
        })
    return rows


def _planner_seats(summary: dict, run_dir: Path):
    # the planner seat is whoever the match id says: planner-<method>-s<seed>-p<side>
    name = run_dir.name
    side = int(name.rsplit("-p", 1)[1])
    yield side, summary


def _budget_of(_name: str) -> int:
    return -1  # filled by the caller from the root path


def aggregate(roots: list[Path]) -> dict:
    stats: dict[str, dict] = {}
    matches = 0
    for root in roots:
        if not root.exists():
            continue
        for row in mine_root(root):
            matches += 1
            last = None
            for dec in row["decisions"]:
                for oid in dec["candidates"]:
                    s = stats.setdefault(oid, {"candidate": 0, "chosen": 0,
                                               "held_spans": 0})
                    s["candidate"] += 1
                if dec["chosen"] in stats:
                    stats[dec["chosen"]]["chosen"] += 1
                if last == dec["chosen"]:
                    stats[dec["chosen"]]["held_spans"] += 1
                last = dec["chosen"]
            if last is not None:
                s = stats.setdefault(last, {"candidate": 0, "chosen": 0,
                                            "held_spans": 0})
                s.setdefault("last_active_diffs", []).append(row["diff"])
    for s in stats.values():
        diffs = s.pop("last_active_diffs", [])
        if diffs:
            s["last_active_mean_diff"] = sum(diffs) // len(diffs)
            s["last_active_n"] = len(diffs)
    return {"matches": matches, "options": stats}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="+", type=Path)
    opts = ap.parse_args()
    doc = aggregate(opts.roots)
    print(f"matches={doc['matches']}")
    hdr = (f"{'option':<16} {'cand':>6} {'chosen':>7} {'held':>6} "
           f"{'lastN':>6} {'lastMeanDiff':>12}")
    print(hdr)
    for oid, s in sorted(doc["options"].items()):
        print(f"{oid:<16} {s['candidate']:>6} {s['chosen']:>7} "
              f"{s['held_spans']:>6} {s.get('last_active_n', 0):>6} "
              f"{s.get('last_active_mean_diff', 0):>12}")
    print(json.dumps(doc, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
