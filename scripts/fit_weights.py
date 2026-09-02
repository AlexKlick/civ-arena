#!/usr/bin/env python3
"""M20c — offline ridge fit of the planner value head (DEV-ONLY, numpy).

Fits ``weights`` for ``value_of`` over the M19a label corpus: one row per
planner decision, x = the own-vs-rival component DELTAS at the decision
world (parsed from the trace ``root_key`` — the canonical abstract doc),
y = ``match_value_differential`` (the match-final label). Identical x rows
are NOT deduped: repeated decision worlds repeat their label, which is
exactly the frequency weighting a least-squares fit should see.

GOLD UNITS (documented decision): the abstract doc carries gold BUCKETED
(``gold // 25``) while ``DEFAULT_WEIGHTS['gold'] = 1`` multiplies RAW gold
in ``score_vector``. The gold feature here is the bucket CONVERTED BACK
(``bucket * 25``), so a fitted gold weight stays on the same RAW-gold
scale as the DEFAULT gold weight and the two are directly comparable.
The conversion floors to a bucket multiple — each row's gold delta is
exact to within 24 per player.

RIDGE: solve (X^T X + lambda*I) w = X^T y with numpy. lambda comes from a
small fixed ascending grid, picked by LEAVE-ONE-BATCH-OUT: train on the
b8+b16 docs, validate on the b32 docs (a deterministic split by parent
directory — the batch IS the path segment above the planner run dir);
ties break to the SMALLER lambda (first strict minimum over the ascending
grid).

SCALE PRESERVATION (critical — UCT_C = 140 is tuned to value_of's DEFAULT
scale, sum|w| = 161): the fitted float vector is rescaled to the DEFAULT
L1 norm (same direction, normalized magnitude), each component rounded to
an integer, then the integer vector is re-normalized to sum|w| == 161 by
UNIT adjustments: while sum|w| != 161, adjust the component with the
largest |w| (ties -> the earlier component in SCORE_COMPONENTS order) by
+1/-1 in the direction that closes the gap (a negative component moves
away from zero). Deterministic end to end: no sampling anywhere, a fixed
split, and a plain numpy solve at this size.

The artifact (canonical JSON, ints only, atomic tmp+replace):

    {"schema": 1,
     "weights": {"cities": int, "population": int, "gold": int,
                 "techs": int, "units": int},
     "fit": {"rows": int, "lambda": int(lambda * 10000),
             "val_mse_fixed": int(round(val_mse * 1000)),
             "corpus": {"docs": int, "sha256": sha256-of-sorted-concat},
             "scale_norm": "l1_161"}}

The corpus hash reuses the labels' own SOURCE hashes (the
events/summary/trace sha256 block every labels.json freezes): per-doc
digest = sha256(canonical(source block)), corpus sha256 = sha256 of the
sorted concatenation of those per-doc digests.

numpy is a DEV dependency ONLY — the runtime stays pure-integer
(pinned by tests/test_learned_weights.py::test_no_numpy_in_src).

    uv run python scripts/fit_weights.py \
        --labels-glob 'runs/exp3/b*/planner-*/labels.json' \
        --out configs/learned-weights-m20c.json
"""

from __future__ import annotations

import argparse
import glob as _glob
import json
from pathlib import Path
from typing import Any

import numpy as np

from civ_arena.canonical import atomic_write_text, canonical, sha256_hex
from civ_arena.game.sim.value import DEFAULT_WEIGHTS, SCORE_COMPONENTS

SCHEMA = 1

# ascending — the first strict validation minimum wins, so an exact tie
# between lambdas resolves to the SMALLER one by construction
LAMBDA_GRID: tuple[float, ...] = (0.1, 1.0, 10.0, 100.0, 1000.0)
LAMBDA_FIXED = 10_000      # artifact fixed point: int(lambda * 10_000)
MSE_FIXED = 1_000          # artifact fixed point: int(round(mse * 1_000))
TARGET_L1 = sum(abs(v) for v in DEFAULT_WEIGHTS.values())  # == 161
GOLD_BUCKET = 25           # abstract_doc: gold // 25 -> back via * 25
TRAIN_BATCHES = ("b8", "b16")
VAL_BATCHES = ("b32",)


def batch_of(path: Path) -> str:
    """The budget batch a labels doc belongs to: the path segment two
    levels above the labels.json file (runs/exp3/<b8>/planner-*/labels.json).
    """
    return path.parts[-3]


