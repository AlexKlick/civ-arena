"""M19c: offline pattern mining over the labeled corpus.

Pinned: PrefixSpan equals exhaustive subsequence enumeration on a small
fixture (supports included); mining is deterministic (byte-identical
artifact twice, CLI included); the CLI is read-only over the corpus
(only --out appears, input digests unchanged); --out aliasing an input
refuses with SystemExit BEFORE any write; a win-saturated corpus (every
sign +1 — the real exp3 shape) still discriminates through the median
axis (non-empty high/low split, promote AND demote heuristics); the
artifact is canonical and float-free (a poisoned float refuses); every
heuristic row serializes with str/int/list values only; and
option_mining's budget now reads the trace's own ``budget`` field
(-1 preserved for budget-less traces).
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from civ_arena.canonical import CanonicalError, canonical
from civ_arena.planner import mining

REPO = Path(__file__).resolve().parents[1]


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ------------------------------------------------------ synthetic corpus


def _root_key(turn: int, candidates: list[str], *, cities: int = 1,
              gold_bucket: int = 6,
              researched: tuple[str, ...] = ("POTTERY",)) -> str:
    """A schema-shaped abstract doc (canonical text, like the trace's own
    root_key): two symmetric players, parameterized state."""
    player = {
        "cities": cities, "population": cities * 3,
        "gold_bucket": gold_bucket, "researched": list(researched),
        "researching": researched[0], "units": {"WARRIOR": 3},
        "development": [],
    }
    return canonical({"turn": turn, "candidates": list(candidates),
                      "0": dict(player), "1": dict(player)})


def _labels_doc(match_id: str, seq: list[tuple[str, list[str]]], diff: int,
                sign: int = 1, **state: int) -> dict[str, Any]:
    """A synthetic M19a label doc: seq is (chosen, candidates) per turn,
    diff/sign stamped doc-constant on every decision (the labels.py
    shape)."""
    decisions = [{
        "seat": 0, "turn": turn, "root_key": _root_key(turn, cands, **state),
        "candidates": list(cands), "chosen": chosen, "prior": [],
        "root_visits": {}, "root_values": {}, "method": "mcgs", "budget": 8,
        "match_value_differential": diff, "outcome_sign": sign,
    } for turn, (chosen, cands) in enumerate(seq, start=1)]
    return {
        "schema": 1, "match_id": match_id, "source": {}, "weights": {},
        "outcome": {"final_turn": len(seq), "aborted": None,
                    "violations_total": 0, "seats": [], "score_vectors": {},
                    "value_differential": {}, "outcome_sign": {}},
        "decisions": decisions, "claims": [],
    }


# win-saturated like the real exp3 corpus (every sign +1); high runs
# build wide+rich (gold_bucket 10 clears the >= 8 ladder) and take the
# expand->economy->tech_race->expand line, low runs do neither
HIGH_SEQ = [("expand", ["expand", "scout_frontier"]),
            ("economy", ["economy", "expand"]),
            ("tech_race", ["tech_race", "economy"]),
            ("expand", ["expand", "tech_race"])]
LOW_SEQ = [("scout_frontier", ["scout_frontier", "defend"]),
           ("defend", ["defend", "economy"]),
           ("defend", ["defend", "expand"]),
           ("economy", ["economy", "scout_frontier"])]
# diffs sorted [100, 200, 300, 450, 500, 600]: median (upper middle,
# ties-to-high) = 450 -> high = {450, 500, 600}, low = {100, 200, 300}
CORPUS_SPECS = [
    ("run-h1", HIGH_SEQ, 600, {"gold_bucket": 10, "cities": 2}),
    ("run-h2", HIGH_SEQ, 500, {"gold_bucket": 10, "cities": 2}),
    ("run-h3", HIGH_SEQ, 450, {"gold_bucket": 10, "cities": 2}),
    ("run-l1", LOW_SEQ, 300, {"gold_bucket": 5, "cities": 1}),
    ("run-l2", LOW_SEQ, 200, {"gold_bucket": 5, "cities": 1}),
    ("run-l3", LOW_SEQ, 100, {"gold_bucket": 5, "cities": 1}),
]


def _write_corpus(runs_root: Path) -> list[Path]:
    """Six synthetic label docs on disk (win-saturated, 3v3 median split)."""
    paths: list[Path] = []
    for name, seq, diff, state in CORPUS_SPECS:
        run_dir = runs_root / name
        run_dir.mkdir(parents=True)
        path = run_dir / "labels.json"
        path.write_text(json.dumps(_labels_doc(name, seq, diff, **state)))
        paths.append(path)
    return paths


def _docs(paths: list[Path]) -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths]


def _build(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return mining.build_artifact(
        docs, source={"glob": "synthetic", "docs": [], "min_support": 3,
                      "max_itemset_depth": 3},
        min_support=3, max_itemset_depth=3)


def _tree_digests(root: Path) -> dict[Path, str]:
    return {p: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _only_str_int_list(node: Any) -> bool:
    if isinstance(node, str):
        return True
    if isinstance(node, int) and not isinstance(node, bool):
        return True
    if isinstance(node, list):
        return all(_only_str_int_list(item) for item in node)
    if isinstance(node, dict):
        return all(isinstance(k, str) and _only_str_int_list(v)
                   for k, v in node.items())
    return False


# ---------------------------------------------------------------- tests


def test_prefixspan_matches_bruteforce_on_small_fixture() -> None:
    """Oracle: exhaustive subsequence enumeration (every bitmask over each
    sequence, support = sequences containing the pattern). Whatever
    support threshold, PrefixSpan must return EXACTLY the brute-force set
    with identical supports."""
    sequences = [
        ["expand", "economy", "expand"],
        ["economy", "rush"],
        ["expand", "rush", "economy", "rush"],
        ["defend", "expand"],
        ["economy", "economy", "defend"],
    ]

    def brute_force(min_support: int) -> dict[tuple[str, ...], int]:
        supports: dict[tuple[str, ...], int] = {}
        for seq in sequences:
            n = len(seq)
            subs = {tuple(seq[i] for i in range(n) if mask >> i & 1)
                    for mask in range(1, 1 << n)}
            for pattern in subs:
                supports[pattern] = supports.get(pattern, 0) + 1
        return {p: c for p, c in supports.items() if c >= min_support}

    for k in (1, 2, 3, 4):
        assert mining.prefixspan(sequences, k) == brute_force(k), k


def test_mining_deterministic(tmp_path: Path) -> None:
    paths = _write_corpus(tmp_path / "runs")
    pm = _load("pattern_mining")
    argv = ["--labels-glob", str(tmp_path / "runs" / "*" / "labels.json"),
            "--min-support", "3"]
    pm.main([*argv, "--out", str(tmp_path / "out1.json")])
    pm.main([*argv, "--out", str(tmp_path / "out2.json")])
    first = (tmp_path / "out1.json").read_bytes()
    second = (tmp_path / "out2.json").read_bytes()
    assert first == second  # byte-identical across two full CLI runs
    assert first  # non-trivial: the corpus mines something

    docs = _docs(paths)
    assert canonical(_build(docs)) == canonical(_build(docs))  # in-process


def test_mining_readonly_over_runs(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    _write_corpus(runs_root)
    out = tmp_path / "mining.json"
    before = _tree_digests(tmp_path)
    _load("pattern_mining").main([
        "--labels-glob", str(runs_root / "*" / "labels.json"),
        "--out", str(out), "--min-support", "3"])
    after = _tree_digests(tmp_path)
    # exactly one new file (the artifact), every input byte-identical
    assert set(after) - set(before) == {out}
    assert {p: d for p, d in after.items() if p != out} == before
    assert json.loads(out.read_text())["schema"] == 1


def test_out_alias_refused(tmp_path: Path) -> None:
    paths = _write_corpus(tmp_path / "runs")
    target = paths[0]  # a labels.json INPUT
    before = _tree_digests(tmp_path)
    with pytest.raises(SystemExit, match="would overwrite"):
        _load("pattern_mining").main([
            "--labels-glob", str(tmp_path / "runs" / "*" / "labels.json"),
            "--out", str(target), "--min-support", "3"])
    assert _tree_digests(tmp_path) == before  # refused BEFORE any write
    # a sibling protected artifact is equally refused (same discipline)
    with pytest.raises(SystemExit, match="would overwrite"):
        _load("pattern_mining").main([
            "--labels-glob", str(tmp_path / "runs" / "*" / "labels.json"),
            "--out", str(paths[0].parent / "summary.json"),
            "--min-support", "3"])
    # a genuinely separate path passes
    _load("pattern_mining").main([
        "--labels-glob", str(tmp_path / "runs" / "*" / "labels.json"),
        "--out", str(tmp_path / "ok.json"), "--min-support", "3"])


def test_win_saturated_corpus_uses_median_axis(tmp_path: Path) -> None:
    """The real corpus shape: every planner run won (sign +1 everywhere —
    the losses side is empty and the sign axis cannot discriminate), so
    the median axis carries the split — non-empty high/low sides, patterns
    and motifs that separate them, and both promote and demote rows."""
    artifact = _build(_docs(_write_corpus(tmp_path / "runs")))
    axis = artifact["source"]["axis"]
    assert axis["docs_wins"] == 6 and axis["docs_losses"] == 0
    assert axis["docs_high"] == 3 and axis["docs_low"] == 3  # ties high
    assert axis["median_diff"] == 450  # sorted[3] of [100..600]

    by_pattern = {tuple(row["pattern"]): row
                  for row in artifact["sequential"]}
    high_line = by_pattern[("expand", "economy", "tech_race", "expand")]
    assert high_line["support_high"] == 3 and high_line["support_low"] == 0
    assert high_line["support_wins"] == 3  # high side == wins here
    assert high_line["support_losses"] == 0  # empty side reports 0

    motifs = {(row["side"], tuple(row["items"])): row
              for row in artifact["motifs"]}
    expand_rich = motifs[("high", ("chosen=expand", "p0:gold_bucket>=8"))]
    assert expand_rich["support"] == 6  # 2 expand takens x 3 high runs
    assert expand_rich["mean_diff_when_present"] == (600 + 500 + 450) * 2 // 6

    rows = artifact["heuristics"]
    kinds = {(row["option"], row["kind"]) for row in rows}
    assert ("expand", "promote") in kinds
    assert ("defend", "demote") in kinds
    for option in ("expand", "economy", "tech_race", "scout_frontier",
                   "defend"):
        assert (option, "context") in kinds


def test_budget_of_reads_trace(tmp_path: Path) -> None:
    """option_mining's row budget comes from the trace's own per-entry
    ``budget`` field (trace[0]); a trace without it keeps -1."""
    om = _load("option_mining")

    def _root(name: str, entries: list[dict[str, Any]]) -> Path:
        run_dir = tmp_path / name
        (run_dir / "planner").mkdir(parents=True)
        (run_dir / "planner" / "trace.json").write_text(json.dumps(entries))
        (run_dir / "summary.json").write_text(json.dumps({
            "final_turn": len(entries),
            "scores": {
                "ROME": {"player_id": 0, "cities": 2, "gold": 46,
                         "population": 9, "techs": 8, "units": 17},
                "KOREA": {"player_id": 1, "cities": 3, "gold": 64,
                          "population": 16, "techs": 8, "units": 25},
            }}))
        return run_dir  # both arms land under tmp_path, one glob root

    def _entry(**extra: Any) -> dict[str, Any]:
        return {"turn": 1, "root_key": "{}", "candidates": ["expand"],
                "chosen": "expand", "method": "mcgs", **extra}

    _root("planner-mcgs-s1-p0",
          [_entry(budget=16), _entry(turn=2, budget=16)])
    _root("planner-mcgs-s2-p0", [_entry()])  # no budget field
    # both arms under the same root: one glob sees both, rows keyed by
    # match (mine_root's own discipline — the glob, not the arg, scopes)
    rows = {row["match"]: row for row in om.mine_root(tmp_path)}
    assert rows["planner-mcgs-s1-p0"]["budget"] == 16
    assert rows["planner-mcgs-s2-p0"]["budget"] == -1  # budget-less keeps -1


def test_no_floats_in_artifact(tmp_path: Path) -> None:
    artifact = _build(_docs(_write_corpus(tmp_path / "runs")))
    text = canonical(artifact)  # a real, float-free artifact round-trips
    assert json.loads(text) == artifact
    poisoned = copy.deepcopy(artifact)
    poisoned["sequential"][0]["support_high"] = 1.5
    with pytest.raises(CanonicalError):
        canonical(poisoned)


def test_heuristics_rows_are_strings(tmp_path: Path) -> None:
    artifact = _build(_docs(_write_corpus(tmp_path / "runs")))
    assert artifact["heuristics"]
    for row in artifact["heuristics"]:
        assert set(row) == {"option", "evidence", "kind", "detail"}
        assert row["kind"] in ("promote", "demote", "context")
        assert _only_str_int_list(json.loads(json.dumps(row))), row
        assert all(isinstance(value, str) for value in row.values())
