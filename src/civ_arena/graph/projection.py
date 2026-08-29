"""Deterministic graph projection: the event log -> Graphiti-shaped nodes/edges.

M12's premise (docs/strategy-lane.md): the claims and observation digests on
the log are ALREADY structured, so the graph needs no LLM extraction —
``project(records)`` is a pure function of the log. Identical records produce
identical nodes/edges in sorted order, so the artifact files are the portable
truth and any graph DB loaded from them is a derived, rebuildable index.

Shape (Graphiti-compatible; refines the M11 sketch — claim nodes are
per-REVISION so SUPERSEDES connects distinct nodes):

- ``group_id`` is ``{match_id}:main`` for match-scoped nodes and
  ``cross_match`` for the agent/match spine that joins games (agent uuids are
  stable across matches — the cross-match join).
- nodes: claim revisions ``{match}:main:claim:p{pid}:{cid}:r{rev}``, entities
  ``{match}:main:entity:{eid}``, players ``...:player:p{pid}``, outcomes
  ``...:outcome:p{pid}:{cid}:r{rev}``, plus ``agent:{agent_id}`` and
  ``match:{match_id}`` in the spine.
- edges: AUTHORED (player -> revision), SUPERSEDES (rev n -> n-1),
  REFERENCES (prediction/lesson -> claim-or-entity via subject_id/about,
  resolved to the revision valid at the referencing claim's created_turn),
  OBSERVED (player -> entity with the last-seen snapshot), VERDICT (claim
  revision -> outcome, recomputed as-of the deadline at projection time),
  PLAYED_AS / PLAYED_IN (agent -> player / match).
- every prop is a primitive or a JSON-encoded string (Neo4j property-ready);
  turns are the only time axis — ``ts`` envelope fields are never read.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from civ_arena.strategy import scoring
from civ_arena.strategy.claims import Goal, Lesson, Prediction
from civ_arena.strategy.store import StrategyStore

CROSS_GROUP = "cross_match"

# match_id and agent_id are operator-chosen strings that land INSIDE the
# node uuid templates. The templates stay readable (the chains query matches
# by prefix) only if these ids cannot smuggle the spine namespaces into a
# match-scoped uuid — a match_id containing ':' could mint an outcome uuid
# identical to another match's agent uuid, and reconciliation would then
# delete spine data. The charset makes that structurally impossible; claim
# ids (g/p/l + digits) and player ids (ints) are store-generated and safe;
# entity ids sit behind a fixed "…:entity:" prefix, so they cannot cross
# namespaces either.
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

# structural keys (everything else on the doc is a graph property)
NODE_KEYS = ("uuid", "group_id", "labels", "name")
EDGE_KEYS = ("uuid", "source", "target", "rel")

_KIND_LABEL = {"goal": "Goal", "prediction": "Prediction", "lesson": "Lesson"}


@dataclass(frozen=True)
class Projection:
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    report: dict[str, Any]


def _props(doc: dict[str, Any]) -> dict[str, Any]:
    """The non-structural part of a node/edge doc (what a loader SETs)."""
    keys = NODE_KEYS if "labels" in doc else EDGE_KEYS
    return {k: v for k, v in doc.items() if k not in keys}


def _jdump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _edge_uuid(source: str, rel: str, target: str) -> str:
    """Edge uuid: ``e:<sha256-40>`` over the canonical [source, rel, target]
    tuple. Ids from belief digests are legal arbitrary strings and may
    contain '|' — a pipe-join could collide two DIFFERENT edges (found by
    adversarial review). 160 bits of SHA-256 is collision-resistant, not
    injective in the mathematical sense: a collision would surface as the
    same loud duplicate-uuid ValueError as a true duplicate."""
    payload = _jdump([source, rel, target]).encode("utf-8")
    return "e:" + hashlib.sha256(payload).hexdigest()[:40]


def _revision_at(history: list[Any], created_seq: int) -> Any:
    """The revision of one claim id that was the authority when the
    referencing claim was WRITTEN: the last revision authored at or before
    the referencing claim's ``created_seq``. Seq is totally ordered by log
    position, so intra-turn ordering resolves too (a lesson written after a
    same-turn amend points at the amended revision — turns alone cannot
    express that). A claim created later than the reference resolves to
    nothing: dropped and reported, never guessed."""
    authority = None
    for rec in history:
        if rec.created_seq <= created_seq:
            authority = rec
    return authority


def _meta(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Roster, horizon and scores from the log envelope records. Lifecycle
    records must agree on ONE match: concatenated or mismatched logs fail
    loudly rather than mixing one match's envelope with another's claims."""
    starts = [r for r in records if r.get("kind") == "MATCH_START"]
    if not starts or not isinstance(starts[0].get("match_id"), str):
        raise ValueError("no MATCH_START record — not a civ-arena event log")
    match_ids = {s.get("match_id") for s in starts}
    if len(match_ids) > 1:
        raise ValueError(f"multiple MATCH_START records disagree on "
                         f"match_id: {sorted(str(m) for m in match_ids)}")
    start = starts[0]
    config = start.get("config") if isinstance(start.get("config"), dict) else {}
    agents = [
        (entry[0], entry[1], entry[2])
        for entry in (config.get("agents") or [])
        if isinstance(entry, list) and len(entry) == 3
        and isinstance(entry[0], str) and isinstance(entry[1], int)
        and not isinstance(entry[1], bool) and isinstance(entry[2], str)
    ]
    match_id = start["match_id"]
    for kind, value in [("match_id", match_id)] + [
            ("agent_id", agent) for agent, _, _ in agents]:
        if not _SAFE_ID_RE.fullmatch(value):
            raise ValueError(
                f"unsafe {kind} {value!r}: must match "
                "[A-Za-z0-9][A-Za-z0-9._-]{0,63} — it lands inside node "
                "uuid templates and could collide with another namespace")
    ends = [r for r in records if r.get("kind") == "MATCH_END"]
    strays = sorted({str(e.get("match_id")) for e in ends
                     if e.get("match_id") != match_id})
    if strays:
        raise ValueError(f"MATCH_END match_id {strays} does not match the "
                         f"MATCH_START match_id {match_id!r}")
    summary: dict[str, Any] = {}
    if ends and isinstance(ends[-1].get("summary"), dict):
        summary = ends[-1]["summary"]
    final_turn = summary.get("final_turn")
    if not isinstance(final_turn, int) or isinstance(final_turn, bool):
        final_turn = None  # project() falls back over MATCH-BOUND records
    civ_by_pid: dict[int, str] = {}
    for civ, doc in (summary.get("scores") or {}).items():
        if isinstance(doc, dict) and isinstance(doc.get("player_id"), int) \
                and not isinstance(doc.get("player_id"), bool):
            civ_by_pid[doc["player_id"]] = str(civ)
    return {
        "match_id": start["match_id"],
        "roster": agents,
        "seed": config.get("seed") if isinstance(config.get("seed"), int) else 0,
        "max_turns": (config.get("max_turns")
                      if isinstance(config.get("max_turns"), int) else 0),
        "final_turn": final_turn,
        "aborted": str(summary.get("aborted") or ""),
        "scores": summary.get("scores") if isinstance(
            summary.get("scores"), dict) else {},
        "civ_by_pid": civ_by_pid,
    }


