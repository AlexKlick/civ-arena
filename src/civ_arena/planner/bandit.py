"""M20b — the contextual fixed-point bandit over options.

An ONLINE, within-match learning prior: every planner decision world is
bucketed to a coarse integer CONTEXT, and each (context, option) arm
accumulates ``n`` trials and ``adv_sum`` — the summed realized advantage
the runtime measures between successive decision worlds
(``value_of(now) - value_of(then)``, integers by construction). Ranking
is mean advantage under EXACT integer cross-multiplication (the M19b
``CaseBase.rank`` precedent: at 2**53 scale IEEE doubles collide, so
a/b vs c/d is a*d vs c*b and no float ever touches the ordering — or
the state: the fixed-point discipline is ints only, ``to_doc`` is
canonical-ready).

CONTEXT — ``context_bucket(state, pid) -> tuple[int, ...]``, six axes
read from the SAME search ``abstract_doc`` the MCGS key and the case
signature come from, with the M19c MINING LADDERS (the threshold
constants are IMPORTED from ``planner.mining`` so offline-mined
heuristics and online bandit stats share one context vocabulary —
future lanes align by construction, pinned against
``mining.feature_items``):

    (own_cities, own_gold, own_researched, own_units,
     rival_cities, rival_researched)                     all ints

    own_cities         exact own city count (a 0..3 range needs no bucket)
    own_gold           ladder: gold_bucket >= 8/16/24 (gold // 25, i.e.
                       >= 200/400/600 gold)                    -> 0..3
    own_researched     ladder: researched count >= 1/3/5        -> 0..3
    own_units          ladder: unit total >= 5/10/20            -> 0..3
    rival_cities       exact city count of the strongest rival
                       (highest ``scalarize(score_vector)``; ties ->
                       smallest player id — ``value_of``'s rival rule)
    rival_researched   ladder: rival researched count >= 1/3/5  -> 0..3

Each ladder value is the COUNT of thresholds cleared (a monotone "at
least this level" rung). States differing only BELOW the thresholds
share a context; crossing a threshold splits it.

STATE — per (context, option): ``{"n": int, "adv_sum": int}``. The
constructor takes an optional init state doc (offline initialization
from labels; ``updates``/``abs_adv_sum`` stay 0 there — they count
ONLINE updates only). exp-M20b starts EMPTY, both arms. ``to_doc``/
``from_doc`` are a canonical round-trip (context keys serialize as
comma-joined integers, e.g. ``"0,0,0,1,0,0"``).

STATE BOUND — at most ``MAX_CONTEXTS`` (512) distinct contexts. When an
update would create context 513, the SMALLEST-n context (total trials
across its arms; ties -> the lexicographically smallest context tuple)
is dropped first — deterministic, enforced at update time and again at
construction, and pinned.

SELECTION — ``rank(context, candidates)`` returns a FULL ranking of the
given candidates: informed arms (n > 0) ordered by mean advantage
(adv_sum_a * n_b vs adv_sum_b * n_a, higher first), ties by option_id
asc; untried arms (n == 0) rank after ALL informed ones, by option_id
asc (a deterministic tail). Duplicated candidates keep the first
occurrence; unknown ids are simply untried.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from functools import cmp_to_key
from typing import Any

from civ_arena.game.sim.value import scalarize, score_vector
from civ_arena.planner.mining import (
    GOLD_THRESHOLDS,
    RESEARCHED_THRESHOLDS,
    UNIT_THRESHOLDS,
)
from civ_arena.planner.search import abstract_doc

SCHEMA = 1

# State-size bound: at most this many distinct context keys (documented
# module contract; overflow evicts smallest-n, then lexicographically).
MAX_CONTEXTS = 512


def _ladder(value: int, thresholds: tuple[int, ...]) -> int:
    """Count of thresholds cleared (each ladder = count of thresholds met)."""
    return sum(1 for threshold in thresholds if value >= threshold)


def context_bucket(state: Any, pid: int) -> tuple[int, ...]:
    """The 6-axis integer context of a decision world (see module docstring
    for the axis-by-axis mapping). Pure function of the determinized world
    the runtime's ``bstate`` already is."""
    doc = abstract_doc(state, pid)
    own = doc[str(pid)]
    rivals = [int(k) for k in doc
              if k not in ("turn", "candidates") and int(k) != pid]
    if rivals:
        rival = max(rivals, key=lambda r: (scalarize(
            score_vector(state, r)), -r))
        rdoc = doc[str(rival)]
        rival_cities = int(rdoc["cities"])
        rival_researched = _ladder(len(rdoc["researched"]),
                                   RESEARCHED_THRESHOLDS)
    else:
        rival_cities, rival_researched = 0, 0
    return (
        int(own["cities"]),
        _ladder(int(own["gold_bucket"]), GOLD_THRESHOLDS),
        _ladder(len(own["researched"]), RESEARCHED_THRESHOLDS),
        _ladder(sum(own["units"].values()), UNIT_THRESHOLDS),
        rival_cities,
        rival_researched,
    )