def _player_block(doc: dict[str, Any], pid: int) -> dict[str, Any] | None:
    block = doc.get(str(pid))
    if not isinstance(block, dict):
        return None
    return block


def _components(block: dict[str, Any]) -> dict[str, int]:
    """The five score components from one abstract-doc player block.
    gold is the bucket CONVERTED BACK to raw-gold scale (bucket * 25);
    techs is the researched COUNT; units is the summed unit counts."""
    researched = block.get("researched")
    units = block.get("units")
    return {
        "cities": int(block["cities"]),
        "population": int(block["population"]),
        "gold": int(block["gold_bucket"]) * GOLD_BUCKET,
        "techs": len(researched) if isinstance(researched, list) else 0,
        "units": sum(int(n) for n in units.values())
        if isinstance(units, dict) else 0,
    }


def feature_row(root_key: str, seat: int) -> list[int] | None:
    """x = own-minus-rival component deltas at the decision world, in
    SCORE_COMPONENTS order. Rival = the OTHER player with the highest
    DEFAULT-weight scalarized components (ties -> smaller player id) —
    mirroring value_of's strongest-rival rule on the same doc. No rival
    block at all mirrors value_of's no-rival branch (own score stands).
    None when the own block is missing (malformed row — skipped)."""
    doc = json.loads(root_key)
    if not isinstance(doc, dict):
        return None
    own = _player_block(doc, seat)
    if own is None:
        return None
    own_c = _components(own)
    rival_ids = sorted(
        int(p) for p in doc
        if p.isdigit() and int(p) != seat and _player_block(doc, int(p)))
    if not rival_ids:
        return [own_c[k] for k in SCORE_COMPONENTS]
    rival_c = max(
        (_components(_player_block(doc, p) or {}) for p in rival_ids),
        key=lambda c: sum(DEFAULT_WEIGHTS[k] * c[k] for k in SCORE_COMPONENTS))
    return [own_c[k] - rival_c[k] for k in SCORE_COMPONENTS]


def load_rows(paths: list[Path]) -> tuple[list[list[int]], list[int],
                                          list[str], dict[str, int],
                                          list[str]]:
    """Every decision of every doc -> (X rows, y labels, per-row batch
    tags, per-batch row counts, per-doc source digests). Decisions whose
    label is None (aborted matches) are skipped — a fabricated 0 would
    poison the fit."""
    xs: list[list[int]] = []
    ys: list[int] = []
    batches: list[str] = []
    by_batch: dict[str, int] = {}
    digests: list[str] = []
    for path in paths:
        doc = json.loads(path.read_text(encoding="utf-8"))
        digests.append(sha256_hex(canonical(doc["source"])))
        batch = batch_of(path)
        for dec in doc.get("decisions", []):
            label = dec.get("match_value_differential")
            row = feature_row(dec["root_key"], int(dec["seat"]))
            if row is None or label is None:
                continue
            xs.append(row)
            ys.append(int(label))
            batches.append(batch)
            by_batch[batch] = by_batch.get(batch, 0) + 1
    return xs, ys, batches, by_batch, digests


def ridge_solve(x: np.ndarray, y: np.ndarray, lam: float) -> np.ndarray:
    """(X^T X + lam*I) w = X^T y — plain solve, deterministic at this size."""
    a = x.T @ x + lam * np.eye(x.shape[1])
    return np.linalg.solve(a, x.T @ y)


def quantize(w: np.ndarray) -> list[int]:
    """Float direction -> integer weights at DEFAULT L1 scale.

    1. rescale to sum|w| == TARGET_L1 (same direction, DEFAULT magnitude);
    2. round each component (numpy round: half-to-even, deterministic);
    3. re-normalize the INTEGER vector: while sum|w| != TARGET_L1, adjust
       the largest-|w| component (ties -> earlier in SCORE_COMPONENTS
       order) by +1/-1 toward the target — a negative component moves
       AWAY from zero, a zero component can only grow (delta > 0 only).
    """
    l1 = float(np.abs(w).sum())
    if l1 <= 0:
        raise SystemExit("error: fitted weight vector is all-zero — "
                         "refusing to quantize a degenerate fit")
    ints = [int(v) for v in np.round(w * (TARGET_L1 / l1))]
    delta = TARGET_L1 - sum(abs(v) for v in ints)
    while delta != 0:
        step = 1 if delta > 0 else -1
        eligible = [i for i in range(len(ints)) if ints[i] != 0 or step > 0]
        i = max(eligible, key=lambda j: (abs(ints[j]), -j))
        ints[i] = ints[i] + step if ints[i] >= 0 else ints[i] - step
        delta -= step
    return ints


