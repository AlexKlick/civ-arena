"""Cross-match query CLI over the loaded graph.

    uv run --group graph python -m civ_arena.graph.query lessons --agent minimax-m3
    uv run --group graph python -m civ_arena.graph.query goal-outcomes --agent minimax-m3
    uv run --group graph python -m civ_arena.graph.query chains --match <id> --player 0 --claim g1
    uv run --group graph python -m civ_arena.graph.query observed --match <id> --player 0
    uv run --group graph python -m civ_arena.graph.query matches
    uv run --group graph python -m civ_arena.graph.query stats

Query functions take any session-like object whose ``run(cypher, **params)``
iterates mappings, so they stay unit-testable without a DB. The cross-match
join is the ``cross_match`` spine: ``(:Agent)-[:PLAYED_AS]->(:Player)`` and
``(:Agent)-[:PLAYED_IN]->(:Match)``; match-scoped rows carry the player's
``group_id`` ("{match_id}:main") and derive the match id from it.
"""

from __future__ import annotations

import argparse
import json
import warnings
from typing import Any

from civ_arena.graph.load import LoadError, config_from_env

GROUP_SUFFIX = ":main"


def connect() -> Any:
    """A neo4j driver from CIV_ARENA_NEO4J_* env (see load.py)."""
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise LoadError(
            "the neo4j driver is not installed — "
            "`uv sync --group graph` first") from exc
    cfg = config_from_env()
    auth = (cfg["user"], cfg["password"]) if cfg["password"] else None
    driver = GraphDatabase.driver(cfg["uri"], auth=auth)
    try:
        driver.verify_connectivity()
    except Exception as exc:
        driver.close()
        raise LoadError(
            f"cannot reach Neo4j at {cfg['uri']} — is the container up? "
            "(docker compose -f infra/neo4j/docker-compose.yaml up -d)"
        ) from exc
    return driver


def match_of(group_id: str) -> str:
    """'{match_id}:main' -> match_id"""
    return group_id[: -len(GROUP_SUFFIX)] if group_id.endswith(GROUP_SUFFIX) \
        else group_id


def _rows(session: Any, cypher: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(rec) for rec in session.run(cypher, **params)]


# -------------------------------------------------------------- the queries


def lessons(session: Any, agent: str) -> list[dict[str, Any]]:
    """Every lesson an agent authored, across matches, with the arena verdict
    on the claim it is about when one exists (verdicts also live in the
    lesson text itself — the model writes 'g6 MET: ...')."""
    return _rows(session, """
        MATCH (:Agent {agent_id: $agent})-[:PLAYED_AS]->(p:Player)
              -[:AUTHORED]->(l:Claim:Lesson)
        OPTIONAL MATCH (l)-[:REFERENCES]->(c:Claim)-[:VERDICT]->(o:Outcome)
        RETURN p.group_id AS group_id, l.claim_id AS lesson_id,
               l.text AS text, l.about AS about,
               l.created_turn AS turn, c.claim_id AS about_id,
               o.verdict AS about_verdict
        ORDER BY group_id, turn, lesson_id
    """, agent=agent)


def goal_outcomes(session: Any, agent: str,
                  metric: str | None = None) -> list[dict[str, Any]]:
    """Every goal revision an agent authored with its outcome (arena verdict
    or terminal status); CURRENT revisions only — the full revision chains
    live in ``chains``."""
    cypher = """
        MATCH (:Agent {agent_id: $agent})-[:PLAYED_AS]->(p:Player)
              -[:AUTHORED]->(g:Claim:Goal {current: true})
        OPTIONAL MATCH (g)-[:VERDICT]->(o:Outcome)
        RETURN p.group_id AS group_id, g.claim_id AS claim_id,
               g.text AS text, g.by_turn AS by_turn, g.status AS status,
               g.metric AS metric, g.target AS target,
               o.verdict AS verdict, o.value AS value
        ORDER BY group_id, claim_id
    """
    rows = _rows(session, cypher, agent=agent)
    if metric is not None:
        rows = [r for r in rows if r.get("metric") == metric]
    return rows


