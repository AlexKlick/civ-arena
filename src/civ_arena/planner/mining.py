"""M19c — offline pattern mining over the M19a labeled corpus.

Three pure functions of label docs (deterministic, integer-only — no
float ever reaches an artifact: every mean is floor division ``sum // n``
(Python floors toward negative infinity), every rate a floor percent):

- SEQUENTIAL miner: PrefixSpan (projected-prefix depth-first) over each
  run's ``chosen`` sequence (decisions ordered by turn; a pattern is a
  SUBSEQUENCE of option ids — gaps allowed, standard sequential-pattern
  semantics), min_support counted in RUNS per corpus side.
- MOTIF miner: each decision's ``root_key`` parsed into an integer
  feature ITEMSET (per-player cities exact, gold bucket / research
  count / unit count on documented threshold ladders, plus the chosen
  option itself); frequent itemsets by Apriori (sorted prefix-join +
  subset prune, depth-bounded), min_support counted in DECISIONS per
  side, each row carrying the mean match differential when present.
- COMPARISONS: one SERIALIZED median-axis row per union itemset (mined
  on the high OR the low side at min_support), carrying BOTH sides'
  supports — including the below-min-support counterpart, flagged
  ``below_min_support`` — so every support number any heuristic row
  cites exists in the artifact itself (Codex M19c C3: heuristics derive
  EXCLUSIVELY from these serialized rows, never from a recompute; the
  sign axis needs no comparison rows because no heuristic cites a
  sign-axis number, and sequential/motif rows already serialize every
  mined side support).
- HEURISTICS: per option a ``context`` row (candidacy stats — floor
  means and a floor percent) plus one ``promote``/``demote`` row per
  comparison row containing ``chosen=<opt>`` whose high-vs-low supports
  differ. Explicit strings only: M20b reads them as candidate context
  features, never as numbers to re-derive.

Corpus unit: one labels.json = one planner RUN. Its diff is the planner
seat's ``match_value_differential``, its sign the seat's
``outcome_sign`` (doc-constant — labels.py stamps both per decision
from the same summary). Runs without decisions or without an integer
differential are SKIPPED and counted, never fabricated as 0: an unknown
outcome cannot condition an outcome-conditioned miner.

Conditioning, TWO axes (each run belongs to one side per axis):
- sign axis: ``wins`` = sign == 1, ``losses`` = sign == -1 (draws count
  in neither sign side).
- median axis: ``median_diff`` = sorted diffs[n // 2] — the UPPER
  middle for even n (an integer, never an averaged float); ``high`` =
  diff >= median (ties go high), ``low`` = diff < median. The exp3
  corpus is WIN-SATURATED (all 240 planner runs won; the losses side is
  empty), so the median axis is the discriminating axis there. Empty
  sides mine to nothing and report support 0 — the side sizes ride in
  the artifact source so 0 is never ambiguous.

Determinism: same inputs -> byte-identical artifact (sorted iteration
everywhere; the artifact is written through canonical()). Read-only
over runs/: the only write is the caller-given artifact path, guarded
against aliasing any input (the labels.py --index-out discipline).

    uv run python scripts/pattern_mining.py \
        --labels-glob 'runs/exp3/b*/planner-*/labels.json' \
        --out /tmp/pattern-mining.json --min-support 3
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from itertools import combinations
from pathlib import Path
from typing import Any

from civ_arena.canonical import atomic_write_text, canonical

SCHEMA = 1

# What one unit of "support" counts, per artifact section — sequential
# support counts RUNS, motif/comparison support counts DECISIONS (the
# two scales differ; serialized in the artifact source, Codex M19c C4).
SUPPORT_UNITS = {"sequential": "runs", "motifs": "decisions",
                 "comparisons": "decisions"}

# Corpus side names, fixed order (mining order; the artifact sorts keys).
SIDES = ("wins", "losses", "high", "low")

# Feature-item threshold ladders (DOCUMENTED bucket boundaries; measured
# on the exp3 corpus — gold_bucket = gold // 25 spans 4..32, researched
# counts 0..8, unit totals 0..28, cities 0..3):
# - gold: >= 8/16/24 buckets == >= 200/400/600 gold,
# - researched: any / mid / deep tech counts at 1/3/5,
# - units: token / standing / large forces at 5/10/20.
# cities stay EXACT (a 0..3 range needs no bucketing). An item fires
# once per threshold CLEARED (monotone ladder), so an itemset reads as
# "at least this level".
GOLD_THRESHOLDS = (8, 16, 24)
RESEARCHED_THRESHOLDS = (1, 3, 5)
UNIT_THRESHOLDS = (5, 10, 20)


# ------------------------------------------------------------- corpus


def feature_items(root_key: str, chosen: str) -> list[str]:
    """One decision's root_key (canonical-JSON text of the search abstract
    doc) -> its integer feature ITEMSET as a sorted-able item list. Loud
    on a malformed doc (KeyError/TypeError propagate — the casebase
    discipline: a silently mis-shaped root_key must never mine a bogus
    motif). ``development`` is ignored (per-city build detail, out of
    scope here)."""
    doc = json.loads(root_key)
    items = [f"chosen={chosen}"]
    for pid in sorted(k for k in doc if k not in ("turn", "candidates")):
        player = doc[pid]
        items.append(f"p{pid}:cities={int(player['cities'])}")
        for threshold in GOLD_THRESHOLDS:
            if player["gold_bucket"] >= threshold:
                items.append(f"p{pid}:gold_bucket>={threshold}")
        researched = len(player["researched"])
        for threshold in RESEARCHED_THRESHOLDS:
            if researched >= threshold:
                items.append(f"p{pid}:researched>={threshold}")
        units = sum(player["units"].values())
        for threshold in UNIT_THRESHOLDS:
            if units >= threshold:
                items.append(f"p{pid}:units>={threshold}")
    return items


def corpus_from_docs(label_docs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Label docs -> the mining corpus: one record per labelable run
    (sequence + diff + sign + per-decision itemsets and candidate menus),
    the median differential, and a skip count. ``match_value_differential``
    and ``outcome_sign`` are read from the FIRST decision — labels.py
    stamps them doc-constant."""
    docs: list[dict[str, Any]] = []
    skipped = 0
    decisions_total = 0
    for label in label_docs:
        decisions = sorted(label.get("decisions", []),
                           key=lambda d: d["turn"])
        diff = decisions[0].get("match_value_differential") \
            if decisions else None
        sign = decisions[0].get("outcome_sign") if decisions else None
        if not decisions or not isinstance(diff, int) \
                or isinstance(diff, bool):
            skipped += 1
            continue
        docs.append({
            "sequence": [d["chosen"] for d in decisions],
            "candidates": [list(d["candidates"]) for d in decisions],
            "diff": diff,
            "sign": sign,
            "itemsets": [frozenset(feature_items(d["root_key"], d["chosen"]))
                         for d in decisions],
        })
        decisions_total += len(decisions)
    diffs = sorted(d["diff"] for d in docs)
    return {
        "docs": docs,
        "median_diff": diffs[len(diffs) // 2] if diffs else None,
        "skipped": skipped,
        "decisions": decisions_total,
    }


def _side_docs(corpus: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Corpus split along both axes (see module docstring)."""
    docs = corpus["docs"]
    median = corpus["median_diff"]
    return {
        "wins": [d for d in docs if d["sign"] == 1],
        "losses": [d for d in docs if d["sign"] == -1],
        "high": [d for d in docs if median is not None
                 and d["diff"] >= median],
        "low": [d for d in docs if median is not None and d["diff"] < median],
    }


def _itemset_agg(docs: list[dict[str, Any]]) -> dict[frozenset[str],
                                                     list[int]]:
    """Distinct decision itemset -> [n_decisions, diff_sum] (identical
    states repeat across the corpus — aggregating first keeps support
    counting linear in DISTINCT itemsets)."""
    agg: dict[frozenset[str], list[int]] = {}
    for doc in docs:
        for iset in doc["itemsets"]:
            rec = agg.setdefault(iset, [0, 0])
            rec[0] += 1
            rec[1] += doc["diff"]
    return agg


# ------------------------------------------------- sequential (PrefixSpan)


def prefixspan(sequences: list[list[str]],
               min_support: int) -> dict[tuple[str, ...], int]:
    """Frequent subsequences -> support (# sequences containing each), by
    projected-prefix depth-first search: extend the prefix by every item
    that clears min_support in the projected database, project past the
    FIRST occurrence per sequence, recurse. One projected entry per
    sequence keeps support == len(projection)."""
    results: dict[tuple[str, ...], int] = {}

    def recurse(prefix: tuple[str, ...],
                db: list[tuple[int, int]]) -> None:
        extensions: dict[str, list[tuple[int, int]]] = {}
        for seq_index, start in db:
            seq = sequences[seq_index]
            seen: set[str] = set()
            for pos in range(start, len(seq)):
                item = seq[pos]
                if item in seen:  # first occurrence per sequence only
                    continue
                seen.add(item)
                extensions.setdefault(item, []).append((seq_index, pos))
        for item in sorted(extensions):
            occ = extensions[item]
            if len(occ) < min_support:
                continue
            pattern = prefix + (item,)
            results[pattern] = len(occ)
            recurse(pattern, [(si, pos + 1) for si, pos in occ])

    recurse((), [(i, 0) for i in range(len(sequences))])
    return results


def mine_sequential(corpus: dict[str, Any],
                    min_support: int) -> list[dict[str, Any]]:
    """PrefixSpan per side; a pattern is reported when it clears
    min_support on at least one side, with its support on ALL four sides
    (0 where absent — side sizes ride in the artifact source)."""
    sides = _side_docs(corpus)
    supports: dict[tuple[str, ...], dict[str, int]] = {}
    for side in SIDES:
        sequences = [doc["sequence"] for doc in sides[side]]
        for pattern, support in prefixspan(sequences,
                                           min_support).items():
            supports.setdefault(pattern, {})[side] = support
    return [{
        "pattern": list(pattern),
        **{f"support_{side}": by_side.get(side, 0) for side in SIDES},
    } for pattern, by_side in sorted(supports.items(),
                                     key=lambda kv: (len(kv[0]), kv[0]))]


# ----------------------------------------------------- motifs (Apriori)


def mine_itemsets(itemsets: Iterable[frozenset[str]]
                  | dict[frozenset[str], int], min_support: int,
                  max_depth: int) -> dict[frozenset[str], int]:
    """Frequent itemsets -> support over the given decision itemsets,
    Apriori: frequent 1-items, then sorted prefix-join candidates
    (``a[:-1] == b[:-1]``) with all-subsets-frequent pruning, depth-
    bounded at max_depth. ``itemsets`` is either a plain iterable (an
    itemset repeated N decisions counts N times) or a pre-aggregated
    mapping itemset -> decision count — Counter handles both; support
    counts DECISIONS, never distinct itemsets."""
    weights = Counter(itemsets)
    support_1: dict[str, int] = {}
    for iset, n in weights.items():
        for item in iset:
            support_1[item] = support_1.get(item, 0) + n
    results: dict[frozenset[str], int] = {}
    level = sorted((frozenset((item,)) for item, n in support_1.items()
                    if n >= min_support), key=lambda s: sorted(s))
    for iset in level:
        results[iset] = support_1[next(iter(iset))]
    depth = 1
    while level and depth < max_depth:
        depth += 1
        tuples = sorted(tuple(sorted(iset)) for iset in level)
        nxt: dict[frozenset[str], int] = {}
        for a, b in combinations(tuples, 2):
            if a[:-1] != b[:-1]:
                continue
            cand = a + (b[-1],)
            if any(frozenset(sub) not in results
                   for sub in combinations(cand, len(cand) - 1)):
                continue
            iset = frozenset(cand)
            support = sum(n for other, n in weights.items() if iset <= other)
            if support >= min_support:
                nxt[iset] = support
        results.update(nxt)
        level = sorted(nxt, key=lambda s: sorted(s))
    return results


def mine_motifs(corpus: dict[str, Any], min_support: int,
                max_depth: int) -> list[dict[str, Any]]:
    """Frequent feature itemsets per corpus side; support in decisions of
    that side, plus the mean match differential across the containing
    decisions (integer floor division)."""
    rows: list[dict[str, Any]] = []
    for side in SIDES:
        docs = _side_docs(corpus)[side]
        if not docs:
            continue
        agg = _itemset_agg(docs)
        mined = mine_itemsets({iset: rec[0] for iset, rec in agg.items()},
                              min_support, max_depth)
        for iset in sorted(mined, key=lambda s: (len(s), sorted(s))):
            containing = [rec for other, rec in agg.items() if iset <= other]
            support = sum(rec[0] for rec in containing)
            diff_sum = sum(rec[1] for rec in containing)
            rows.append({
                "side": side,
                "items": sorted(iset),
                "support": support,
                "mean_diff_when_present": diff_sum // support,
            })
    rows.sort(key=lambda r: (r["side"], len(r["items"]), r["items"]))
    return rows


# ------------------------------------------------------------ heuristics


def build_comparisons(corpus: dict[str, Any], min_support: int,
                      max_depth: int) -> list[dict[str, Any]]:
    """Serialized median-axis evidence: one row per UNION itemset (mined
    on the high OR the low side at min_support), with BOTH sides'
    supports — the below-min-support counterpart included and flagged
    ``below_min_support`` so consumers can filter weak-comparison rows
    while the numbers stay serialized (Codex M19c C3). Heuristics cite
    ONLY these rows; support units are decisions (the motif scale)."""
    sides = _side_docs(corpus)
    agg_by_side = {side: _itemset_agg(sides[side])
                   for side in ("high", "low")}
    union: set[frozenset[str]] = set()
    for side in ("high", "low"):
        if agg_by_side[side]:
            union.update(mine_itemsets(
                {iset: rec[0] for iset, rec in agg_by_side[side].items()},
                min_support, max_depth))
    rows: list[dict[str, Any]] = []
    for iset in sorted(union, key=lambda s: (len(s), sorted(s))):
        supports = {
            side: sum(rec[0] for other, rec in agg_by_side[side].items()
                      if iset <= other)
            for side in ("high", "low")
        }
        rows.append({
            "items": sorted(iset),
            "support_high": supports["high"],
            "support_low": supports["low"],
            "below_min_support": any(n < min_support
                                     for n in supports.values()),
        })
    return rows


def build_heuristics(corpus: dict[str, Any],
                     comparisons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """M20b-consumable rows — explicit strings only ({option, evidence,
    kind, detail}; kind in promote/demote/context):

    - one ``context`` row per option ever candidate or chosen: candidacy
      stats over the WHOLE corpus — candidate count, chosen count, a
      floor taken-rate percent, mean diff when taken, and mean diff when
      on the menu but NOT taken (the candidacies are the A/B contrast;
      both means floor division). Candidacy counts are not supports —
      they are serialized in the row itself.
    - one ``promote`` (support_high > support_low) or ``demote`` (<) row
      per SERIALIZED comparison row that contains ``chosen=<opt>`` and
      whose high-vs-low supports differ — derived EXCLUSIVELY from the
      ``comparisons`` rows exactly as they appear in the artifact, so
      every cited support number is traceable to a serialized row.

    Options with zero discriminating itemsets keep just their context
    row — absence of evidence is reported, never invented."""
    docs = corpus["docs"]
    options = sorted({oid for doc in docs
                      for menu in doc["candidates"] for oid in menu}
                     | {c for doc in docs for c in doc["sequence"]})
    acc = {oid: {"cand": 0, "taken": 0, "taken_sum": 0, "untaken_sum": 0,
                 "untaken_n": 0} for oid in options}
    for doc in docs:
        for menu, chosen in zip(doc["candidates"], doc["sequence"],
                                strict=True):
            for oid in menu:
                rec = acc[oid]
                rec["cand"] += 1
                if oid == chosen:
                    rec["taken"] += 1
                    rec["taken_sum"] += doc["diff"]
                else:
                    rec["untaken_n"] += 1
                    rec["untaken_sum"] += doc["diff"]

    def _mean(total: int, n: int) -> str:
        return str(total // n) if n else "none"

    rows: list[dict[str, Any]] = []
    for oid in options:
        rec = acc[oid]
        rate = rec["taken"] * 100 // rec["cand"] if rec["cand"] else 0
        rows.append({
            "option": oid,
            "evidence": "candidacy",
            "kind": "context",
            "detail": (
                f"candidate={rec['cand']} chosen={rec['taken']} "
                f"taken_rate_pct={rate} "
                f"mean_diff_when_taken={_mean(rec['taken_sum'], rec['taken'])}"
                " mean_diff_when_candidate_untaken="
                f"{_mean(rec['untaken_sum'], rec['untaken_n'])}"),
        })

    discriminating: list[tuple[str, int, int, list[str]]] = []
    for comp in comparisons:
        high, low = comp["support_high"], comp["support_low"]
        if high == low:
            continue
        chosen_item = next((i for i in comp["items"]
                            if i.startswith("chosen=")), None)
        if chosen_item is None:
            continue
        discriminating.append((chosen_item[len("chosen="):], high, low,
                               comp["items"]))
    for oid, high, low, items in sorted(
            discriminating,
            key=lambda t: (t[0], -abs(t[1] - t[2]), t[3])):
        rows.append({
            "option": oid,
            "evidence": "itemset " + ",".join(items),
            "kind": "promote" if high > low else "demote",
            "detail": f"support_high={high} support_low={low}",
        })
    return rows


# ------------------------------------------------------------- artifact


def build_artifact(label_docs: Iterable[dict[str, Any]], *, source: dict,
                   min_support: int, max_itemset_depth: int) -> dict:
    """The one artifact: {schema, source (+ support_units + axis sizes),
    sequential, motifs, comparisons, heuristics}. ``source``
    (glob/docs/params) is the caller's provenance block — the CLI
    supplies it; tests pass their own. Heuristics are derived from the
    SERIALIZED comparisons rows (C3 traceability)."""
    if min_support < 1:
        raise ValueError(f"min_support must be >= 1, got {min_support}")
    if max_itemset_depth < 1:
        raise ValueError(f"max_itemset_depth must be >= 1, "
                         f"got {max_itemset_depth}")
    corpus = corpus_from_docs(label_docs)
    sides = _side_docs(corpus)
    comparisons = build_comparisons(corpus, min_support, max_itemset_depth)
    return {
        "schema": SCHEMA,
        "source": {**source, "support_units": SUPPORT_UNITS, "axis": {
            "docs": len(corpus["docs"]),
            "skipped_docs": corpus["skipped"],
            "decisions": corpus["decisions"],
            "median_diff": corpus["median_diff"],
            **{f"docs_{side}": len(sides[side]) for side in SIDES},
        }},
        "sequential": mine_sequential(corpus, min_support),
        "motifs": mine_motifs(corpus, min_support, max_itemset_depth),
        "comparisons": comparisons,
        "heuristics": build_heuristics(corpus, comparisons),
    }


def write_artifact(out: Path, artifact: dict[str, Any]) -> None:
    """Atomic tmp+replace via canonical.atomic_write_text — mkstemp is
    O_EXCL, so a symlink or hardlink planted at any predictable tmp name
    can never be written through (Codex M19c C1); canonical text refuses
    a float-bearing artifact at write time, never on disk."""
    atomic_write_text(out, canonical(artifact) + "\n")


def assert_out_safe(out: Path, label_paths: list[Path]) -> None:
    """--out must never clobber an input (the labels.py --index-out
    discipline): the resolved out path is checked against every mined
    labels.json AND every protected run artifact beside it — directly,
    via ``..``, or through a symlink. Each label path is RESOLVED before
    its siblings are built: an input labels.json that is itself a
    symlink to ../<run>/labels.json relocates its whole run directory,
    and guarding the UNRESOLVED parent would check aliases beside the
    link instead of beside the target (Codex M19c C2)."""
    from civ_arena.labels import PROTECTED_ARTIFACTS

    resolved_out = Path(out).resolve()
    for path in label_paths:
        resolved_label = path.resolve()
        for target in [resolved_label,
                       *(resolved_label.parent / rel
                         for rel in PROTECTED_ARTIFACTS)]:
            if target.resolve() == resolved_out:
                raise SystemExit(f"error: --out {out} would overwrite the "
                                 f"run artifact {target} — refusing")