def project(records: list[dict[str, Any]]) -> Projection:
    """Project one match's event log into the graph. Pure: same records in,
    same nodes/edges out, sorted by uuid — never reads ``ts``, never touches
    wall-clock, network, or a DB."""
    meta = _meta(records)
    match_id = meta["match_id"]
    group = f"{match_id}:main"
    # bind every record to the selected match: a concatenated log must not
    # leak another match's claims/digests into this match's graph
    records = [r for r in records if r.get("match_id") == match_id]
    store = StrategyStore.from_log(records)
    final_turn: int = meta["final_turn"] if meta["final_turn"] is not None else max(
        (r.get("turn") for r in records
         if r.get("kind") == "TURN_END" and isinstance(r.get("turn"), int)
         and not isinstance(r.get("turn"), bool)),
        default=0)

    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}

    def add_node(uuid: str, group_id: str, labels: list[str], name: str,
                 **props: Any) -> str:
        if uuid in nodes:
            raise ValueError(f"duplicate node uuid {uuid!r}")
        nodes[uuid] = {
            "uuid": uuid, "group_id": group_id, "labels": labels,
            "name": name, **props,
        }
        return uuid

    def add_edge(source: str, rel: str, target: str,
                 **props: Any) -> None:
        uuid = _edge_uuid(source, rel, target)
        if uuid in edges:
            raise ValueError(f"duplicate edge {uuid!r}")
        # every edge carries a group_id so the loader can reconcile one
        # match's graph without touching another match's (spine edges are
        # cross_match and never reconciled away)
        group_id = props.pop("edge_group", group)
        edges[uuid] = {
            "uuid": uuid, "source": source, "target": target, "rel": rel,
            "group_id": group_id, **props,
        }

    def claim_uuid(pid: int, cid: str, revision: int) -> str:
        return f"{group}:claim:p{pid}:{cid}:r{revision}"

    # ------------------------------------------------- the cross-match spine
    match_uuid = add_node(
        f"match:{match_id}", CROSS_GROUP, ["Match"], match_id,
        match_id=match_id, seed=meta["seed"], max_turns=meta["max_turns"],
        final_turn=final_turn, aborted=meta["aborted"],
        scores_json=_jdump(meta["scores"]))
    agent_by_pid = {pid: agent for agent, pid, _ in meta["roster"]}
    policy_by_agent = {agent: policy for agent, _, policy in meta["roster"]}
    for agent in sorted(policy_by_agent):
        agent_uuid = add_node(
            f"agent:{agent}", CROSS_GROUP, ["Agent"], agent, agent_id=agent)
        add_edge(agent_uuid, "PLAYED_IN", match_uuid, edge_group=CROSS_GROUP)

    player_ids = sorted(
        set(agent_by_pid)
        | set(store.goals) | set(store.predictions) | set(store.lessons)
        | set(store.beliefs.entries))
    player_uuids: dict[int, str] = {}
    for pid in player_ids:
        agent_id = agent_by_pid.get(pid, "")
        civ = meta["civ_by_pid"].get(pid, "")
        puuid = add_node(
            f"{group}:player:p{pid}", group, ["Player"],
            civ if civ else f"p{pid}",
            player_id=pid, agent_id=agent_id, civ_name=civ,
            policy=policy_by_agent.get(agent_id, ""))
        player_uuids[pid] = puuid
        if agent_id:
            # policy is a PER-MATCH fact: same agent_id may play different
            # policies in different matches, so it lives on the
            # match-scoped participation edge, never on the shared Agent
            # node (which would make DB contents load-order dependent)
            add_edge(f"agent:{agent_id}", "PLAYED_AS", puuid,
                     policy=policy_by_agent.get(agent_id, ""))

    # the match's entity universe (ids are match-global): both the entity
    # nodes and REFERENCES resolution use the same set
    entity_ids: set[str] = set()
    for pid in player_ids:
        entity_ids.update(store.beliefs.entries.get(pid) or {})

    # ------------------------------------------------------- claims + verdicts
    claim_counts = {"goal": 0, "prediction": 0, "lesson": 0}
    dropped_refs: list[str] = []
    for pid in player_ids:
        puuid = player_uuids[pid]
        for kind, by_id in (("goal", store.goals.get(pid) or {}),
                            ("prediction", store.predictions.get(pid) or {})):
            for cid in sorted(by_id):
                history = by_id[cid]
                for rec in history:
                    claim_counts[kind] += 1
                    _add_claim_node(add_node, group, pid, rec, kind, cid,
                                    current=rec is history[-1])
                    add_edge(puuid, "AUTHORED",
                             claim_uuid(pid, cid, rec.revision))
                    if rec.revision > 1:
                        add_edge(claim_uuid(pid, cid, rec.revision),
                                 "SUPERSEDES",
                                 claim_uuid(pid, cid, rec.revision - 1),
                                 amend_turn=rec.created_turn)
                    ref = (rec.subject_id
                           if isinstance(rec, Prediction) else None)
                    if ref:
                        _add_reference(
                            add_edge, group, pid, cid, rec.revision,
                            rec.created_seq, ref, "subject_id", store,
                            entity_ids, dropped_refs)
        for lesson in store.lesson_list(pid):
            claim_counts["lesson"] += 1
            _add_claim_node(add_node, group, pid, lesson, "lesson",
                            lesson.lesson_id, current=True)
            add_edge(puuid, "AUTHORED", claim_uuid(pid, lesson.lesson_id, 1))
            if lesson.about:
                _add_reference(
                    add_edge, group, pid, lesson.lesson_id, 1,
                    lesson.created_seq, lesson.about, "about", store,
                    entity_ids, dropped_refs)

        # VERDICT: outcomes for claims due by the projection horizon. Verdicts
        # are recomputed here (never stored in the log); claims resolved
        # another way (a goal closed done/dropped, a prediction closed by its
        # verdict lesson) carry that on the claim node / REFERENCES edge.
        due: list[tuple[Any, str]] = [
            (g, g.goal_id) for g in scoring.due_goals(store, pid, final_turn)
        ] + [
            (p, p.prediction_id) for p in
            scoring.due_predictions(store, pid, final_turn)
        ]
        for claim, cid in due:
            verdict = scoring.verdict(claim, store.facts, pid, final_turn)
            deadline = scoring.deadline_turn(claim, final_turn) or final_turn
            as_of = min(final_turn, deadline)
            value = (scoring.metric_value(store.facts, pid, claim.metric, as_of)
                     if verdict != scoring.SELF_ASSESS else None)
            src = claim_uuid(pid, cid, claim.revision)
            outcome_uuid = f"{group}:outcome:p{pid}:{cid}:r{claim.revision}"
            outcome_props: dict[str, Any] = {
                "claim_uuid": src, "verdict": verdict, "metric": claim.metric,
                "target": claim.target, "due_turn": deadline, "as_of_turn": as_of,
            }
            if value is not None:
                outcome_props["value"] = value
            add_node(outcome_uuid, group, ["Outcome"], f"{cid}:r{claim.revision}",
                     **outcome_props)
            add_edge(src, "VERDICT", outcome_uuid)

    # ------------------------------------------------------ entities: OBSERVED
    kind_conflicts: list[str] = []
    for eid in sorted(entity_ids):
        # first observation in sorted-player order settles the node's kind
        settled = ""
        seen_kinds: set[str] = set()
        for pid in player_ids:
            bucket = store.beliefs.entries.get(pid) or {}
            if eid in bucket:
                seen_kinds.add(bucket[eid]["kind"])
                if not settled:
                    settled = bucket[eid]["kind"]
        if len(seen_kinds) > 1:
            kind_conflicts.append(
                f"{eid}: kept {settled}, saw {sorted(seen_kinds - {settled})}")
        extra = {"unit": ["Unit"], "city": ["City"]}.get(settled, [])
        add_node(f"{group}:entity:{eid}", group, ["Entity"] + extra, eid,
                 entity_id=eid, kind=settled)
    for pid in player_ids:
        puuid = player_uuids[pid]
        for eid, rec in sorted(
                (store.beliefs.entries.get(pid) or {}).items()):
            add_edge(
                puuid, "OBSERVED", f"{group}:entity:{eid}",
                kind=rec["kind"], last_seen_turn=rec["last_seen_turn"],
                last_seen_seq=rec["last_seen_seq"],
                fields_json=_jdump(rec["fields"]))

    ordered_nodes = [nodes[u] for u in sorted(nodes)]
    ordered_edges = [edges[u] for u in sorted(edges)]
    report = {
        "match_id": match_id,
        "final_turn": final_turn,
        "source_records": len(records),
        "claims": {
            "goal_ids": sum(len(store.goals.get(p) or {})
                            for p in player_ids),
            "goal_revisions": claim_counts["goal"],
            "prediction_ids": sum(len(store.predictions.get(p) or {})
                                  for p in player_ids),
            "prediction_revisions": claim_counts["prediction"],
            "lessons": claim_counts["lesson"],
        },
        "nodes": {
            "total": len(ordered_nodes),
            "by_label": {
                key: sum(1 for x in ordered_nodes
                         if "+".join(sorted(x["labels"])) == key)
                for key in sorted({"+".join(sorted(n["labels"]))
                                   for n in ordered_nodes})
            },
        },
        "edges": {
            "total": len(ordered_edges),
            "by_rel": {
                rel: sum(1 for e in ordered_edges if e["rel"] == rel)
                for rel in sorted({e["rel"] for e in ordered_edges})
            },
        },
        "dropped_references": dropped_refs,
        "entity_kind_conflicts": kind_conflicts,
    }
    return Projection(nodes=ordered_nodes, edges=ordered_edges, report=report)


