"""Outcome-label CLI: one run directory -> one canonical labels.json.

Read-only over the run's own artifacts (events.jsonl, summary.json, the
optional planner/trace.json — opened read-only, never rewritten) and
writes exactly one side artifact beside them: ``runs/<id>/labels.json``,
atomic tmp+replace like the journal. The doc freezes WHAT HAPPENED —
seats, final score vectors, per-seat value differentials and outcome
signs, the planner's per-decision search trace annotated with the match
result, and every rebuilt claim with its scoring-module verdict — so
downstream analysis (option mining, outcome-conditioned evaluation)
never re-derives state. Ints/strings/lists/None only: canonical JSON,
no floats ever; the write itself refuses a float-bearing doc.

Claim verdicts are DERIVED through strategy.scoring on a store rebuilt
by StrategyStore.from_log — zero re-implementation of the scoring rules
(the same discipline the in-match memory view uses).

    uv run python -m civ_arena.labels runs/<match_id>
    uv run python -m civ_arena.labels --corpus 'runs/exp3/b8/planner-*' \
        --index-out runs/exp3/b8/labels-index.json
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from civ_arena.canonical import canonical
from civ_arena.game.sim.value import DEFAULT_WEIGHTS, score_differential
from civ_arena.graph.project import load_records
from civ_arena.strategy import scoring
from civ_arena.strategy.store import StrategyStore

SCHEMA = 1


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seats(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Seat triples from the run's MATCH_START config.agents — the ONLY
    seats source (the summary carries none). Fail closed on a malformed
    roster: silently dropping an entry would mis-attribute that seat's
    claims and decisions (the recall._roster discipline)."""
    for rec in records:
        if rec.get("kind") != "MATCH_START":
            continue
        config = rec.get("config") if isinstance(rec.get("config"), dict) else {}
        entries = config.get("agents") or []
        seats: list[dict[str, Any]] = []
        for entry in entries:
            if (not isinstance(entry, list) or len(entry) != 3
                    or not isinstance(entry[0], str) or not entry[0]
                    or not isinstance(entry[1], int)
                    or isinstance(entry[1], bool)
                    or not isinstance(entry[2], str)):
                raise ValueError(
                    f"malformed MATCH_START roster entry {entry!r} — refusing "
                    "to label with a possibly mis-attributed seat")
            seats.append({"agent_id": entry[0], "player_id": entry[1],
                          "policy": entry[2]})
        if not seats:
            raise ValueError("MATCH_START carries no agent roster")
        return seats
    raise ValueError("no MATCH_START record in the event log")


def _planner_seat(seats: list[dict[str, Any]]) -> int:
    planner = [s["player_id"] for s in seats if s["policy"] == "planner"]
    if len(planner) != 1:
        raise ValueError(
            f"planner/trace.json present but the MATCH_START roster carries "
            f"{len(planner)} planner-policy seats — a single-file trace is "
            "attributable to exactly one seat")
    return planner[0]


def _decisions(trace: list[Any], seat: int, diff: int, sign: int,
               ) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in trace:
        out.append({
            "seat": seat,
            "turn": entry["turn"],
            "root_key": entry["root_key"],
            "candidates": list(entry["candidates"]),
            "chosen": entry["chosen"],
            "prior": list(entry.get("prior", [])),  # older traces predate it
            "root_visits": dict(entry["root_visits"]),
            "root_values": dict(entry["root_values"]),
            "method": entry["method"],
            "budget": entry["budget"],
            "match_value_differential": diff,
            "outcome_sign": sign,
        })
    return out


def _claims(store: StrategyStore, seats: list[dict[str, Any]],
            final_turn: int) -> list[dict[str, Any]]:
    """One row per claim id (current revision), verdicts derived through
    strategy.scoring over the rebuilt store's folded facts. Lessons carry
    no verdict by contract. Observed is the metric sample at the same
    as-of turn the verdict used — None when there is no metric or no
    sample (the fields ride verbatim; canonical() rejects a float)."""
    rows: list[dict[str, Any]] = []
    for seat in seats:
        pid = seat["player_id"]
        for goal in store.current_goals(pid):
            rows.append(_scored_row(goal, "goal", pid, store, final_turn))
        for pred in store.current_predictions(pid):
            rows.append(_scored_row(pred, "prediction", pid, store,
                                    final_turn))
        for lesson in store.lesson_list(pid):
            rows.append({"seat": pid, "claim_id": lesson.lesson_id,
                         "kind": "lesson", "metric": None, "target": None,
                         "deadline_turn": None, "verdict": None,
                         "observed": None})
    return rows


