"""Project CLI: a run's event log -> graph artifacts.

    uv run python -m civ_arena.graph.project runs/<match_id> [--load]

Offline and deterministic — no DB needed to produce the artifacts. ``--load``
(M12b) additionally MERGEs them into the configured Neo4j.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from civ_arena.graph.artifacts import write_artifacts
from civ_arena.graph.projection import project


def load_records(path: Path) -> list[dict]:
    """The EventLog._load discipline (arena/events.py): only the FINAL line
    may be torn (a crash mid-write leaves a partial line, which is not a
    record); mid-file corruption and a stored seq that does not equal its
    position fail closed. The projector feeds a graph — a silent truncation
    to a valid-looking prefix would be worse than an error."""
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").splitlines()
    out: list[dict] = []
    for i, line in enumerate(raw):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            if i == len(raw) - 1:
                return out  # torn tail from a crash mid-write
            raise ValueError(
                f"corrupt event log line {i}: {line[:80]!r}") from None
        if not isinstance(rec, dict) or rec.get("seq") != len(out):
            raise ValueError(
                f"event log seq broken at line {i}: expected {len(out)}, "
                f"got {rec.get('seq') if isinstance(rec, dict) else type(rec)}"
            )
        out.append(rec)
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="civ-arena-graph-project")
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--load", action="store_true",
                    help="also MERGE the artifacts into Neo4j (needs the "
                         "optional graph dependency group + a running DB)")
    opts = ap.parse_args(argv)

    try:
        records = load_records(opts.run_dir / "events.jsonl")
        if not records:
            raise SystemExit(f"error: no event records under {opts.run_dir}")
        proj = project(records)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc

    paths = write_artifacts(opts.run_dir, proj)
    rep = proj.report
    print(f"match {rep['match_id']} | final turn {rep['final_turn']} "
          f"| {rep['source_records']} records | "
          f"claims {rep['claims']}")
    print(f"nodes {rep['nodes']['total']}: {rep['nodes']['by_label']}")
    print(f"edges {rep['edges']['total']}: {rep['edges']['by_rel']}")
    if rep["dropped_references"]:
        print(f"dropped references ({len(rep['dropped_references'])}):")
        for entry in rep["dropped_references"][:10]:
            print(f"  - {entry}")
    if rep["entity_kind_conflicts"]:
        print(f"entity kind conflicts: {rep['entity_kind_conflicts']}")
    print("wrote " + ", ".join(str(p) for p in paths.values()))

    if opts.load:
        from civ_arena.graph.load import LoadError, load_artifacts_into_db
        try:
            counts = load_artifacts_into_db(opts.run_dir)
        except LoadError as exc:
            raise SystemExit(f"error: {exc}") from exc
        print(f"loaded into Neo4j: {counts}")


if __name__ == "__main__":
    main()
