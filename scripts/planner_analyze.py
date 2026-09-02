#!/usr/bin/env python3
"""Paired analysis for Experiment-3: MCGS vs MCTS at equal budget.

H3 (docs/civgraph-program.md §5 M15d) is answered by PAIRED comparisons —
same seed, same side, only the search method varies — not by per-arm means.
The two arms are flags (M20a): --arm-a/--arm-b default to the exp3 arms,
so the historical invocation is byte-identical; diff = arm_b − arm_a and
the one-sided hypothesis is arm_b > arm_a, whatever the flags name.
Usage: planner_analyze.py [--arm-a A] [--arm-b B] results.json [more.json ...]
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

ARM_A_DEFAULT = "planner-mcts"
ARM_B_DEFAULT = "planner-mcgs"


def binom_p_ge(n: int, k: int) -> float:
    """Exact one-sided P(X >= k) for X ~ Binomial(n, 1/2)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    return sum(math.comb(n, i) for i in range(k, n + 1)) / (2.0 ** n)


def analyze(path: str, arm_a: str = ARM_A_DEFAULT,
            arm_b: str = ARM_B_DEFAULT) -> None:
    # printed labels follow the flags; the "planner-" prefix (the exp3 arm
    # convention) is stripped for the short inline labels, preserving the
    # historical output exactly under the default arms
    la = arm_a.removeprefix("planner-")
    lb = arm_b.removeprefix("planner-")
    doc = json.loads(Path(path).read_text())
    rows = doc["results"]

    # validity gate: every planner match ran to the same horizon cleanly
    bad = [r for r in rows
           if r["arm"] in (arm_a, arm_b)
           and (r["violations"] or r.get("planner_rejections"))]
    turns_set = {r["turns"] for r in rows}
    print(f"== {path}")
    print(f"   matches={len(rows)} turns={sorted(turns_set)} "
          f"dirty_planner_matches={len(bad)}")
    if bad:
        for r in bad:
            print(f"   DIRTY: {r['match_id']} viol={r['violations']} "
                  f"rej={r.get('planner_rejections')}")
        print("   WARNING: interpret diffs with the dirty rows excluded")

    by_key: dict[tuple[int, int], dict[str, dict]] = {}
    for r in rows:
        if r["arm"] in (arm_a, arm_b):
            by_key.setdefault((r["seed"], r["side"]), {})[r["arm"]] = r
    pairs = [(k, v[arm_a], v[arm_b])
             for k, v in sorted(by_key.items())
             if arm_a in v and arm_b in v]

    diffs = [b_row["value_differential"] - a_row["value_differential"]
             for _, a_row, b_row in pairs]
    clean = [d for d in diffs if d != 0]
    wins = sum(1 for d in clean if d > 0)
    losses = len(clean) - wins
    ties = len(diffs) - len(clean)
    print(f"   pairs={len(pairs)} ({lb} wins {wins} / {la} wins {losses} "
          f"/ ties {ties})")
    if clean:
        print(f"   paired diff: mean={statistics.mean(clean):+.0f} "
              f"median={statistics.median(clean):+.0f} "
              f"min={min(clean):+d} max={max(clean):+d}")
        p_one = binom_p_ge(len(clean), wins)
        print(f"   one-sided exact binomial p (H3: {lb}>{la}) = {p_one:.4f}")
    else:
        print("   no decidable pairs (all ties)")

    # per-side breakdown: map/first-move asymmetry must not hide an effect
    for side in sorted({k[1] for k, _, _ in pairs}):
        sd = [b_row["value_differential"] - a_row["value_differential"]
              for k, a_row, b_row in pairs if k[1] == side]
        w = sum(1 for d in sd if d > 0)
        losses = sum(1 for d in sd if d < 0)
        print(f"   side {side}: {lb} {w} / {la} {losses} / tie {len(sd)-w-losses}")

    # reuse diagnostics on decidable pairs
    if clean:
        win_tp = [b_row["transposition_hits"] for (_, _, b_row), d
                  in zip(pairs, diffs, strict=True) if d > 0]
        loss_tp = [b_row["transposition_hits"] for (_, _, b_row), d
                   in zip(pairs, diffs, strict=True) if d < 0]
        if win_tp and loss_tp:
            print(f"   mean tp_hits: {lb}-won={statistics.mean(win_tp):.0f} "
                  f"{la}-won={statistics.mean(loss_tp):.0f}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--arm-a", default=ARM_A_DEFAULT,
                    help=f"first arm of the pair (default {ARM_A_DEFAULT})")
    ap.add_argument("--arm-b", default=ARM_B_DEFAULT,
                    help=f"second arm of the pair (default {ARM_B_DEFAULT})")
    ap.add_argument("paths", nargs="+")
    opts = ap.parse_args()
    for p in opts.paths:
        analyze(p, arm_a=opts.arm_a, arm_b=opts.arm_b)


if __name__ == "__main__":
    main()

