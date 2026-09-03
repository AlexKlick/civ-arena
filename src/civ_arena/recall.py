"""Recall: the in-game cross-match memory (M13).

The corpus is an agent's OWN lessons from PRIOR matches, read straight from
their event logs — the same trust root resume and replay use, so a match
never depends on a graph DB or a projection step (the graph stays the human
query surface). Retrieval is deterministic lexical scoring — no embeddings,
no LLM: same corpus + same query -> the same lessons, in the same order.

Prior runs are immutable finished matches named explicitly by the match
config; a run still being written is out of scope by contract (the config
names what to recall, not a directory to watch).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from civ_arena.graph.project import load_records
from civ_arena.strategy.store import StrategyStore

LESSON_LIMIT = 5
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _declared_schema(path: Path) -> int | None:
    """Read only the first record to route V1 versus V2 custody."""

    if path.is_symlink():
        raise ValueError("recall event ledger path must not be a symlink")
    try:
        first = next(line for line in path.read_text(encoding="utf-8").splitlines() if line)
        doc = json.loads(first)
    except (StopIteration, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    schema = doc.get("schema")
    return schema if isinstance(schema, int) and not isinstance(schema, bool) else None


def _v2_committed_operations(
    episode_dir: Path,
    episode_id: str,
    *,
    seen: set[str] | None = None,
) -> list[Any]:
    """Load observable policy operations through the last completed phase.

    A child resumes policy operation sequence numbers from its parent, so
    recall follows and verifies that lineage first. Partial policy work after
    the last completed receipt is deliberately excluded, matching resume.
    """

    from civ_arena.v2.contracts import (
        ArtifactRefV2,
        EpisodeReceiptV2,
        EventTypeV2,
        PolicyStateV2,
    )
    from civ_arena.v2.ledger import ObjectStoreV2, load_events_v2, verify_ledger_v2
    from civ_arena.v2.resume import resume_checkpoint_sequence_v2

    visited = set() if seen is None else seen
    if episode_id in visited:
        raise ValueError(f"recall V2 parent lineage contains a cycle at {episode_id!r}")
    visited.add(episode_id)

    verification = verify_ledger_v2(episode_dir)
    events = load_events_v2(episode_dir / "events.jsonl")
    terminal = events[-1].payload_value
    if not isinstance(terminal, EpisodeReceiptV2) or terminal.episode_id != episode_id:
        raise ValueError(
            f"recall corpus run {episode_id!r} terminal identity does not match its directory"
        )
    operations: list[Any] = []
    if terminal.parent_episode_id is not None:
        parent_dir = episode_dir.parent / terminal.parent_episode_id
        if not parent_dir.is_dir():
            raise ValueError(
                f"recall corpus child {episode_id!r} is missing parent "
                f"{terminal.parent_episode_id!r}"
            )
        parent_verification = verify_ledger_v2(parent_dir)
        if parent_verification.terminal_event_hash != terminal.parent_terminal_event_hash:
            raise ValueError(
                f"recall corpus child {episode_id!r} parent hash does not match"
            )
        operations.extend(
            _v2_committed_operations(
                parent_dir,
                terminal.parent_episode_id,
                seen=visited,
            )
        )

    checkpoint_sequence = resume_checkpoint_sequence_v2(episode_dir)
    store = ObjectStoreV2(episode_dir)
    for event in events[1 : checkpoint_sequence + 1]:
        if event.event_type is not EventTypeV2.POLICY_STATE_RECORDED:
            continue
        ref = event.payload_value
        if not isinstance(ref, ArtifactRefV2) or ref.schema_ref != PolicyStateV2.SCHEMA_REF:
            raise ValueError("recall V2 policy-state event has the wrong artifact contract")
        state = PolicyStateV2.from_doc(store.read_doc(ref))
        operations.extend(state.operations)

    # ``verification`` is intentionally consumed: its full chain/artifact
    # validation is the authority for every operation returned above.
    assert verification.terminal_event_hash == terminal.terminal_event_hash
    return operations


def _v2_lesson_entries(episode_dir: Path, episode_id: str) -> list[dict[str, Any]]:
    """Rebuild typed strategy records from committed V2 policy operations."""

    from civ_arena.strategy.store import CLAIM_TOOLS

    operations = _v2_committed_operations(episode_dir, episode_id)
    stores: dict[int, StrategyStore] = {}
    agents: dict[int, str] = {}
    expected_sequence: dict[int, int] = {}
    claim_sequence: dict[int, int] = {}
    for operation in operations:
        pid = operation.player_id
        prior_agent = agents.setdefault(pid, operation.agent_id)
        if prior_agent != operation.agent_id:
            raise ValueError("recall V2 policy operation changes its seat identity")
        expected = expected_sequence.get(pid, 0)
        if operation.sequence != expected:
            raise ValueError(
                "recall V2 policy operation sequence is not contiguous "
                f"for player {pid}: expected {expected}"
            )
        expected_sequence[pid] = expected + 1
        if operation.tool not in CLAIM_TOOLS:
            continue
        store = stores.setdefault(pid, StrategyStore())
        seq = claim_sequence.get(pid, 0)
        result = store.apply_claim(
            operation.tool,
            pid,
            operation.args,
            operation.turn,
            seq,
        )
        claim_sequence[pid] = seq + 1
        if result != operation.result:
            raise ValueError("recall V2 strategy operation does not replay exactly")

    entries: list[dict[str, Any]] = []
    for pid, store in sorted(stores.items()):
        agent_id = agents[pid]
        for lesson in store.lesson_list(pid):
            entries.append(
                {
                    "agent_id": agent_id,
                    "match_id": episode_id,
                    "turn": lesson.created_turn,
                    "lesson_id": lesson.lesson_id,
                    "text": lesson.text,
                    "about": lesson.about,
                }
            )
    return entries


def _roster(records: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """(agent_id, player_id) pairs from the run's MATCH_START. Fail-closed:
    every entry must be exactly (str, non-bool int, str) — silently
    filtering a malformed entry could mis-attribute another agent's
    lessons."""
    for rec in records:
        if rec.get("kind") != "MATCH_START":
            continue
        config = rec.get("config") if isinstance(rec.get("config"), dict) else {}
        entries = config.get("agents") or []
        if not isinstance(entries, list) or not entries:
            return []
        roster: list[tuple[str, int]] = []
        for entry in entries:
            if not isinstance(entry, list) or len(entry) != 3 \
                    or not isinstance(entry[0], str) or not entry[0] \
                    or not isinstance(entry[1], int) \
                    or isinstance(entry[1], bool) \
                    or not isinstance(entry[2], str):
                return []  # malformed -> caller reports "no valid roster"
            roster.append((entry[0], entry[1]))
        return roster
    return []


def _terms(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


class RecallCorpus:
    """Lessons of prior matches, keyed by author. A pure function of
    immutable inputs: constructed once per match (and rebuilt on resume to
    the same content)."""

    def __init__(self, entries: list[dict[str, Any]]):
        # one entry per (prior match, author, lesson), sorted for stability
        self._entries = sorted(
            entries,
            key=lambda e: (e["match_id"], e["agent_id"], e["turn"],
                           e["lesson_id"]),
        )

    @classmethod
    def from_runs(
        cls, run_dir: Path, match_id: str, prior_ids: list[str],
    ) -> RecallCorpus:
        """Build the corpus from prior run dirs. The CURRENT match id is
        always excluded (even if listed — on resume its own log exists and
        must never feed its own recall). Every prior must be a FINISHED
        match (MATCH_END present, identity matching the requested id) with a
        well-formed roster — a run still being written could feed a
        crash-resume rebuild lessons that did not exist when the match
        started, and a malformed roster could attribute one agent's lessons
        to another. All failures are loud, before any match spend."""
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for prior in prior_ids:
            if prior == match_id or prior in seen:
                continue
            seen.add(prior)
            path = Path(run_dir) / prior / "events.jsonl"
            if not path.exists():
                raise ValueError(
                    f"recall corpus names missing run {prior!r} "
                    f"under {run_dir}")
            if _declared_schema(path) == 2:
                entries.extend(_v2_lesson_entries(path.parent, prior))
                continue
            records = load_records(path)
            # bind EVERY record to the requested match: a concatenated log
            # must not relabel a foreign match's lessons as this prior's
            records = [r for r in records if r.get("match_id") == prior]
            roster = _roster(records)
            if not roster:
                raise ValueError(
                    f"recall corpus run {prior!r} has no well-formed "
                    "MATCH_START roster (missing, malformed, or carrying a "
                    "foreign match_id)")
            starts = [r for r in records if r.get("kind") == "MATCH_START"]
            ends = [r for r in records if r.get("kind") == "MATCH_END"]
            if len(starts) != 1 or starts[0].get("match_id") != prior:
                raise ValueError(
                    f"recall corpus run {prior!r} must carry exactly one "
                    f"MATCH_START with a matching id (got {len(starts)}) — "
                    "replay twins and misplaced dirs are not corpus members")
            if len(ends) != 1 or records[-1].get("kind") != "MATCH_END":
                raise ValueError(
                    f"recall corpus run {prior!r} is not a finished match "
                    "(exactly one terminal MATCH_END required) — corpus "
                    "members must be immutable")
            players = [pid for _, pid in roster]
            agents = [agent for agent, _ in roster]
            if len(set(players)) != len(players) \
                    or len(set(agents)) != len(agents):
                raise ValueError(
                    f"recall corpus run {prior!r} has a malformed roster "
                    f"(duplicate identity): {roster!r}")
            store = StrategyStore.from_log(records)
            for agent_id, pid in roster:
                for lesson in store.lesson_list(pid):
                    entries.append({
                        "agent_id": agent_id,
                        "match_id": prior,
                        "turn": lesson.created_turn,
                        "lesson_id": lesson.lesson_id,
                        "text": lesson.text,
                        "about": lesson.about,
                    })
        return cls(entries)

    def query(
        self, agent_id: str, query: str, limit: int = LESSON_LIMIT,
    ) -> list[dict[str, Any]]:
        """The author's own lessons matching the query, best first.
        Deterministic: score = matched term count (text + about), ties break
        by (match_id, turn, lesson_id); zero-score lessons never return —
        no filler, no randomness."""
        terms = _terms(query)
        scored: list[tuple[int, dict[str, Any]]] = []
        for entry in self._entries:
            if entry["agent_id"] != agent_id:
                continue  # another agent's lessons are not yours to read
            tokens = _terms(entry["text"])
            if entry["about"]:
                tokens |= _terms(entry["about"])
            score = len(terms & tokens)
            if score > 0:
                scored.append((score, entry))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["match_id"],
                                      pair[1]["turn"], pair[1]["lesson_id"]))
        return [
            {k: entry[k]
             for k in ("match_id", "turn", "lesson_id", "text", "about")}
            for _, entry in scored[:max(0, limit)]
        ]

    def size(self, agent_id: str | None = None) -> int:
        if agent_id is None:
            return len(self._entries)
        return sum(1 for e in self._entries if e["agent_id"] == agent_id)
