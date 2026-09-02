"""M20c: the learned value head — offline fit, purity, and the seam.

Pinned: numpy stays a DEV dependency (no file under src/civ_arena ever
imports it — the runtime stays pure-integer); the weights seam is live
at the search's leaf evaluation (a fake weights dict measurably moves
root_values) while ``weights=None`` is bit-identical to DEFAULT_WEIGHTS
(canonical trace docs byte-equal); the fitter is DETERMINISTIC (two
independent corpus builds -> byte-identical artifacts), quantizes to
integers at the DEFAULT L1 scale (sum|w| within 1 of 161); the committed
artifact parses, carries exactly the five components as positive ints,
and is canonical on disk; and the PlannerRuntime ``weights`` kwarg
actually REACHES search_option (the trace's root_values differ from the
default runtime's at the same seed).

The synthetic corpus generator (``write_synthetic_corpus``) is also what
produced the committed configs/learned-weights-m20c.json placeholder —
labels are noise-free y = w_true . x with w_true = DEFAULT_WEIGHTS, so
the recovered integers come back positive; the production fit over the
real M19a corpus is the orchestrator's call (it overwrites the artifact;
this file never binds the committed artifact to the synthetic fit).
"""

from __future__ import annotations

import importlib.util
import json
import random
import re
from pathlib import Path

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


# ------------------------------------------------------------- purity


def test_no_numpy_in_src() -> None:
    """numpy is a DEV dependency (the offline fitter only): no module
    under src/civ_arena may import it — the runtime stays pure-integer."""
    offenders: list[str] = []
    for path in sorted((REPO / "src" / "civ_arena").rglob("*.py")):
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"\s*(import\s+numpy|from\s+numpy\b)", line):
                offenders.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()}")
    assert offenders == [], f"numpy leaked into the runtime tree: {offenders}"


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

# noise-free ground truth: y = DEFAULT_WEIGHTS . x — recovered integers
# come back positive, which is what the committed placeholder needs
_SYNTH_TRUE_W = dict(DEFAULT_WEIGHTS)


def _hex64(rng: random.Random) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(64))


def write_synthetic_corpus(root: Path) -> list[Path]:
    """A deterministic 6-doc M19a-shaped corpus: b8/b16 (train) and b32
    (validation) under ``root``, labels exactly linear in the deltas."""
    rng = random.Random(20260902)
    paths: list[Path] = []
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
            labels.write_text(canonical(doc) + "\n", encoding="utf-8")
            paths.append(labels)
    return paths


def test_fit_weights_deterministic_and_quantized(tmp_path) -> None:
    """Two INDEPENDENT corpus builds + fits -> byte-identical artifacts;
    every weight an int; sum|w| within 1 of the DEFAULT L1 norm (161)."""
    fit = _fit_module()
    out_a, out_b = tmp_path / "weights-a.json", tmp_path / "weights-b.json"
    for out, root in ((out_a, tmp_path / "corpus-a"),
                      (out_b, tmp_path / "corpus-b")):
        write_synthetic_corpus(root)
        glob = str(root / "b*" / "planner-*" / "labels.json")
        fit.main(["--labels-glob", glob, "--out", str(out)])

    bytes_a, bytes_b = out_a.read_bytes(), out_b.read_bytes()
    assert bytes_a == bytes_b, "the fit is not deterministic"
    doc = json.loads(bytes_a)
    assert doc["schema"] == 1
    assert bytes_a.decode("utf-8") == canonical(doc) + "\n"

    weights = doc["weights"]
    assert set(weights) == set(SCORE_COMPONENTS)
    assert all(isinstance(v, int) and not isinstance(v, bool)
               for v in weights.values()), "weights must be integers"
    target = sum(abs(v) for v in DEFAULT_WEIGHTS.values())
    assert abs(sum(abs(v) for v in weights.values()) - target) <= 1

    fit_meta = doc["fit"]
    assert fit_meta["scale_norm"] == "l1_161"
    assert isinstance(fit_meta["rows"], int) and fit_meta["rows"] == 6 * 12
    assert isinstance(fit_meta["lambda"], int)
    assert isinstance(fit_meta["val_mse_fixed"], int)
    assert fit_meta["corpus"]["docs"] == 6
    assert re.fullmatch(r"[0-9a-f]{64}", fit_meta["corpus"]["sha256"])


def test_learned_artifact_valid() -> None:
    """The committed artifact parses, carries the five components as ints
    of ANY SIGN at the DEFAULT L1 scale, and is canonical on disk. The
    original positive-int pin was an orchestrator overconstraint, relaxed
    by ruling 2026-09-02: the production fit legitimately learns units=-2
    (the win-saturated turtler corpus prices military units below cost)
    and gold->0 — sign-carrying weights are the finding, not a defect.
    The scale pin (sum|w| within 1 of DEFAULT's 161) still holds."""
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
    assert isinstance(fit_meta["rows"], int) and fit_meta["rows"] > 0
    assert isinstance(fit_meta["lambda"], int) and fit_meta["lambda"] >= 0
    assert isinstance(fit_meta["val_mse_fixed"], int)
    assert isinstance(fit_meta["corpus"]["docs"], int) \
        and fit_meta["corpus"]["docs"] > 0
    assert re.fullmatch(r"[0-9a-f]{64}", fit_meta["corpus"]["sha256"])
    target = sum(abs(v) for v in DEFAULT_WEIGHTS.values())
    assert abs(sum(abs(v) for v in weights.values()) - target) <= 1
