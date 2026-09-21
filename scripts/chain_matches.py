#!/usr/bin/env python3
"""Chain N live arch1 matches unattended — one orchestrator per match.

Wraps ``scripts/live_zero_touch.py`` (the proven smoke-006/007 entry) in a
serial loop: launch -> wait -> classify the run's summary.json -> record ->
next. The chain is the SINGLE orchestrator for the whole series; nothing
else may touch the wire or the game while it runs (monitor via run-dir
mtimes and /tmp logs only).

Verdics per match:
  pass    clean == True and completed_rounds == rounds
  partial a summary exists with 0 < completed_rounds < rounds (or not clean)
  fail    no summary.json, or completed_rounds == 0

Stop conditions: all matches fired; --max-consecutive-failures consecutive
``fail`` verdics (the slot is wedged — do not burn it further); or the
overall --budget-s wall clock.

Every launcher log goes to /tmp/<run_id>.launcher.log — NEVER inside the
runs root (redirecting there creates the run dir before the harness's
existence check fires: "dispatch run id already exists").
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def classify(summary_path: Path, rounds: int) -> tuple[str, dict]:
    """(verdict, row). Reads summary.json defensively — every field the
    smoke ladder observed is optional from this side of the seam."""
    row: dict = {}
    if not summary_path.exists():
        return "fail", {"reason": "no summary.json"}
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return "fail", {"reason": f"unreadable summary: {exc}"}
    completed = int(summary.get("completed_rounds", 0) or 0)
    clean = bool(summary.get("clean", False))
    row = {
        "completed_rounds": completed,
        "clean": clean,
        "final_turn": summary.get("final_turn"),
        "violations_total": summary.get("violations_total"),
        "failure_stage": summary.get("failure_stage"),
        "aborted": summary.get("aborted"),
        "elapsed_s": summary.get("elapsed_s"),
    }
    if completed == 0:
        row["reason"] = "zero completed rounds"
        return "fail", row
    if clean and completed >= rounds:
        return "pass", row
    return "partial", row


def should_stop(consecutive_failures: int, max_consecutive: int,
                elapsed_s: float, budget_s: float) -> str | None:
    """None = keep going; a string = the stop reason."""
    if consecutive_failures >= max_consecutive:
        return (f"{consecutive_failures} consecutive failures "
                f"(max {max_consecutive})")
    if elapsed_s >= budget_s:
        return f"budget exhausted ({elapsed_s:.0f}s >= {budget_s:.0f}s)"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path,
                        help="the worktree live_zero_touch runs from")
    parser.add_argument("--python", required=True, type=Path,
                        help="venv python for live_zero_touch")
    parser.add_argument("--config", required=True,
                        help="config path, repo-relative")
    parser.add_argument("--rounds", type=int, default=30)
    parser.add_argument("--count", type=int, default=3,
                        help="matches to chain")
    parser.add_argument("--run-prefix", required=True,
                        help="run ids become <prefix>-001, -002, ...")
    parser.add_argument("--runs-root", default="runs",
                        help="repo-relative runs root")
    parser.add_argument("--fresh-x-first", action="store_true",
                        help="pass --fresh-x on the FIRST match only "
                             "(X is young from the prior boot afterwards)")
    parser.add_argument("--match-timeout-s", type=float, default=5400.0)
    parser.add_argument("--budget-s", type=float, default=None,
                        help="overall wall clock (default: count * timeout)")
    parser.add_argument("--max-consecutive-failures", type=int, default=2)
    parser.add_argument("--settle-s", type=float, default=30.0,
                        help="cooldown between matches (tuner single-client)")
    args = parser.parse_args()

    repo = args.repo.resolve()
    zero_touch = repo / "scripts" / "live_zero_touch.py"
    runs_root = repo / args.runs_root
    chain_dir = runs_root / f"chain-{args.run_prefix}"
    chain_dir.mkdir(parents=True, exist_ok=True)
    ledger = chain_dir / "chain.jsonl"
    budget = args.budget_s or args.count * args.match_timeout_s

    env = {
        **os.environ,
        "HOME": "/home/alexk",
        "DISPLAY": ":1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": str(repo / "src"),
    }

    started = time.monotonic()
    consecutive_failures = 0
    rows: list[dict] = []
    print(f"chain {args.run_prefix}: {args.count} matches x {args.rounds} "
          f"rounds, budget {budget:.0f}s", flush=True)

    for index in range(1, args.count + 1):
        run_id = f"{args.run_prefix}-{index:03d}"
        run_dir = runs_root / run_id
        if run_dir.exists() or (runs_root / f"{run_id}-startup").exists():
            print(f"[{index}/{args.count}] REFUSE {run_id}: run dir exists",
                  flush=True)
            return 2
        launcher_log = Path(f"/tmp/{run_id}.launcher.log")

        cmd = [str(args.python), str(zero_touch),
               "--session", "arch1", "--kill-first",
               "--config", args.config,
               "--rounds", str(args.rounds),
               "--run-id", run_id,
               "--runs-root", args.runs_root]
        if index == 1 and args.fresh_x_first:
            cmd.append("--fresh-x")

        print(f"[{index}/{args.count}] LAUNCH {run_id} -> {launcher_log}",
              flush=True)
        launch_t = time.monotonic()
        rc: int | None = None
        with launcher_log.open("w", encoding="utf-8") as log:
            try:
                proc = subprocess.run(
                    cmd, cwd=repo, env=env, stdout=log, stderr=log,
                    timeout=args.match_timeout_s, check=False)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                rc = None
                print(f"    TIMEOUT after {args.match_timeout_s:.0f}s",
                      flush=True)
        wall = time.monotonic() - launch_t

        verdict, detail = classify(run_dir / "summary.json", args.rounds)
        row = {"run_id": run_id, "index": index, "verdict": verdict,
               "launcher_rc": rc, "wall_s": round(wall, 1), **detail}
        rows.append(row)
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
        print(f"    -> {verdict} rc={rc} wall={wall:.0f}s "
              f"rounds={detail.get('completed_rounds')}", flush=True)

        if verdict == "fail":
            consecutive_failures += 1
        else:
            consecutive_failures = 0

        stop = should_stop(consecutive_failures, args.max_consecutive_failures,
                           time.monotonic() - started, budget)
        if stop or index == args.count:
            break
        time.sleep(args.settle_s)

    passes = sum(1 for r in rows if r["verdict"] == "pass")
    partials = sum(1 for r in rows if r["verdict"] == "partial")
    fails = sum(1 for r in rows if r["verdict"] == "fail")
    md = [f"# Chain {args.run_prefix}", "",
          f"{len(rows)} fired: {passes} pass / {partials} partial / "
          f"{fails} fail, total wall "
          f"{(time.monotonic() - started) / 60:.1f} min", "",
          "| run | verdict | rounds | clean | violations | stage | wall_s |",
          "|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['run_id']} | {r['verdict']} | "
                  f"{r.get('completed_rounds')} | {r.get('clean')} | "
                  f"{r.get('violations_total')} | {r.get('failure_stage')} | "
                  f"{r.get('wall_s')} |")
    (chain_dir / "chain.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    stop = should_stop(consecutive_failures, args.max_consecutive_failures,
                       time.monotonic() - started, budget)
    print(f"chain done: {passes} pass / {partials} partial / {fails} fail"
          + (f" — stopped early: {stop}" if stop else ""), flush=True)
    return 0 if (stop is None and fails == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
