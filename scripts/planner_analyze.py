#!/usr/bin/env python3
"""Paired analysis for Experiment-3: MCGS vs MCTS at equal budget.

H3 (docs/civgraph-program.md §5 M15d) is answered by PAIRED comparisons —
same seed, same side, only the search method varies — not by per-arm means.
Usage: exp3_analyze.py runs/exp3/b32/planner-experiment.json [more.json ...]
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path


def binom_p_ge(n: int, k: int) -> float:
    """Exact one-sided P(X >= k) for X ~ Binomial(n, 1/2)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    return sum(math.comb(n, i) for i in range(k, n + 1)) / (2.0 ** n)


def analyze(path: str) -> None:
    doc = json.loads(Path(path).read_text())
    rows = doc["results"]

    # validity gate: every planner match ran to the same horizon cleanly
    bad = [r for r in rows
           if r["arm"] in ("planner-mcgs", "planner-mcts")
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
        if r["arm"] in ("planner-mcgs", "planner-mcts"):
            by_key.setdefault((r["seed"], r["side"]), {})[r["arm"]] = r
    pairs = [(k, v["planner-mcts"], v["planner-mcgs"])
             for k, v in sorted(by_key.items())
             if "planner-mcgs" in v and "planner-mcts" in v]

    diffs = [mc["value_differential"] - mcts["value_differential"]
             for _, mcts, mc in pairs]
    clean = [d for d in diffs if d != 0]
    wins = sum(1 for d in clean if d > 0)
    losses = len(clean) - wins
    ties = len(diffs) - len(clean)
    print(f"   pairs={len(pairs)} (mcgs wins {wins} / mcts wins {losses} "
          f"/ ties {ties})")
    if clean:
        print(f"   paired diff: mean={statistics.mean(clean):+.0f} "
              f"median={statistics.median(clean):+.0f} "
              f"min={min(clean):+d} max={max(clean):+d}")
        p_one = binom_p_ge(len(clean), wins)
        print(f"   one-sided exact binomial p (H3: mcgs>mcts) = {p_one:.4f}")
    else:
        print("   no decidable pairs (all ties)")

    # per-side breakdown: map/first-move asymmetry must not hide an effect
    for side in sorted({k[1] for k, _, _ in pairs}):
        sd = [mc["value_differential"] - mcts["value_differential"]
              for k, mcts, mc in pairs if k[1] == side]
        w = sum(1 for d in sd if d > 0)
        losses = sum(1 for d in sd if d < 0)
        print(f"   side {side}: mcgs {w} / mcts {losses} / tie {len(sd)-w-losses}")

    # reuse diagnostics on decidable pairs
    if clean:
        win_tp = [mc["transposition_hits"] for (_, _, mc), d
                  in zip(pairs, diffs, strict=True) if d > 0]
        loss_tp = [mc["transposition_hits"] for (_, _, mc), d
                   in zip(pairs, diffs, strict=True) if d < 0]
        if win_tp and loss_tp:
            print(f"   mean tp_hits: mcgs-won={statistics.mean(win_tp):.0f} "
                  f"mcts-won={statistics.mean(loss_tp):.0f}")
    print()


if __name__ == "__main__":
    for p in sys.argv[1:]:
        analyze(p)
