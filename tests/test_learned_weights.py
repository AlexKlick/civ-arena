"""M20c: the learned value head — offline fit, purity, and the seam.

Pinned: numpy stays a DEV dependency (no module under src/civ_arena ever
imports it — AST-checked, and pyproject keeps it out of the runtime
dependency group); the weights seam is live at the search's leaf
evaluation (a fake weights dict measurably moves root_values) while
``weights=None`` is bit-identical to DEFAULT_WEIGHTS (canonical trace
docs byte-equal); the fitter is DETERMINISTIC (two independent corpus
builds -> byte-identical artifacts), index-provenanced, and quantizes to
integers at the DEFAULT L1 scale with sum|w| == 161 EXACTLY; the
committed artifact parses, carries exactly the five integer components
at that exact scale, records the UCT-C declared limitation, and is
canonical on disk; the PlannerRuntime ``weights`` kwarg actually REACHES
search_option; and the experiment runner's artifact loader holds its
contract and its validity gate refuses inference on polluted rows.

NOTE on signs: the committed artifact is the REAL production corpus fit
— its gold weight floors to 0 and its units weight is negative, so the
artifact contract here pins INTS and the exact L1 scale, not positivity
(the original lane wording assumed positive ints; the measured corpus
overruled it — ints of any sign at exactly DEFAULT magnitude are the
shippable contract).

The synthetic corpus generator (``write_synthetic_corpus``) produces the
M19a shape INCLUDING a corpus index (run dir -> labels byte-hash), so
the determinism test exercises the full provenanced path.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import random
import re
import tomllib
from pathlib import Path

import pytest

from civ_arena.canonical import canonical
from civ_arena.game.sim.layouts import duel_start
from civ_arena.game.sim.state import TECHS, SimState
from civ_arena.game.sim.value import (
    DEFAULT_WEIGHTS,
    SCORE_COMPONENTS,
    score_vector,
    value_of,
)
from civ_arena.planner.belief import PlannerBelief
from civ_arena.planner.runtime import PlannerRuntime
from civ_arena.planner.search import search_option
from test_action_dag import FakeFacade, feed_via
from test_planner import playout

REPO = Path(__file__).resolve().parents[1]

# a weights head that ONLY counts cities — maximally far from DEFAULT
CITIES_ONLY = {"cities": 9999, "population": 0, "gold": 0, "techs": 0,
               "units": 0}


def _fit_module():
    """scripts/fit_weights.py loaded by path (the bandit_experiment
    planner_analyze pattern) — the fit lives in scripts/, never in src/."""
    spec = importlib.util.spec_from_file_location(
        "fit_weights", REPO / "scripts" / "fit_weights.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _runner_module():
    spec = importlib.util.spec_from_file_location(
        "weights_experiment", REPO / "scripts" / "weights_experiment.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ------------------------------------------------------------- purity


def test_no_numpy_in_src() -> None:
    """numpy is a DEV dependency (the offline fitter only): no module
    under src/civ_arena may import it — AST-walked (Codex M20c C9: a
    line-regex is bypassable), and pyproject keeps numpy OUT of the
    runtime dependency group while declaring it in dev."""
    offenders: list[str] = []
    for path in sorted((REPO / "src" / "civ_arena").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"),
                         filename=str(path))
        for node in ast.walk(tree):
            roots: list[str] = []
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots = [(node.module or "").split(".")[0]]
            if "numpy" in roots:
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert offenders == [], f"numpy leaked into the runtime tree: {offenders}"

    def dep_names(raw: list[str]) -> set[str]:
        return {re.match(r"[A-Za-z0-9_.\-]+", d).group() for d in raw}  # type: ignore[union-attr]

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text("utf-8"))
    runtime = dep_names(pyproject["project"]["dependencies"])
    dev = dep_names(pyproject["dependency-groups"]["dev"])
    assert "numpy" not in runtime, "numpy must never be a runtime dependency"
    assert "numpy" in dev, "numpy must be declared in the dev group"


# ---------------------------------------------------------- the seam


async def test_value_weights_seam_threaded() -> None:
    """A fake weights dict measurably changes search_option's root_values
    at a small budget, and value_of respects weights directly."""
    state = SimState.from_doc(duel_start(21))
    facade = FakeFacade(state, 0)
    belief = PlannerBelief(0)
    await feed_via(facade, belief)
    default = search_option(belief, 0, method="mcts", budget=8, seed=4)
    cities_only = search_option(belief, 0, method="mcts", budget=8, seed=4,
                                weights=CITIES_ONLY)
    assert default.root_values != cities_only.root_values, (
        "the weights kwarg did not move the search's leaf evaluation")

    # value_of respects weights directly: own-minus-rival under the fake
    # head is exactly the cities delta scaled
    s = playout(11, 6)
    own, rival = score_vector(s, 0), score_vector(s, 1)
    assert value_of(s, 0, CITIES_ONLY) == \
        CITIES_ONLY["cities"] * (own["cities"] - rival["cities"])


async def test_default_weights_behavior_unchanged() -> None:
    """weights=None is EXACTLY DEFAULT_WEIGHTS: byte-equal canonical trace
    docs from the search, and identical value_of on a played state."""
    state = SimState.from_doc(duel_start(21))
    facade = FakeFacade(state, 0)
    belief = PlannerBelief(0)
    await feed_via(facade, belief)
    for method in ("mcts", "mcgs"):
        none_w = search_option(belief, 0, method=method, budget=8, seed=4)
        default_w = search_option(belief, 0, method=method, budget=8, seed=4,
                                  weights=dict(DEFAULT_WEIGHTS))
        assert canonical(none_w.to_doc()) == canonical(default_w.to_doc())
    s = playout(13, 5)
    assert value_of(s, 0) == value_of(s, 0, dict(DEFAULT_WEIGHTS))


async def test_runtime_weights_kwarg_reaches_search() -> None:
    """PlannerRuntime(weights=...) threads into search_option: the first
    trace entry's root_values differ from the default runtime's at the
    same seed and world."""

    async def first_root_values(weights: dict | None) -> dict:
        bot = PlannerRuntime(0, 7, budget=4, weights=weights)
        await bot.take_turn(FakeFacade(SimState.from_doc(duel_start(21)), 0))
        assert bot.trace, "no search decision was recorded"
        return bot.trace[0]["root_values"]

    loaded = await first_root_values(CITIES_ONLY)
    default = await first_root_values(None)
    assert loaded != default, (
        "PlannerRuntime(weights=...) never reached search_option")


# ------------------------------------------------------------- fitter

# noise-free ground truth: y = DEFAULT_WEIGHTS . x — the recovered
# integers come back on the DEFAULT scale
_SYNTH_TRUE_W = dict(DEFAULT_WEIGHTS)


def _hex64(rng: random.Random) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(64))


def write_synthetic_corpus(root: Path) -> tuple[list[Path], Path]:
    """A deterministic 6-doc M19a-shaped corpus: b8/b16 (train) and b32
    (validation) under ``root``, labels exactly linear in the deltas, plus
    the corpus INDEX (run dir -> labels byte-hash) the fitter's
    provenance gate requires (Codex M20c C1)."""
    rng = random.Random(20260902)
    paths: list[Path] = []
    index: dict[str, dict] = {}
    for batch, count in (("b8", 2), ("b16", 2), ("b32", 2)):
        for i in range(count):
            decisions = []
            for turn in range(1, 13):
                delta = {
                    "cities": rng.randrange(-2, 3),
                    "population": rng.randrange(-10, 11),
                    "gold_bucket": rng.randrange(-4, 5),
                    "techs": rng.randrange(-3, 4),
                    "units": rng.randrange(-3, 4),
                }
                own = {
                    "cities": rng.randrange(1, 5),
                    "population": rng.randrange(4, 25),
                    "gold_bucket": rng.randrange(2, 12),
                    "techs": rng.randrange(0, 6),
                    "units": rng.randrange(1, 6),
                }

                def block(cities: int, population: int, gold_bucket: int,
                          techs: int, units: int) -> dict:
                    return {
                        "cities": cities, "population": population,
                        "gold_bucket": gold_bucket,
                        "researched": sorted(TECHS)[:techs],
                        "researching": "",
                        "units": {"SCOUT": 1, "WARRIOR": units},
                        "development": [],
                    }

                own_block = block(own["cities"], own["population"],
                                  own["gold_bucket"], own["techs"],
                                  own["units"])
                rival_block = block(
                    max(0, own["cities"] - delta["cities"]),
                    max(0, own["population"] - delta["population"]),
                    max(0, own["gold_bucket"] - delta["gold_bucket"]),
                    max(0, own["techs"] - delta["techs"]),
                    max(0, own["units"] - delta["units"]))
                root_key = canonical({
                    "0": own_block, "1": rival_block,
                    "candidates": ["expand", "tech_race"], "turn": turn})
                # labels over the REALIZED deltas (the clamp can bite), so
                # y is exactly linear in the row the fitter will parse —
                # gold on the fitter's RAW-gold scale (bucket delta * 25)
                realized = {
                    "cities": own_block["cities"] - rival_block["cities"],
                    "population": own_block["population"]
                    - rival_block["population"],
                    "gold": (own_block["gold_bucket"]
                             - rival_block["gold_bucket"]) * 25,
                    "techs": len(own_block["researched"])
                    - len(rival_block["researched"]),
                    "units": sum(own_block["units"].values())
                    - sum(rival_block["units"].values()),
                }
                label = sum(_SYNTH_TRUE_W[k] * realized[k]
                            for k in SCORE_COMPONENTS)
                decisions.append({
                    "seat": 0, "turn": turn, "root_key": root_key,
                    "candidates": ["expand", "tech_race"], "chosen": "expand",
                    "prior": [], "root_visits": {"expand": 4, "tech_race": 4},
                    "root_values": {"expand": label, "tech_race": 0},
                    "method": "mcts", "budget": 8,
                    "match_value_differential": label, "outcome_sign": 1,
                })
            run_dir = root / batch / f"planner-synth-{batch}-{i}"
            run_dir.mkdir(parents=True)
            doc = {
                "schema": 1, "match_id": f"planner-synth-{batch}-{i}",
                "source": {"events_sha256": _hex64(rng),
                           "summary_sha256": _hex64(rng),
                           "trace_sha256": _hex64(rng)},
                "weights": dict(DEFAULT_WEIGHTS),
                "decisions": decisions,
            }
            labels = run_dir / "labels.json"
            raw = (canonical(doc) + "\n").encode("utf-8")
            labels.write_bytes(raw)
            paths.append(labels)
            index[str(run_dir)] = {
                "match_id": doc["match_id"],
                "labels_sha256": hashlib.sha256(raw).hexdigest(),
                "decisions": len(decisions), "claims": 0,
            }
    index_path = root / "labels-index.json"
    index_path.write_text(canonical(index) + "\n", encoding="utf-8")
    return paths, index_path


def test_fit_weights_deterministic_and_quantized(tmp_path) -> None:
    """Fitting the SAME corpus twice -> byte-identical artifacts; every
    weight an int; sum|w| EXACTLY the DEFAULT L1 norm (161); the split
    accounting and the UCT-C declared limitation are recorded; an
    unindexed corpus and a tampered labels doc both refuse (provenance
    gate). A corpus rebuilt at a DIFFERENT path yields identical labels
    bytes and an artifact equal in every path-free field (the manifest
    binds run paths BY DESIGN — Codex M20c C1)."""
    fit = _fit_module()
    root = tmp_path / "corpus"
    paths, index = write_synthetic_corpus(root)
    glob = str(root / "b*" / "planner-*" / "labels.json")
    out_a, out_b = tmp_path / "weights-a.json", tmp_path / "weights-b.json"
    fit.main(["--labels-glob", glob, "--out", str(out_a),
              "--index", str(index)])
    fit.main(["--labels-glob", glob, "--out", str(out_b),
              "--index", str(index)])

    bytes_a, bytes_b = out_a.read_bytes(), out_b.read_bytes()
    assert bytes_a == bytes_b, "the fit is not deterministic"
    doc = json.loads(bytes_a)
    assert doc["schema"] == 1
    assert bytes_a.decode("utf-8") == canonical(doc) + "\n"
    assert doc["uct_c_note"] == fit.UCT_C_NOTE

    weights = doc["weights"]
    assert set(weights) == set(SCORE_COMPONENTS)
    assert all(isinstance(v, int) and not isinstance(v, bool)
               for v in weights.values()), "weights must be integers"
    target = sum(abs(v) for v in DEFAULT_WEIGHTS.values())
    assert sum(abs(v) for v in weights.values()) == target, (
        "sum|w| must pin the DEFAULT L1 norm EXACTLY (Codex M20c C7)")

    fit_meta = doc["fit"]
    assert fit_meta["scale_norm"] == "l1_161"
    assert isinstance(fit_meta["rows"], int) and fit_meta["rows"] == 6 * 12
    assert isinstance(fit_meta["lambda"], int)
    assert isinstance(fit_meta["val_mse_fixed"], int)
    assert fit_meta["refit"] == "all_rows_at_selected_lambda"
    split = fit_meta["split"]
    assert split == {"train_docs": 4, "val_docs": 2,
                     "train_rows": 4 * 12, "val_rows": 2 * 12}
    assert fit_meta["rows"] == split["train_rows"] + split["val_rows"]
    corpus = fit_meta["corpus"]
    assert corpus["docs"] == 6
    assert len(corpus["indexes"]) == 1 and corpus["indexes"][0][
        "sha256"] == hashlib.sha256(index.read_bytes()).hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", corpus["sha256"])

    # generator determinism + path-free artifact equality
    root2 = tmp_path / "corpus-2"
    paths2, index2 = write_synthetic_corpus(root2)
    assert [p.read_bytes() for p in paths2] == [p.read_bytes() for p in paths]
    out_c = tmp_path / "weights-c.json"
    fit.main(["--labels-glob", str(root2 / "b*" / "planner-*" / "labels.json"),
              "--out", str(out_c), "--index", str(index2)])
    doc_c = json.loads(out_c.read_bytes())
    for path_field in (("corpus", "indexes"), ("corpus", "sha256")):
        node_a = doc["fit"]
        node_c = doc_c["fit"]
        for key in path_field:
            node_a[key] = node_c[key] = None
    assert doc_c == doc, ("artifacts at different corpus paths differ "
                          "beyond the recorded path fields")

    # provenance gate: a corpus with NO index refuses; a tampered labels
    # doc (digest mismatch) refuses
    bare_root = tmp_path / "corpus-bare"
    write_synthetic_corpus(bare_root)
    with pytest.raises(SystemExit):
        fit.main(["--labels-glob", str(bare_root / "b*" / "planner-*"
                                       / "labels.json"),
                  "--out", str(tmp_path / "never.json")])
    tamper_root = tmp_path / "corpus-tamper"
    tamper_paths, tamper_index = write_synthetic_corpus(tamper_root)
    tampered = json.loads(tamper_paths[0].read_text())
    tampered["decisions"][0]["match_value_differential"] += 1
    tamper_paths[0].write_text(canonical(tampered) + "\n")
    with pytest.raises(SystemExit):
        fit.main(["--labels-glob", str(tamper_root / "b*" / "planner-*"
                                       / "labels.json"),
                  "--out", str(tmp_path / "never.json"),
                  "--index", str(tamper_index)])
    # clobber guard (Codex M20c C5): --out refusing to alias an --index
    # file and a protected run artifact beside a matched doc
    with pytest.raises(SystemExit):
        fit.main(["--labels-glob", glob, "--out", str(index),
                  "--index", str(index)])
    with pytest.raises(SystemExit):
        fit.main(["--labels-glob", glob, "--out", str(paths[0]),
                  "--index", str(index)])


def test_learned_artifact_valid() -> None:
    """The committed artifact parses, carries the five components as ints
    at EXACTLY the DEFAULT L1 scale, records the UCT-C declared
    limitation and the split accounting, and is canonical on disk. Signs
    are NOT pinned: the production corpus fit ships gold=0 and a negative
    units weight — ints at the DEFAULT magnitude are the contract."""
    path = REPO / "configs" / "learned-weights-m20c.json"
    text = path.read_text(encoding="utf-8")
    doc = json.loads(text)
    assert doc["schema"] == 1
    weights = doc["weights"]
    assert set(weights) == set(SCORE_COMPONENTS)
    for name, value in weights.items():
        assert isinstance(value, int) and not isinstance(value, bool), (
            f"{name} is not an int: {value!r}")
    assert text == canonical(doc) + "\n"
    fit_meta = doc["fit"]
    assert fit_meta["scale_norm"] == "l1_161"
    assert fit_meta["refit"] == "all_rows_at_selected_lambda"
    split = fit_meta["split"]
    assert all(isinstance(v, int) and v > 0 for v in split.values())
    assert fit_meta["rows"] == split["train_rows"] + split["val_rows"]
    assert isinstance(fit_meta["lambda"], int) and fit_meta["lambda"] >= 0
    assert isinstance(fit_meta["val_mse_fixed"], int)
    assert isinstance(fit_meta["corpus"]["docs"], int) \
        and fit_meta["corpus"]["docs"] > 0
    assert fit_meta["corpus"]["indexes"], "production fit records its indexes"
    assert re.fullmatch(r"[0-9a-f]{64}", fit_meta["corpus"]["sha256"])
    target = sum(abs(v) for v in DEFAULT_WEIGHTS.values())
    assert sum(abs(v) for v in weights.values()) == target, (
        "sum|w| must pin the DEFAULT L1 norm EXACTLY (Codex M20c C7)")


# ------------------------------------------------- runner contract


def test_runner_loader_and_validity_gate(tmp_path) -> None:
    """Codex M20c C2/C3: the runner's loader holds the EXACT artifact
    contract (schema, five int components, sum|w| == 161 — loud refusals
    otherwise), and the paired report's validity gate REFUSES inference
    on dirty/short/duplicated rows (no p-value over polluted rows)."""
    runner = _runner_module()
    committed = REPO / "configs" / "learned-weights-m20c.json"
    weights, sha, fit_meta = runner.load_learned_weights(committed)
    assert set(weights) == set(SCORE_COMPONENTS)
    assert re.fullmatch(r"[0-9a-f]{64}", sha)
    assert isinstance(fit_meta, dict) and "rows" in fit_meta

    counter = iter(range(100))

    def write_bad(doc: dict) -> Path:
        bad = tmp_path / f"bad-{next(counter)}.json"
        bad.write_text(json.dumps(doc))
        return bad

    good = json.loads(committed.read_text())
    with pytest.raises(SystemExit):
        runner.load_learned_weights(write_bad({**good, "schema": 2}))
    broken = json.loads(json.dumps(good))
    broken["weights"]["gold"] = broken["weights"]["gold"] + 1  # L1 off by 1
    with pytest.raises(SystemExit):
        runner.load_learned_weights(write_bad(broken))
    floaty = json.loads(json.dumps(good))
    floaty["weights"]["units"] = float(floaty["weights"]["units"])
    with pytest.raises(SystemExit):
        runner.load_learned_weights(write_bad(floaty))

    def row(arm: str, seed: int, side: int, diff: int, turns: int = 40,
            violations: int = 0, rejections: int = 0) -> dict:
        return {"arm": arm, "seed": seed, "side": side, "turns": turns,
                "violations": violations, "planner_rejections": rejections,
                "value_differential": diff,
                "match_id": f"exp20c-{arm}-s{seed}-p{side}"}

    clean_rows = [row(arm, 500_003, side, 5 if arm == "learned" else 2)
                  for arm in ("learned", "base") for side in (0, 1)]
    report = runner.paired_report(clean_rows, 40)
    assert "INFERENCE SUPPRESSED" not in report
    assert "binomial p" in report and "all-pairs mean" in report
    assert "decidable-only mean" in report

    dirty = [row("learned", 500_003, 0, 99, violations=1)] + clean_rows[1:]
    assert "INFERENCE SUPPRESSED" in runner.paired_report(dirty, 40)
    short = [row("learned", 500_003, 0, 99, turns=39)] + clean_rows[1:]
    assert "INFERENCE SUPPRESSED" in runner.paired_report(short, 40)
    dup = clean_rows + [row("learned", 500_003, 0, 7)]
    suppressed = runner.paired_report(dup, 40)
    assert "INFERENCE SUPPRESSED" in suppressed
    assert "binomial p" not in suppressed
