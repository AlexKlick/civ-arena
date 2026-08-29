"""Graph artifacts: the projection's on-disk form — the portable truth.

``runs/<match_id>/graph/`` holds ``nodes.jsonl`` + ``edges.jsonl`` (one
canonical doc per line, same serialization discipline as the event log) and
``projection.json`` (the report). Artifacts are a pure function of the event
log, so any graph DB loaded from them is a derived, rebuildable index: drop
the DB, re-project, re-load.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from civ_arena.graph.projection import Projection


def _line(doc: dict[str, Any]) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n"


def graph_dir(run_dir: Path) -> Path:
    return Path(run_dir) / "graph"


def write_artifacts(run_dir: Path, proj: Projection) -> dict[str, Path]:
    """Write nodes/edges/report under ``<run_dir>/graph/``. Returns paths."""
    gdir = graph_dir(run_dir)
    gdir.mkdir(parents=True, exist_ok=True)
    paths = {
        "nodes": gdir / "nodes.jsonl",
        "edges": gdir / "edges.jsonl",
        "report": gdir / "projection.json",
    }
    paths["nodes"].write_text("".join(_line(n) for n in proj.nodes))
    paths["edges"].write_text("".join(_line(e) for e in proj.edges))
    paths["report"].write_text(
        json.dumps(proj.report, sort_keys=True, indent=2) + "\n")
    return paths


def read_lines(path: Path) -> list[dict[str, Any]]:
    """Load canonical JSONL lines (empty file -> empty list)."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def load_artifacts(run_dir: Path) -> tuple[list[dict[str, Any]],
                                           list[dict[str, Any]]]:
    """Read back what write_artifacts wrote."""
    gdir = graph_dir(run_dir)
    return read_lines(gdir / "nodes.jsonl"), read_lines(gdir / "edges.jsonl")