def _ctx_key(ctx: tuple[int, ...]) -> str:
    """Context tuple -> its doc form (comma-joined integers; ints and
    commas only, so the mapping is unambiguous)."""
    return ",".join(str(x) for x in ctx)


def _parse_ctx_key(key: Any) -> tuple[int, ...]:
    if not isinstance(key, str) or not key:
        raise ValueError(f"bandit context key must be a comma-joined "
                         f"integer string, got {key!r}")
    parts = key.split(",")
    for part in parts:
        digits = part[1:] if part.startswith("-") else part
        if not digits or not digits.isdigit():
            raise ValueError(f"bandit context key {key!r} is not "
                             "comma-joined integers")
    return tuple(int(part) for part in parts)


def _validated(init: Any) -> tuple[dict[tuple[int, ...],
                                        dict[str, dict[str, int]]],
                                   int, int]:
    """Loud shape check (the CaseBase construction discipline — an init
    doc from labels is validated BEFORE any match spend). Tolerant of
    UNKNOWN top-level keys: the journal's bandit block legitimately
    carries the pending-decision fields beside the state. Strict on
    everything it reads: schema, per-record int coherence (n >= 1 — an
    untried arm carries no record), non-negative online counters."""
    if init is None:
        return {}, 0, 0
    if not isinstance(init, dict):
        raise ValueError(f"bandit init state must be a mapping (the "
                         f"to_doc shape), got {type(init).__name__}")
    if init.get("schema") != SCHEMA:
        raise ValueError(f"bandit state schema must be {SCHEMA}, "
                         f"got {init.get('schema')!r}")
    raw = init.get("contexts", {})
    if not isinstance(raw, dict):
        raise ValueError("bandit state 'contexts' must be a mapping")
    arms: dict[tuple[int, ...], dict[str, dict[str, int]]] = {}
    for key, bucket in raw.items():
        ctx = _parse_ctx_key(key)
        if not isinstance(bucket, dict):
            raise ValueError(f"bandit context {key!r} must map option ids "
                             "to stat mappings")
        records: dict[str, dict[str, int]] = {}
        for oid, rec in bucket.items():
            where = f"bandit stat {key}..{oid}"
            if not isinstance(oid, str) or not isinstance(rec, dict):
                raise ValueError(f"{where}: option entries must map option "
                                 "ids to stat mappings")
            if set(rec) != {"n", "adv_sum"}:
                raise ValueError(f"{where}: a record carries exactly "
                                 "n/adv_sum")
            n, adv_sum = rec["n"], rec["adv_sum"]
            for name, val in (("n", n), ("adv_sum", adv_sum)):
                if not isinstance(val, int) or isinstance(val, bool):
                    raise ValueError(f"{where}.{name} must be an integer, "
                                     f"got {val!r}")
            if n < 1:
                raise ValueError(f"{where}.n must be >= 1 — an untried arm "
                                 "carries no record")
            records[oid] = {"n": n, "adv_sum": adv_sum}
        arms[ctx] = records
    counters: list[int] = []
    for name in ("updates", "abs_adv_sum"):
        val = init.get(name, 0)
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            raise ValueError(f"bandit state {name} must be a non-negative "
                             f"integer, got {val!r}")
        counters.append(val)
    return arms, counters[0], counters[1]


