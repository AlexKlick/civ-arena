"""Neo4j loader: MERGE graph artifacts into a live graph.

The DB is a DERIVED index over the artifacts (which are a pure function of
the event log): loading is idempotent — MERGE by ``uuid`` — and it
RECONCILES: match-scoped nodes and edges absent from the artifacts are
deleted, replaced properties clear, stale labels removed, so re-loading a
changed projection (a resumed match's longer log) leaves no residue. The
whole load is ONE transaction: a failure mid-load commits nothing. The
spine (``cross_match`` group) is never reconciled away — agent and match
nodes are shared across loads.

This is the only module that touches the neo4j driver; the driver lives in
the optional ``graph`` dependency group and the core stays importable (and
testable) without it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from civ_arena.graph.artifacts import load_artifacts

BATCH = 500
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")  # fullmatch-anchored below

# every label the projection emits — the loader removes any of these that a
# re-loaded node should no longer carry (an entity whose settled kind
# changed, e.g. Unit -> City). GraphNode (the MERGE anchor) is deliberately
# NOT here: removing it would orphan every node from future MERGEs and mint
# duplicates — the live DB test caught exactly that.
LABEL_UNIVERSE = (
    "Agent", "Match", "Player", "Claim", "Goal", "Prediction", "Lesson",
    "Entity", "Unit", "City", "Outcome",
)

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
    injection surface explicitly zero. fullmatch, not ``$``: Python's ``$``
    also matches before a trailing newline."""
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise LoadError(f"unsafe {kind} {value!r}: not a bare identifier")
    return value


def _props(doc: dict[str, Any], structural: tuple[str, ...]) -> dict[str, Any]:
    # None is not a storable Neo4j property value; artifacts avoid it, the
    # loader enforces it. uuid rides along so the replacing SET keeps the
    # MERGE key alive.
    props = {k: v for k, v in doc.items()
             if k not in structural and v is not None}
    props["uuid"] = doc["uuid"]
    return props


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
    # SET n = props REPLACES the property map (a re-loaded projection fully
    # defines the node — omitted props clear); stale labels from the
    # universe are removed so kind changes reconcile
    label_sets = "".join(f" SET n :{lab}" for lab in labels)
    stale = "".join(f" REMOVE n:{lab}"
                    for lab in LABEL_UNIVERSE if lab not in labels)
    return ("UNWIND $rows AS row "
            "MERGE (n:GraphNode {uuid: row.uuid}) "
            "SET n = row.props" + label_sets + stale)


def edge_query(rel: str) -> str:
    return ("UNWIND $rows AS row "
            "MERGE (s:GraphNode {uuid: row.source}) "
            "MERGE (t:GraphNode {uuid: row.target}) "
            f"MERGE (s)-[e:{_token('relationship type', rel)} "
            "{uuid: row.uuid}]->(t) "
            "SET e = row.props")


def legacy_edge_cleanup_query() -> str:
    """Pre-rework loads wrote pipe-join edge uuids with no group_id; those
    rows are invisible to reconciliation and would silently duplicate every
    relationship on the next load. The sweep is ENDPOINT-scoped: it removes
    pipe-uuid edges touching THIS match's nodes — including ghosts whose
    triple no longer exists in the artifacts — and nothing else. Endpoint
    scoping, not uuid reconstruction: pipe uuids were not injective, so a
    reconstructed uuid can equal another match's legacy edge."""
    return ("MATCH (a)-[e]->(b) WHERE e.uuid CONTAINS '|' "
            "AND (a.group_id = $group OR b.group_id = $group "
            "OR a.uuid = $match_node OR b.uuid = $match_node) DELETE e")


def reconcile_nodes_query() -> str:
    """Delete match-scoped nodes absent from the artifacts (DETACH removes
    their edges). The cross_match spine carries a different group_id and is
    never touched, so reconciling one match cannot disturb another."""
    return ("MATCH (n) WHERE n.group_id = $group "
            "AND NOT n.uuid IN $keep_nodes DETACH DELETE n")


def reconcile_edges_query() -> str:
    """Delete match-scoped edges absent from the artifacts — the projection
    stamps every edge with its group_id for exactly this scope."""
    return ("MATCH ()-[e]->() WHERE e.group_id = $group "
            "AND NOT e.uuid IN $keep_edges DELETE e")


def _match_group(nodes: list[dict[str, Any]]) -> str | None:
    groups = {n.get("group_id") for n in nodes
              if isinstance(n.get("group_id"), str)
              and n.get("group_id") != "cross_match"}
    return sorted(groups)[0] if groups else None


def load_plan(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
    batch: int = BATCH,
) -> list[tuple[str, dict[str, Any]]]:
    """Fully validate and materialize the write plan BEFORE any write: a
    token problem must never surface after node batches have committed."""
    plan: list[tuple[str, dict[str, Any]]] = [
        *[(node_query(labels), {"rows": rows})
          for labels, rows in node_batches(nodes, batch)],
        *[(edge_query(rel), {"rows": rows})
          for rel, rows in edge_batches(edges, batch)],
    ]
    group = _match_group(nodes)
    if group is not None:
        match_node = "match:" + group[: -len(":main")]
        keep = {
            "keep_nodes": [n["uuid"] for n in nodes],
            "keep_edges": [e["uuid"] for e in edges],
            "group": group,
        }
        plan.insert(0, (legacy_edge_cleanup_query(),
                        {"group": group, "match_node": match_node}))
        plan.append((reconcile_nodes_query(), dict(keep)))
        plan.append((reconcile_edges_query(), dict(keep)))
    return plan


def _execute(session: Any, plan: list[tuple[str, dict[str, Any]]]) -> None:
    def work(tx: Any) -> None:
        for query, params in plan:
            tx.run(query, **params).consume()
    session.execute_write(work)


def load_into_db(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]], *,
    uri: str | None = None, user: str | None = None,
    password: str | None = None, session: Any = None,
) -> dict[str, int]:
    """MERGE artifacts into Neo4j as ONE transaction (nodes, then edges,
    then reconciliation). ``session`` injection exists for tests; production
    callers get a driver from env config."""
    plan = load_plan(nodes, edges)
    if session is not None:
        _execute(session, plan)
        return {"nodes": len(nodes), "edges": len(edges),
                "queries": len(plan)}

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
    try:
        with driver.session() as live:
            _execute(live, plan)
    finally:
        driver.close()
    return {"nodes": len(nodes), "edges": len(edges), "queries": len(plan)}


def load_artifacts_into_db(run_dir: Path, **kwargs: Any) -> dict[str, int]:
    nodes, edges = load_artifacts(run_dir)
    if not nodes:
        raise LoadError(
            f"no graph artifacts under {run_dir}/graph — "
            "run `python -m civ_arena.graph.project` first")
    return load_into_db(nodes, edges, **kwargs)