def _add_claim_node(
    add_node: Any, group: str, pid: int, rec: Goal | Prediction | Lesson,
    kind: str, cid: str, current: bool,
) -> str:
    # lessons never amend: revision is always 1, validity open from creation
    revision = getattr(rec, "revision", 1)
    props: dict[str, Any] = {
        "kind": kind, "claim_id": cid, "text": rec.text,
        "created_turn": rec.created_turn, "created_seq": rec.created_seq,
        "revision": revision, "current": current,
        "valid_from_turn": getattr(rec, "valid_from_turn", rec.created_turn),
        "valid_to_turn": getattr(rec, "valid_to_turn", 0),
    }
    if isinstance(rec, Goal):
        props.update(by_turn=rec.by_turn, metric=rec.metric,
                     target=rec.target, status=rec.status,
                     confidence=rec.confidence)
    elif isinstance(rec, Prediction):
        props.update(review_turn=rec.review_turn, subject_id=rec.subject_id,
                     metric=rec.metric, target=rec.target,
                     confidence=rec.confidence)
    else:
        props.update(about=rec.about)
    return add_node(
        f"{group}:claim:p{pid}:{cid}:r{revision}", group,
        ["Claim", _KIND_LABEL[kind]], f"{cid}:r{revision}", **props)


def _add_reference(
    add_edge: Any, group: str, pid: int, cid: str, revision: int,
    created_seq: int, ref: str, via: str, store: StrategyStore,
    entity_ids: set[str], dropped: list[str],
) -> None:
    """Resolve subject_id/about to a claim revision (the one authoritative
    at the referencing claim's created_seq) or an entity node; dangling refs
    are dropped and reported, never guessed."""
    src = f"{group}:claim:p{pid}:{cid}:r{revision}"
    target = ""
    claim_namespace = False
    for by_id in (store.goals.get(pid) or {}, store.predictions.get(pid) or {}):
        if ref in by_id:
            # the id names a CLAIM: if no revision was authoritative yet
            # (the reference predates the claim), it must DROP — falling
            # through to a same-id entity would silently misbind it
            claim_namespace = True
            resolved = _revision_at(by_id[ref], created_seq)
            if resolved is not None:
                target = f"{group}:claim:p{pid}:{ref}:r{resolved.revision}"
            break
    if not target and not claim_namespace and ref in entity_ids:
        target = f"{group}:entity:{ref}"
    if not target:
        dropped.append(f"p{pid}:{cid} -> {ref!r} ({via})")
        return
    add_edge(src, "REFERENCES", target, via=via)