def tally(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Verdict tallies per metric (pure — unit-testable). A row with no
    outcome tallies under its terminal status or 'unscored'."""
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = row.get("metric") or "(none)"
        verdict = row.get("verdict") or row.get("status") or "unscored"
        counts = out.setdefault(bucket, {})
        counts[verdict] = counts.get(verdict, 0) + 1
    return out


def chains(session: Any, match_id: str, player: int,
           claim: str) -> list[dict[str, Any]]:
    """The full revision chain of one claim (SUPERSEDES is derivable from the
    ordered revisions; both are in the graph)."""
    prefix = f"{match_id}:main:claim:p{player}:{claim}:"
    return _rows(session, """
        MATCH (c:Claim)
        WHERE c.uuid STARTS WITH $prefix
        RETURN c.uuid AS uuid, c.claim_id AS claim_id, c.revision AS revision,
               c.current AS current, c.text AS text,
               c.valid_from_turn AS from_turn,
               c.valid_to_turn AS to_turn, c.created_turn AS created_turn
        ORDER BY c.revision
    """, prefix=prefix)


def observed(session: Any, match_id: str, player: int) -> list[dict[str, Any]]:
    """One player's last-known foreign entities in a match, newest first."""
    puuid = f"{match_id}:main:player:p{player}"
    return _rows(session, """
        MATCH (p:GraphNode {uuid: $puid})-[o:OBSERVED]->(e:Entity)
        RETURN e.entity_id AS entity_id, e.kind AS kind,
               o.last_seen_turn AS turn, o.last_seen_seq AS seq,
               o.fields_json AS fields_json
        ORDER BY turn DESC, seq DESC
    """, puid=puuid)


def matches(session: Any) -> list[dict[str, Any]]:
    return _rows(session, """
        MATCH (m:Match)
        OPTIONAL MATCH (a:Agent)-[:PLAYED_IN]->(m)
        RETURN m.match_id AS match_id, m.seed AS seed,
               m.final_turn AS final_turn, m.aborted AS aborted,
               m.scores_json AS scores_json,
               collect(a.agent_id) AS agents
        ORDER BY match_id
    """)


def stats(session: Any) -> dict[str, Any]:
    nodes = _rows(session, """
        MATCH (n) RETURN labels(n) AS labels, count(*) AS n ORDER BY labels
    """)
    rels = _rows(session, """
        MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS n ORDER BY rel
    """)
    return {
        "nodes": {"+".join(sorted(r["labels"])): r["n"] for r in nodes},
        "rels": {r["rel"]: r["n"] for r in rels},
    }


# -------------------------------------------------------------------- CLI


def _print_lessons(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        verdict = f" [{row['about_id']}:{row['about_verdict']}]" \
            if row.get("about_verdict") else ""
        print(f"{match_of(row['group_id'])} t{row['turn']} "
              f"{row['lesson_id']}: {row['text']!r}{verdict}")
    if not rows:
        print("(no lessons)")


def _print_goal_outcomes(rows: list[dict[str, Any]]) -> None:
    for metric, counts in sorted(tally(rows).items()):
        print(f"{metric}: {counts}")
    for row in rows:
        outcome = f"{row['verdict']}({row['value']})" if row.get("verdict") \
            else f"status={row['status']}"
        print(f"  {match_of(row['group_id'])} {row['claim_id']} "
              f"[{outcome}] {row['text']!r}")


def _print_chains(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        closed = f"-t{row['to_turn']}" if row["to_turn"] else "-open"
        marker = "*" if row["current"] else " "
        print(f"{marker} r{row['revision']} t{row['from_turn']}{closed} "
              f"t{row['created_turn']}: {row['text']!r}")
    if not rows:
        print("(no such claim)")


def _print_observed(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        fields = json.loads(row["fields_json"])
        what = fields.get("type") or fields.get("name") or "?"
        print(f"t{row['turn']} {row['entity_id']} {what} "
              f"@ {fields.get('coord', '?')}")
    if not rows:
        print("(nothing observed)")


def _print_matches(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        agents = ", ".join(sorted(a for a in row["agents"] if a))
        aborted = f" ABORTED({row['aborted']})" if row.get("aborted") else ""
        print(f"{row['match_id']} seed={row['seed']} "
              f"final_turn={row['final_turn']}{aborted} [{agents}]")
        scores = json.loads(row.get("scores_json") or "{}")
        for civ, doc in sorted(scores.items()):
            print(f"  {civ}: {json.dumps(doc, sort_keys=True)}")


def main(argv: list[str] | None = None) -> None:
    # the driver surfaces missing-label/property DBMS notifications as
    # warnings (e.g. no Outcome nodes exist yet when nothing was scored);
    # advisory noise for a read-only CLI — the queries still answer.
    warnings.filterwarnings("ignore", message="Received notification")
    ap = argparse.ArgumentParser(prog="civ-arena-graph-query")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("lessons", help="an agent's lessons across matches")
    p.add_argument("--agent", required=True)

    p = sub.add_parser("goal-outcomes", help="verdict tallies for an agent's goals")
    p.add_argument("--agent", required=True)
    p.add_argument("--metric", default=None)

    p = sub.add_parser("chains", help="one claim's full revision chain")
    p.add_argument("--match", required=True)
    p.add_argument("--player", type=int, required=True)
    p.add_argument("--claim", required=True)

    p = sub.add_parser("observed", help="a player's last-known entities")
    p.add_argument("--match", required=True)
    p.add_argument("--player", type=int, required=True)

    sub.add_parser("matches", help="all matches with scores")
    sub.add_parser("stats", help="node/relationship counts")

    opts = ap.parse_args(argv)
    driver = connect()
    try:
        with driver.session() as session:
            if opts.cmd == "lessons":
                _print_lessons(lessons(session, opts.agent))
            elif opts.cmd == "goal-outcomes":
                _print_goal_outcomes(
                    goal_outcomes(session, opts.agent, opts.metric))
            elif opts.cmd == "chains":
                _print_chains(
                    chains(session, opts.match, opts.player, opts.claim))
            elif opts.cmd == "observed":
                _print_observed(
                    observed(session, opts.match, opts.player))
            elif opts.cmd == "matches":
                _print_matches(matches(session))
            else:
                print(json.dumps(stats(session), indent=2, sort_keys=True))
    finally:
        driver.close()


if __name__ == "__main__":
    main()
