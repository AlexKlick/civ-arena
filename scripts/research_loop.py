#!/usr/bin/env python
"""Research loop CLI: plan | run | aggregate a strategy batch.

    uv run python scripts/research_loop.py plan --config configs/research/batch-001.yaml
    uv run python scripts/research_loop.py run --config configs/research/batch-001.yaml \
        --runs-root runs/research/batch-001
    uv run python scripts/research_loop.py aggregate --runs-root runs/research/batch-001

``run`` is resumable: completed games (summary.json present) are skipped and
re-summarized, never re-spent. ``aggregate`` rewrites results.json's
aggregate section from the stored rows and renders results.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import yaml

from civ_arena.research.aggregate import aggregate
from civ_arena.research.matrix import plan_batch
from civ_arena.research.runner import run_game, summarize_existing


def load_batch(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    batch = doc.get("batch")
    if not isinstance(batch, dict):
        raise SystemExit(f"{path}: missing 'batch:' block")
    for key in ("batch_id", "roster", "seeds"):
        if key not in batch:
            raise SystemExit(f"{path}: batch.{key} is required")
    roster_raw = batch["roster"]
    if not isinstance(roster_raw, list):
        raise SystemExit(f"{path}: batch.roster must be a list")
    seeds = [int(s) for s in batch["seeds"]]
    return {
        "batch_id": str(batch["batch_id"]),
        "roster": roster_raw,  # doctrine names and adaptive seat maps mix
        "seeds": seeds,
        "max_turns": int(batch.get("max_turns", 40)),
    }


def cmd_plan(args: argparse.Namespace) -> None:
    batch = load_batch(Path(args.config))
    plans = plan_batch(batch["batch_id"], batch["roster"], batch["seeds"],
                       max_turns=batch["max_turns"])
    print(f"{len(plans)} games: {batch['batch_id']} roster={batch['roster']}")
    for plan in plans:
        seating = ", ".join(f"seat{pid}={spec.label}"
                            for pid, spec in plan.seats)
        print(f"  {plan.match_id}  seed={plan.seed} rot={plan.rotation}"
              f"  {seating}")


async def cmd_run(args: argparse.Namespace) -> None:
    batch = load_batch(Path(args.config))
    runs_root = Path(args.runs_root)
    runs_root.mkdir(parents=True, exist_ok=True)
    plans = plan_batch(batch["batch_id"], batch["roster"], batch["seeds"],
                       max_turns=batch["max_turns"])

    rows: list[dict] = []
    for index, plan in enumerate(plans, start=1):
        run_dir = runs_root / plan.match_id
        if (run_dir / "summary.json").exists():
            row = summarize_existing(plan, run_dir)
            print(f"[{index}/{len(plans)}] SKIP {plan.match_id} (complete)")
        else:
            print(f"[{index}/{len(plans)}] RUN  {plan.match_id}", flush=True)
            row = await run_game(plan, runs_root)
            verdict = "dirty" if row["dirty"] else "clean"
            print(f"    -> {verdict}, winner={row['winner_pid']}, "
                  f"margin={row['margin']}")
        rows.append(row)

    results = {"batch": batch, "rows": rows, "aggregates": aggregate(rows)}
    (runs_root / "results.json").write_text(
        json.dumps(results, sort_keys=True, indent=1))
    print(f"results -> {runs_root / 'results.json'}")


def cmd_aggregate(args: argparse.Namespace) -> None:
    results_path = Path(args.runs_root) / "results.json"
    results = json.loads(results_path.read_text())
    results["aggregates"] = aggregate(results["rows"])
    results_path.write_text(json.dumps(results, sort_keys=True, indent=1))
    write_summary_md(Path(args.runs_root), results)
    print(f"aggregates -> {results_path} + results.md")


def write_summary_md(runs_root: Path, results: dict) -> None:
    agg = results["aggregates"]
    lines = [
        f"# Batch {results['batch']['batch_id']}", "",
        f"games: {agg['games']['total']} total, {agg['games']['clean']} clean, "
        f"{agg['games']['dirty']} dirty (excluded: "
        f"{agg['games']['dirty_ids']})", "",
        "## Per strategy (clean rows, descriptive only)", "",
        "| doctrine | matches | wins | ties | mean_rank | mean_scalar |",
        "|---|---|---|---|---|---|",
    ]
    for doctrine, t in agg["per_strategy"].items():
        lines.append(
            f"| {doctrine} | {t['matches']} | {t['wins']} | {t['ties']} | "
            f"{t['mean_rank']} | {t['mean_scalar']} |")
    lines += ["", "## Seat decomposition (seat-luck detector)", "",
              "| doctrine@seat | n | mean_rank |", "|---|---|---|"]
    for key, cell in agg["seat_decomposition"].items():
        lines.append(f"| {key} | {cell['n']} | {cell['mean_rank']} |")
    (runs_root / "results.md").write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan_p = sub.add_parser("plan", help="print the game matrix")
    plan_p.add_argument("--config", required=True)

    run_p = sub.add_parser("run", help="run every planned game (resumable)")
    run_p.add_argument("--config", required=True)
    run_p.add_argument("--runs-root", required=True)

    agg_p = sub.add_parser("aggregate", help="rewrite aggregates + results.md")
    agg_p.add_argument("--runs-root", required=True)

    args = parser.parse_args()
    if args.command == "plan":
        cmd_plan(args)
    elif args.command == "run":
        asyncio.run(cmd_run(args))
    else:
        cmd_aggregate(args)


if __name__ == "__main__":
    sys.exit(main())
