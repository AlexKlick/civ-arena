"""M19c — CLI over the offline pattern miners (civ_arena.planner.mining).

Globs M19a label docs, runs both miners plus the heuristic roll-up,
writes ONE canonical JSON artifact (atomic tmp+replace) and prints a
human table. Read-only over the corpus: the only path written is --out,
guarded — before ANY write — against aliasing a mined labels.json or
any protected run artifact beside it (the labels.py --index-out
resolve() discipline).

    uv run python scripts/pattern_mining.py \
        --labels-glob 'runs/exp3/b*/planner-*/labels.json' \
        --out /tmp/pattern-mining.json --min-support 3
"""

from __future__ import annotations

import argparse
import glob as _glob
import json
from pathlib import Path
from typing import Any

from civ_arena.planner import mining

# human-table bounds (the artifact carries everything; the table is a
# digest, deterministic top-N by max side support then lexicographic)
_TABLE_SEQUENTIAL = 15
_TABLE_HEURISTICS = 30


def _print_report(artifact: dict[str, Any], out: Path,
                  min_support: int) -> None:
    axis = artifact["source"]["axis"]
    print(f"docs={axis['docs']} skipped={axis['skipped_docs']} "
          f"decisions={axis['decisions']} median_diff={axis['median_diff']} "
          f"wins={axis['docs_wins']} losses={axis['docs_losses']} "
          f"high={axis['docs_high']} low={axis['docs_low']}")
    sequential = artifact["sequential"]
    print(f"sequential patterns (min_support={min_support}): "
          f"{len(sequential)}")
    top = sorted(sequential,
                 key=lambda r: (-max(r[f"support_{s}"] for s in mining.SIDES),
                                r["pattern"]))[:_TABLE_SEQUENTIAL]
    for row in top:
        print(f"  {' > '.join(row['pattern']):<58} "
              f"high={row['support_high']:>4} low={row['support_low']:>4} "
              f"wins={row['support_wins']:>4} "
              f"losses={row['support_losses']:>4}")
    motifs = artifact["motifs"]
    by_side: dict[str, int] = {side: 0 for side in mining.SIDES}
    for row in motifs:
        by_side[row["side"]] += 1
    print(f"motif itemsets: {len(motifs)} "
          + " ".join(f"{side}={n}" for side, n in by_side.items()))
    comparisons = artifact["comparisons"]
    below = sum(1 for row in comparisons if row["below_min_support"])
    print(f"median-axis comparison rows: {len(comparisons)} "
          f"(below_min_support={below})")
    heuristics = artifact["heuristics"]
    kinds = {kind: 0 for kind in ("promote", "demote", "context")}
    for row in heuristics:
        kinds[row["kind"]] += 1
    print(f"heuristics: {len(heuristics)} "
          + " ".join(f"{k}={n}" for k, n in kinds.items()))
    for row in heuristics[:_TABLE_HEURISTICS]:
        print(f"  {row['option']:<15} {row['kind']:<8} {row['evidence']}")
    print(f"wrote {out}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="civ-arena-pattern-mining",
        description="Mine M19a label docs into the sequential/motif/"
                    "heuristic pattern-mining artifact.")
    ap.add_argument("--labels-glob", required=True, metavar="GLOB",
                    help="label docs to mine, e.g. "
                         "'runs/exp3/b*/planner-*/labels.json'")
    ap.add_argument("--out", required=True, type=Path, metavar="PATH",
                    help="artifact path to write (must not alias an input)")
    ap.add_argument("--min-support", type=int, default=3, metavar="N",
                    help="minimum support: RUNS for sequential patterns, "
                         "DECISIONS for motif itemsets (default 3)")
    ap.add_argument("--max-itemset-depth", type=int, default=3,
                    metavar="N",
                    help="maximum motif itemset cardinality (default 3)")
    opts = ap.parse_args(argv)
    if opts.min_support < 1:
        ap.error("--min-support must be >= 1")
    if opts.max_itemset_depth < 1:
        ap.error("--max-itemset-depth must be >= 1")

    paths = [Path(p) for p in sorted(_glob.glob(opts.labels_glob))]
    if not paths:
        raise SystemExit(f"error: no label docs under {opts.labels_glob}")
    # guards before ANY write
    mining.assert_out_safe(opts.out, paths)

    docs: list[dict[str, Any]] = []
    for path in paths:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise SystemExit(f"error: {path} is not a JSON object")
        docs.append(doc)

    artifact = mining.build_artifact(
        docs,
        source={"glob": opts.labels_glob,
                "docs": [str(p) for p in paths],
                "min_support": opts.min_support,
                "max_itemset_depth": opts.max_itemset_depth},
        min_support=opts.min_support,
        max_itemset_depth=opts.max_itemset_depth)
    mining.write_artifact(opts.out, artifact)
    _print_report(artifact, opts.out, opts.min_support)


if __name__ == "__main__":
    main()
