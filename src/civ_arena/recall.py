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

import re
from pathlib import Path
from typing import Any

from civ_arena.graph.project import load_records
from civ_arena.strategy.store import StrategyStore

LESSON_LIMIT = 5
_TOKEN_RE = re.compile(r"[a-z0-9]+")


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