class Bandit:
    """Deterministic contextual fixed-point bandit. Construct loudly (a
    malformed init doc refuses), consumed fail-soft at runtime (the
    ``_case_prior`` boundary shape — any per-turn failure degrades to no
    bandit prior)."""

    def __init__(self, init: dict[str, Any] | None = None) -> None:
        arms, updates, abs_adv_sum = _validated(
            copy.deepcopy(init) if init is not None else None)
        self._arms = arms
        self.updates = updates          # online update() calls, ever
        self.abs_adv_sum = abs_adv_sum  # summed |advantage| (diagnostics)
        self._enforce_bound()

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> Bandit:
        """Restore from a ``to_doc`` doc (or the journal's bandit block —
        unknown top-level keys are tolerated by design)."""
        return cls(doc)

    def to_doc(self) -> dict[str, Any]:
        """Canonical-ready state doc: ints/strings only, sorted iteration
        everywhere, context keys comma-joined."""
        contexts = {
            _ctx_key(ctx): {oid: {"n": rec["n"], "adv_sum": rec["adv_sum"]}
                            for oid, rec in sorted(bucket.items())}
            for ctx, bucket in sorted(self._arms.items())
        }
        return {"schema": SCHEMA, "contexts": contexts,
                "updates": self.updates, "abs_adv_sum": self.abs_adv_sum}

    def __len__(self) -> int:
        return len(self._arms)

    def _total_n(self, ctx: tuple[int, ...]) -> int:
        return sum(rec["n"] for rec in self._arms[ctx].values())

    def _enforce_bound(self) -> None:
        """Drop smallest-n contexts (ties -> lexicographically smallest
        context tuple) until at most MAX_CONTEXTS remain. Deterministic
        in the state alone, never in dict insertion order."""
        while len(self._arms) > MAX_CONTEXTS:
            drop = min(self._arms, key=lambda c: (self._total_n(c), c))
            del self._arms[drop]

    def rank(self, context: tuple[int, ...],
             candidates: Iterable[str]) -> list[str]:
        """Full ranking of ``candidates`` (see module docstring): informed
        arms by mean advantage — EXACT integer cross-multiplication, the
        M19b rank() precedent — ties by option_id asc; untried arms after
        all informed ones, by option_id asc. Read-only: never creates
        state. Duplicates keep the first occurrence."""
        menu: list[str] = []
        for oid in candidates:
            if isinstance(oid, str) and oid not in menu:
                menu.append(oid)
        bucket = self._arms.get(context, {})
        informed = [oid for oid in menu
                    if bucket.get(oid, {"n": 0})["n"] > 0]
        untried = [oid for oid in menu
                   if bucket.get(oid, {"n": 0})["n"] == 0]

        def _cmp(a: str, b: str) -> int:
            ra, rb = bucket[a], bucket[b]
            lhs = ra["adv_sum"] * rb["n"]
            rhs = rb["adv_sum"] * ra["n"]
            if lhs != rhs:
                return -1 if lhs > rhs else 1
            return -1 if a < b else (1 if a > b else 0)

        informed.sort(key=cmp_to_key(_cmp))
        return informed + sorted(untried)

    def update(self, context: tuple[int, ...], option: str,
               advantage: int) -> None:
        """n += 1, adv_sum += advantage for (context, option); called ONCE
        per settled decision with the realized advantage. Creating context
        513 first evicts the smallest-n context (deterministic bound)."""
        if not isinstance(option, str):
            raise ValueError(f"bandit option id must be a str, "
                             f"got {option!r}")
        if not isinstance(advantage, int) or isinstance(advantage, bool):
            raise ValueError(f"bandit advantage must be an int (the "
                             f"fixed-point contract), got {advantage!r}")
        if context not in self._arms:
            if len(self._arms) >= MAX_CONTEXTS:
                drop = min(self._arms, key=lambda c: (self._total_n(c), c))
                del self._arms[drop]
            self._arms[context] = {}
        rec = self._arms[context].setdefault(option, {"n": 0, "adv_sum": 0})
        rec["n"] += 1
        rec["adv_sum"] += advantage
        self.updates += 1
        self.abs_adv_sum += abs(advantage)