def _scored_row(claim: Any, kind: str, pid: int, store: StrategyStore,
                final_turn: int) -> dict[str, Any]:
    """One goal/prediction row: verdict + observed BOTH derived through
    strategy.scoring, evaluated as of min(final_turn, deadline) — the
    same as-of turn, so the row can never contradict itself. A claim
    with no deadline is judged as of the final turn."""
    verdict = scoring.verdict(claim, store.facts, pid, final_turn)
    deadline = scoring.deadline_turn(claim, final_turn)
    observed: int | str | None = None
    if claim.metric:
        as_of = min(final_turn, deadline) if deadline is not None \
            else final_turn
        observed = scoring.metric_value(store.facts, pid, claim.metric, as_of)
    claim_id = claim.goal_id if kind == "goal" else claim.prediction_id
    return {"seat": pid, "claim_id": claim_id, "kind": kind,
            "metric": claim.metric or None, "target": claim.target,
            "deadline_turn": deadline, "verdict": verdict,
            "observed": observed}


def label_run(run_dir: Path) -> dict[str, Any]:
    """Build the label doc for one run directory. A pure function of the
    run's own artifacts — labeling twice yields byte-identical output."""
    run_dir = Path(run_dir)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    match_id = str(summary["match_id"])
    records = [r for r in load_records(run_dir / "events.jsonl")
               if r.get("match_id") == match_id]
    seats = _seats(records)

    scores = summary["scores"]
    diffs = {str(s["player_id"]): score_differential(scores, s["player_id"])
             for s in seats}
    signs = {pid: (0 if d == 0 else 1 if d > 0 else -1)
             for pid, d in diffs.items()}
    final_turn = summary["final_turn"]

    trace_path = run_dir / "planner" / "trace.json"
    decisions: list[dict[str, Any]] = []
    if trace_path.exists():
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        if not isinstance(trace, list):
            raise ValueError("planner/trace.json is not a JSON array")
        seat = _planner_seat(seats)
        decisions = _decisions(trace, seat, diffs[str(seat)],
                               signs[str(seat)])

    store = StrategyStore.from_log(records)

    doc = {
        "schema": SCHEMA,
        "match_id": match_id,
        "source": {
            "events_sha256": _sha256_file(run_dir / "events.jsonl"),
            "summary_sha256": _sha256_file(run_dir / "summary.json"),
            "trace_sha256": _sha256_file(trace_path)
            if trace_path.exists() else None,
        },
        "weights": dict(DEFAULT_WEIGHTS),
        "outcome": {
            "final_turn": final_turn,
            "aborted": summary.get("aborted"),
            "violations_total": summary["violations_total"],
            "seats": seats,
            "score_vectors": {civ: dict(entry)
                              for civ, entry in scores.items()},
            "value_differential": diffs,
            "outcome_sign": signs,
        },
        "decisions": decisions,
        "claims": _claims(store, seats, final_turn),
    }
    return doc


def write_labels(run_dir: Path, doc: dict[str, Any]) -> None:
    """Atomic tmp+replace (the journal discipline). Canonical text, so a
    float anywhere in the doc refuses at write time, never on disk."""
    path = Path(run_dir) / "labels.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(canonical(doc) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_index(path: Path, index: dict[str, Any]) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(canonical(index) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="civ-arena-labels",
        description="Write the canonical outcome-label side artifact "
                    "labels.json for one run (or a corpus glob of runs).")
    ap.add_argument("run_dir", type=Path, nargs="?",
                    help="label this one run directory")
    ap.add_argument("--corpus", metavar="GLOB",
                    help="label every globbed directory that carries a "
                         "summary.json, e.g. 'runs/exp3/b8/planner-*'")
    ap.add_argument("--index-out", type=Path,
                    help="write a corpus index JSON mapping match_id -> "
                         "{labels_sha256, decisions, claims}")
    opts = ap.parse_args()
    if bool(opts.run_dir) == bool(opts.corpus):
        ap.error("give exactly one of run_dir or --corpus")

    if opts.corpus:
        run_dirs = [Path(p) for p in sorted(glob.glob(opts.corpus))
                    if (Path(p) / "summary.json").exists()]
        if not run_dirs:
            raise SystemExit(f"error: no run directories under {opts.corpus}")
    else:
        if not (opts.run_dir / "summary.json").exists():
            raise SystemExit(f"error: no summary.json under {opts.run_dir}")
        run_dirs = [opts.run_dir]

    index: dict[str, Any] = {}
    for run_dir in run_dirs:
        doc = label_run(run_dir)
        write_labels(run_dir, doc)
        labels_path = run_dir / "labels.json"
        index[doc["match_id"]] = {
            "labels_sha256": _sha256_file(labels_path),
            "decisions": len(doc["decisions"]),
            "claims": len(doc["claims"]),
        }
        print(f"{doc['match_id']}: decisions={len(doc['decisions'])} "
              f"claims={len(doc['claims'])} -> {labels_path}")

    if opts.index_out is not None:
        write_index(opts.index_out, index)
        print(f"wrote {opts.index_out} ({len(index)} entries)")


if __name__ == "__main__":
    main()