def fit(paths: list[Path]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The whole deterministic pipeline: rows -> lambda pick -> ridge ->
    quantize -> (artifact doc, human report). The artifact is ints only,
    canonical-ready; the report carries floats for stdout only."""
    xs, ys, batches, by_batch, digests = load_rows(paths)
    if not xs:
        raise SystemExit("error: no usable decision rows in the corpus")
    x = np.array(xs, dtype=np.float64)
    y = np.array(ys, dtype=np.float64)
    rb = np.array(batches)
    train_rows = np.isin(rb, TRAIN_BATCHES)
    val_rows = np.isin(rb, VAL_BATCHES)
    if not train_rows.any():
        raise SystemExit(f"error: no training rows (need {TRAIN_BATCHES})")
    if not val_rows.any():
        raise SystemExit(f"error: no validation rows (need {VAL_BATCHES})")

    best_lam, best_mse, best_w = None, None, None
    for lam in LAMBDA_GRID:  # ascending: a strict < keeps the smaller lambda
        w = ridge_solve(x[train_rows], y[train_rows], lam)
        resid = x[val_rows] @ w - y[val_rows]
        mse = float((resid @ resid) / int(val_rows.sum()))
        if best_mse is None or mse < best_mse:
            best_lam, best_mse, best_w = lam, mse, w

    weights = quantize(best_w)
    artifact = {
        "schema": SCHEMA,
        "weights": dict(zip(SCORE_COMPONENTS, weights, strict=True)),
        "fit": {
            "rows": len(xs),
            "lambda": int(round(best_lam * LAMBDA_FIXED)),
            "val_mse_fixed": int(round(best_mse * MSE_FIXED)),
            "corpus": {
                "docs": len(paths),
                "sha256": sha256_hex("".join(sorted(digests))),
            },
            "scale_norm": "l1_161",
        },
    }
    report = {"by_batch": by_batch, "best_lam": best_lam, "best_mse": best_mse,
              "float_weights": [float(v) for v in best_w]}
    return artifact, report


def _assert_out_safe(out: Path, inputs: list[Path]) -> None:
    """--out must never alias an input labels doc (the labels.py
    --index-out resolve() discipline — before ANY write)."""
    resolved = out.resolve()
    for path in inputs:
        if path.resolve() == resolved:
            raise SystemExit(
                f"error: --out {out} would overwrite the corpus doc {path} "
                "— refusing")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="civ-arena-fit-weights",
        description="Ridge-fit the planner value-head weights over M19a "
                    "label docs; write the quantized integer artifact.")
    ap.add_argument("--labels-glob", required=True, metavar="GLOB",
                    help="label docs to fit, e.g. "
                         "'runs/exp3/b*/planner-*/labels.json'")
    ap.add_argument("--out", required=True, type=Path, metavar="PATH",
                    help="artifact path to write (must not alias an input)")
    opts = ap.parse_args(argv)

    paths = [Path(p) for p in sorted(_glob.glob(opts.labels_glob))]
    if not paths:
        raise SystemExit(f"error: no label docs under {opts.labels_glob}")
    _assert_out_safe(opts.out, paths)

    artifact, report = fit(paths)
    atomic_write_text(opts.out, canonical(artifact) + "\n")

    w = artifact["weights"]
    print(f"docs={artifact['fit']['corpus']['docs']} "
          f"rows={artifact['fit']['rows']} per-batch={report['by_batch']}")
    print(f"lambda={report['best_lam']} "
          f"(fixed {artifact['fit']['lambda']}) "
          f"val_mse={report['best_mse']:.3f} "
          f"(fixed {artifact['fit']['val_mse_fixed']})")
    print("float weights (pre-quantization): "
          + " ".join(f"{k}={v:.4f}" for k, v
                     in zip(SCORE_COMPONENTS, report["float_weights"],
                            strict=True)))
    print(f"quantized weights: {w} sum|w|={sum(abs(v) for v in w.values())} "
          f"(target {TARGET_L1})")
    print(f"corpus sha256={artifact['fit']['corpus']['sha256']}")
    print(f"wrote {opts.out}")


if __name__ == "__main__":
    main()
