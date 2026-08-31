"""M16a — the planner belief journal: resume without amnesia.

A side artifact in the ``spend.jsonl`` tradition: beside the event log,
never inside it. Each completed planner turn appends one canonical line —
the turn's four projected observations plus the option state — and the
runtime rebuilds its ``PlannerBelief`` on resume by replaying entries
through the SAME ``observe_*`` methods (the ``StrategyStore.from_log``
discipline: rebuilt == live by construction, not by imitation).

Trust model, stated plainly: the journal is ADVISORY. It contains only
projections the planner already received, so writing it changes no
information flow; replay never compares it; a tampered journal can
mislead the planner's internal approximation but cannot touch the
referee's authority (illegal actions still reject; the watchdog still
sweeps). Corruption fails CLOSED like the event log: a torn TRAILING
line (crash mid-write) is tolerated and dropped; a malformed mid-file
line refuses loudly rather than guessing.

Turn-keyed idempotency: ``append(turn, ...)`` first drops every entry
with ``turn >= turn`` — a crashed-and-resumed run that re-executes a
turn overwrites that turn's entry instead of duplicating it, and
``replay_upto(turn)`` returns only entries with a strictly earlier turn.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class PlannerJournal:
    """One planner seat's observation journal (JSONL, atomic rewrites)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> tuple[list[dict[str, Any]], bool]:
        """EventLog semantics: torn tail tolerated, mid-file corruption
        refused, ordering by turn validated (monotone, one per turn)."""
        if not self.path.exists():
            return [], False
        raw = self.path.read_text(encoding="utf-8").splitlines()
        out: list[dict[str, Any]] = []
        for i, line in enumerate(raw):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                if i == len(raw) - 1:
                    return out, True  # torn tail from a crash mid-write
                raise ValueError(
                    f"corrupt planner journal line {i}: {line[:80]!r}") from None
            if not isinstance(rec, dict):
                raise ValueError(
                    f"planner journal line {i} is not an object")
            turn = rec.get("turn")
            if not isinstance(turn, int) or isinstance(turn, bool):
                raise ValueError(f"planner journal line {i} has no int turn")
            if out and turn <= out[-1]["turn"]:
                raise ValueError(
                    f"planner journal turns not monotone at line {i}")
            out.append(rec)
        return out, False

    def append(self, turn: int, doc: dict[str, Any]) -> None:
        """Record ``doc`` for ``turn``, dropping any entry at ``turn`` or
        later first (idempotent turn re-execution)."""
        recs, _torn = self._load()
        recs = [r for r in recs if r["turn"] < turn]
        recs.append({"turn": turn, "doc": doc})
        tmp = self.path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for rec in recs:
                fh.write(json.dumps(rec, sort_keys=True,
                                    separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def replay_upto(self, turn: int) -> list[dict[str, Any]]:
        """Entries for strictly earlier turns, oldest first."""
        recs, _torn = self._load()
        return [r["doc"] for r in recs if r["turn"] < turn]
