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

PROVENANCE (Codex M20c C1 — the casebase miner's discipline): ``--index``
(repeatable, REQUIRED) names the M19a corpus indexes; every matched
labels.json must appear in one with a MATCHING byte digest
(labels.py's ``labels_sha256``), so an unindexed doc or a doc edited
after indexing refuses. The artifact's ``corpus.sha256`` binds the DATA,
not just each doc's source block: sha256 over the canonical sorted
manifest of ``{batch, run, sha256}`` (run dir, batch role, VERIFIED
labels byte-hash) — any decisions/root_key/label change now changes the
binding.

RIDGE: solve (X^T X + lambda*I) w = X^T y with numpy. lambda comes from a
small fixed ascending grid. SELECTION uses leave-one-batch-out: train on
the b8+b16 docs, validate on the b32 docs (deterministic split by parent
directory — the batch IS the path segment above the planner run dir);
ties break to the SMALLER lambda (first strict minimum over the ascending
grid). The SHIPPED weights are then REFIT ON ALL ROWS at the selected
lambda (``fit.refit`` records this; ``fit.split`` records
train/val docs AND rows separately — the artifact states exactly what
ran). Batches outside {b8, b16, b32} refuse (Codex M20c C8).

SCALE PRESERVATION + QUANTIZATION (Codex M20c C7): the fitted float
vector is rescaled to the DEFAULT L1 norm (sum|w| = 161 — UCT_C = 140 is
tuned to the DEFAULT scale), magnitudes are floored, and the integer
correction is allocated to the components with the LARGEST FRACTIONAL
RESIDUALS (ties -> the earlier component in SCORE_COMPONENTS order; each
bump applies in the component's SIGN direction) — largest-remainder
allocation, which preserves direction where a largest-|w| lump would
skew it. sum|w| pins to 161 EXACTLY (asserted; a failure refuses).

DECLARED LIMITATION (Codex M20c C6 ruling): UCT_C=140 is NOT recalibrated
for this head — L1 normalization bounds the NORM, not per-branch Q-value
dispersion. Recorded as ``uct_c_note`` in the artifact and mirrored in
the experiment runner docstring; calibration is a future rung.

The artifact (canonical JSON, ints only, atomic tmp+replace):

    {"schema": 1,
     "weights": {"cities": int, "population": int, "gold": int,
                 "techs": int, "units": int},
     "uct_c_note": "UCT_C=140 not recalibrated for this head; L1 "
                   "normalization bounds the norm, not per-branch dispersion",
     "fit": {"rows": int, "lambda": int(lambda * 10000),
             "val_mse_fixed": int(round(val_mse * 1000)),
             "refit": "all_rows_at_selected_lambda",
             "split": {"train_docs": int, "val_docs": int,
                       "train_rows": int, "val_rows": int},
             "corpus": {"docs": int, "sha256": manifest-sha256,
                        "indexes": [{"path": str, "sha256": file-sha256}]},
             "scale_norm": "l1_161"}}

DETERMINISM: no sampling anywhere, a fixed split, a plain numpy solve at
this size — two runs over the same corpus produce byte-identical
artifact bytes (pinned on a synthetic fixture by
tests/test_learned_weights.py).

numpy is a DEV dependency ONLY — the runtime stays pure-integer
(pinned by tests/test_learned_weights.py::test_no_numpy_in_src).

    uv run python scripts/fit_weights.py \
        --labels-glob 'runs/exp3/b*/planner-*/labels.json' \
        --index runs/labels-exp3-b8.json --index runs/labels-exp3-b16.json \
        --index runs/labels-exp3-b32.json \
        --out configs/learned-weights-m20c.json
"""

from __future__ import annotations

import argparse
import glob as _glob
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

from civ_arena.canonical import atomic_write_text, canonical, sha256_hex
from civ_arena.game.sim.value import DEFAULT_WEIGHTS, SCORE_COMPONENTS
from civ_arena.planner.casebase import (
    _assert_out_safe,
    _load_index_entries,
    _verify_provenance,
)

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
ALLOWED_BATCHES = TRAIN_BATCHES + VAL_BATCHES  # C8: anything else refuses

UCT_C_NOTE = ("UCT_C=140 not recalibrated for this head; L1 normalization "
              "bounds the norm, not per-branch dispersion")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
                                          list[str], dict[str, int]]:
    """Every decision of every doc -> (X rows, y labels, per-row batch
    tags, per-batch row counts). Decisions whose label is None (aborted
    matches) are skipped — a fabricated 0 would poison the fit."""
    xs: list[list[int]] = []
    ys: list[int] = []
    batches: list[str] = []
    by_batch: dict[str, int] = {}
    for path in paths:
        doc = json.loads(path.read_text(encoding="utf-8"))
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
    return xs, ys, batches, by_batch


def ridge_solve(x: np.ndarray, y: np.ndarray, lam: float) -> np.ndarray:
    """(X^T X + lam*I) w = X^T y — plain solve, deterministic at this size."""
    a = x.T @ x + lam * np.eye(x.shape[1])
    return np.linalg.solve(a, x.T @ y)


def quantize(w: np.ndarray) -> list[int]:
    """Float direction -> integer weights at DEFAULT L1 scale, EXACTLY.

    1. rescale magnitudes to sum == TARGET_L1 (same direction, DEFAULT
       magnitude);
    2. floor each magnitude; the shortfall (TARGET_L1 - sum of floors) is
       allocated as +1 magnitude bumps to the components with the LARGEST
       FRACTIONAL RESIDUALS (ties -> earlier in SCORE_COMPONENTS order),
       each bump applied in the component's SIGN direction —
       largest-remainder allocation (Codex M20c C7: a largest-|w| lump
       distorts direction);
    3. assert sum|w| == TARGET_L1 exactly; a miss refuses.
    """
    l1 = float(np.abs(w).sum())
    if l1 <= 0:
        raise SystemExit("error: fitted weight vector is all-zero — "
                         "refusing to quantize a degenerate fit")
    mags = np.abs(w) * (TARGET_L1 / l1)
    floors = np.floor(mags)
    resid = mags - floors  # each in [0, 1); sums to the shortfall
    shortfall = TARGET_L1 - int(floors.sum())
    order = sorted(range(len(w)), key=lambda i: (-resid[i], i))
    ints: list[int] = []
    for i in range(len(w)):
        mag = int(floors[i]) + (1 if i in order[:shortfall] else 0)
        ints.append(mag if w[i] >= 0 else -mag)
    if sum(abs(v) for v in ints) != TARGET_L1:
        raise SystemExit(f"error: quantization pinned sum|w|="
                         f"{sum(abs(v) for v in ints)} != {TARGET_L1}")
    return ints


def fit(paths: list[Path], index_paths: list[Path]) -> tuple[
        dict[str, Any], dict[str, Any]]:
    """The whole deterministic pipeline: rows -> lambda selection on the
    train/val split -> REFIT ON ALL ROWS at the selected lambda ->
    quantize -> (artifact doc, human report). The artifact is ints only,
    canonical-ready; the report carries floats for stdout only."""
    xs, ys, batches, by_batch = load_rows(paths)
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

    best_lam, best_mse = None, None
    for lam in LAMBDA_GRID:  # ascending: a strict < keeps the smaller lambda
        w = ridge_solve(x[train_rows], y[train_rows], lam)
        resid = x[val_rows] @ w - y[val_rows]
        mse = float((resid @ resid) / int(val_rows.sum()))
        if best_mse is None or mse < best_mse:
            best_lam, best_mse = lam, mse

    # the SHIPPED weights are refit on ALL rows at the selected lambda
    # (Codex M20c C8) — selection stays on the split, the fit states it
    final_w = ridge_solve(x, y, best_lam)
    weights = quantize(final_w)

    # corpus binding (Codex M20c C1): the VERIFIED byte hash of every
    # labels doc, with its run dir and batch role — the DATA, not just
    # each doc's source block
    manifest = sorted(
        ({"batch": batch_of(p), "run": str(p.parent),
          "sha256": _sha256_file(p)} for p in paths),
        key=lambda entry: (entry["run"], entry["batch"], entry["sha256"]))
    doc_batches = [batch_of(p) for p in paths]
    artifact = {
        "schema": SCHEMA,
        "weights": dict(zip(SCORE_COMPONENTS, weights, strict=True)),
        "uct_c_note": UCT_C_NOTE,
        "fit": {
            "rows": len(xs),
            "lambda": int(round(best_lam * LAMBDA_FIXED)),
            "val_mse_fixed": int(round(best_mse * MSE_FIXED)),
            "refit": "all_rows_at_selected_lambda",
            "split": {
                "train_docs": sum(b in TRAIN_BATCHES for b in doc_batches),
                "val_docs": sum(b in VAL_BATCHES for b in doc_batches),
                "train_rows": int(train_rows.sum()),
                "val_rows": int(val_rows.sum()),
            },
            "corpus": {
                "docs": len(paths),
                "sha256": sha256_hex(canonical(manifest)),
                "indexes": [{"path": str(i), "sha256": _sha256_file(i)}
                            for i in index_paths],
            },
            "scale_norm": "l1_161",
        },
    }
    report = {"by_batch": by_batch, "best_lam": best_lam, "best_mse": best_mse,
              "float_weights": [float(v) for v in final_w]}
    return artifact, report


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="civ-arena-fit-weights",
        description="Ridge-fit the planner value-head weights over M19a "
                    "label docs; write the quantized integer artifact.")
    ap.add_argument("--labels-glob", required=True, metavar="GLOB",
                    help="label docs to fit, e.g. "
                         "'runs/exp3/b*/planner-*/labels.json'")
    ap.add_argument("--index", action="append", type=Path, default=[],
                    metavar="PATH", required=True,
                    help="corpus index binding the label docs to their runs "
                         "(repeatable, REQUIRED — the casebase miner's "
                         "provenance discipline): every matched labels.json "
                         "must appear with a matching labels_sha256")
    ap.add_argument("--out", required=True, type=Path, metavar="PATH",
                    help="artifact path to write (must not alias an input)")
    opts = ap.parse_args(argv)

    paths = [Path(p) for p in sorted(_glob.glob(opts.labels_glob))]
    if not paths:
        raise SystemExit(f"error: no label docs under {opts.labels_glob}")
    for index in opts.index:
        if not index.is_file():
            raise SystemExit(f"error: --index {index} does not exist")
    # Codex M20c C8: only known batch roles — an unknown directory shape
    # means the split accounting would silently misattribute rows
    unknown = sorted({batch_of(p) for p in paths} - set(ALLOWED_BATCHES))
    if unknown:
        raise SystemExit(f"error: labels docs from unknown batch role(s) "
                         f"{unknown} — expected {list(ALLOWED_BATCHES)}")
    # guards before ANY write (Codex M20c C5): --out must not clobber any
    # --index file or ANY protected run artifact beside a matched doc
    # (events.jsonl/summary.json/planner/trace.json/labels.json — the
    # labels.py PROTECTED_ARTIFACTS, via the casebase helper)
    _assert_out_safe(opts.out, paths, opts.index)
    # provenance before ANY write (Codex M20c C1): every doc index-covered
    # with a matching byte digest
    _verify_provenance(paths, _load_index_entries(opts.index))

    artifact, report = fit(paths, opts.index)
    atomic_write_text(opts.out, canonical(artifact) + "\n")

    w = artifact["weights"]
    split = artifact["fit"]["split"]
    print(f"docs={artifact['fit']['corpus']['docs']} "
          f"rows={artifact['fit']['rows']} per-batch={report['by_batch']} "
          f"split={split}")
    print(f"lambda={report['best_lam']} "
          f"(fixed {artifact['fit']['lambda']}) "
          f"val_mse={report['best_mse']:.3f} "
          f"(fixed {artifact['fit']['val_mse_fixed']}) "
          f"refit={artifact['fit']['refit']}")
    print("float weights (all-rows refit, pre-quantization): "
          + " ".join(f"{k}={v:.4f}" for k, v
                     in zip(SCORE_COMPONENTS, report["float_weights"],
                            strict=True)))
    print(f"quantized weights: {w} sum|w|={sum(abs(v) for v in w.values())} "
          f"(target {TARGET_L1}, exact)")
    print(f"corpus manifest sha256={artifact['fit']['corpus']['sha256']}")
    print(f"wrote {opts.out}")


if __name__ == "__main__":
    main()
