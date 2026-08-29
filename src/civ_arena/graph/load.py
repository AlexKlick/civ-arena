"""Neo4j loader: MERGE graph artifacts into a live graph.

The DB is a DERIVED index over the artifacts (which are a pure function of
the event log): loading is idempotent — MERGE by ``uuid`` — so re-loading
identical artifacts is a no-op and dropping the DB + re-projecting is always
safe. This is the only module that touches the neo4j driver; the driver lives
in the optional ``graph`` dependency group and the core stays importable
(and testable) without it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from civ_arena.graph.artifacts import load_artifacts

BATCH = 500
_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

# structural keys stay OUT of the property map — but group_id/name DO load
# as properties (queries filter on them); only uuid/labels/rel identify
_NODE_STRUCTURAL = ("uuid", "labels")
_EDGE_STRUCTURAL = ("uuid", "source", "target", "rel")


class LoadError(RuntimeError):
    """Loader precondition failed (driver missing, DB unreachable, no
    artifacts). Carries the remedy in its message."""


def config_from_env() -> dict[str, str]:
    return {
        "uri": os.environ.get("CIV_ARENA_NEO4J_URI", "bolt://127.0.0.1:7687"),
        "user": os.environ.get("CIV_ARENA_NEO4J_USER", "neo4j"),
        "password": os.environ.get("CIV_ARENA_NEO4J_PASSWORD", ""),
    }


def _token(kind: str, value: Any) -> str:
    """Labels and relationship types are interpolated into cypher — they come
    from our own artifacts, but validating them costs one regex and makes the
    injection surface explicitly zero."""
    if not isinstance(value, str) or not _TOKEN_RE.match(value):
        raise LoadError(f"unsafe {kind} {value!r}: not a bare identifier")
    return value


def _props(doc: dict[str, Any], structural: tuple[str, ...]) -> dict[str, Any]:
    # None is not a storable Neo4j property value; artifacts avoid it, the
    # loader enforces it
    return {k: v for k, v in doc.items()
            if k not in structural and v is not None}


def node_batches(
    nodes: list[dict[str, Any]], batch: int = BATCH,
) -> Iterator[tuple[tuple[str, ...], list[dict[str, Any]]]]:
    """Group nodes by label tuple (one query per label set), chunked."""
    by_labels: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for node in nodes:
        labels = tuple(_token("label", lab) for lab in node.get("labels") or ())
        by_labels.setdefault(labels, []).append({
            "uuid": node["uuid"],
            "props": _props(node, _NODE_STRUCTURAL),
        })
    for labels, rows in sorted(by_labels.items()):
        for i in range(0, len(rows), batch):
            yield labels, rows[i:i + batch]


def edge_batches(
    edges: list[dict[str, Any]], batch: int = BATCH,
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Group edges by relationship type (one query per type), chunked."""
    by_rel: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        rel = _token("relationship type", edge.get("rel"))
        by_rel.setdefault(rel, []).append({
            "uuid": edge["uuid"], "source": edge["source"],
            "target": edge["target"],
            "props": _props(edge, _EDGE_STRUCTURAL),
        })
    for rel, rows in sorted(by_rel.items()):
        for i in range(0, len(rows), batch):
            yield rel, rows[i:i + batch]


def node_query(labels: tuple[str, ...]) -> str:
    label_sets = "".join(f" SET n :{lab}" for lab in labels)
    return ("UNWIND $rows AS row "
            "MERGE (n:GraphNode {uuid: row.uuid}) "
            "SET n += row.props" + label_sets)


def edge_query(rel: str) -> str:
    return ("UNWIND $rows AS row "
            "MERGE (s:GraphNode {uuid: row.source}) "
            "MERGE (t:GraphNode {uuid: row.target}) "
            f"MERGE (s)-[e:{_token('relationship type', rel)} "
            "{uuid: row.uuid}]->(t) "
            "SET e += row.props")


def load_into_db(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]], *,
    uri: str | None = None, user: str | None = None,
    password: str | None = None,
) -> dict[str, int]:
    """MERGE artifacts into Neo4j. Nodes first (edges MERGE their endpoints
    and would otherwise create bare stubs). Returns write counts."""
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise LoadError(
            "the neo4j driver is not installed — "
            "`uv sync --group graph` first") from exc

    cfg = config_from_env()
    uri = uri or cfg["uri"]
    user = user or cfg["user"]
    password = cfg["password"] if password is None else password
    auth = (user, password) if password else None  # NEO4J_AUTH=none container

    driver = GraphDatabase.driver(uri, auth=auth)
    try:
        driver.verify_connectivity()
    except Exception as exc:
        driver.close()
        raise LoadError(
            f"cannot reach Neo4j at {uri} — is the container up? "
            "(docker compose -f infra/neo4j/docker-compose.yaml up -d)"
        ) from exc

    batches = 0
    try:
        with driver.session() as session:
            def write(query: str, rows: list[dict[str, Any]]) -> None:
                session.execute_write(
                    lambda tx: tx.run(query, rows=rows).consume())
            for labels, rows in node_batches(nodes):
                write(node_query(labels), rows)
                batches += 1
            for rel, rows in edge_batches(edges):
                write(edge_query(rel), rows)
                batches += 1
    finally:
        driver.close()
    return {"nodes": len(nodes), "edges": len(edges), "batches": batches}


def load_artifacts_into_db(run_dir: Path, **kwargs: Any) -> dict[str, int]:
    nodes, edges = load_artifacts(run_dir)
    if not nodes:
        raise LoadError(
            f"no graph artifacts under {run_dir}/graph — "
            "run `python -m civ_arena.graph.project` first")
    return load_into_db(nodes, edges, **kwargs)
