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
    """Torn-tail tolerant JSONL load (the report.py discipline: a crash mid-
    write leaves a partial final line, which is not a record)."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            break
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="civ-arena-graph-project")
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--load", action="store_true",
                    help="also MERGE the artifacts into Neo4j (needs the "
                         "optional graph dependency group + a running DB)")
    opts = ap.parse_args(argv)

    records = load_records(opts.run_dir / "events.jsonl")
    if not records:
        raise SystemExit(f"error: no event records under {opts.run_dir}")
    try:
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
