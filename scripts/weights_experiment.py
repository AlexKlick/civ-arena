#!/usr/bin/env python3
"""exp-M20c harness: learned value head ON vs OFF, paired seeds + sides.

The learned-head question (program doc §4 M20 row): does priming the
planner's leaf evaluation with OFFLINE-fit integer weights
(configs/learned-weights-m20c.json — ridge over the M19a label corpus,
quantized, L1-normalized to the DEFAULT scale so UCT_C=140 stays tuned)
raise the value differential at equal budget against the turtler? PAIRED
by (seed, side) — only the value head varies. Seeds come from a FIFTH
block (500_003 + i*7919) that never overlaps the exp3 corpus, the
exp-M19b evaluation block (200_003), the M20a league seeds, or the
exp-M20b bandit block (400_003). H (pre-registered): learned > base; a
null is a result.

VALIDITY GATE (Codex M20c C3, the M20b B1 class): inference is REFUSED
unless zero dirty rows (watchdog violations / planner rejections), every
row reached the requested horizon, and every (seed, side) carries
exactly one row per arm — a failed gate prints its reasons and never a
p-value or win count over polluted rows.

BUDGET 16 (not the exp-M20b b4 constraint): the value head changes LEAF
EVALUATION, which affects the UCT backup at ANY budget — there is no
"prior is inert at full exploration" hazard here, because the weights
retire nothing and reorder nothing directly; they change what every
finished rollout is worth.

EVALUATION SCOPE (Codex M20c C10): the endpoint scorer is FIXED —
score_differential over summary scores takes NO weights (always
DEFAULT), so this experiment measures policy behavior under a fixed
DEFAULT endpoint on held-out seeds against the turtler; no
generalization beyond this scorer/opponent is claimed. Both arms' raw
mean differentials are printed beside the paired stats as descriptives
of that fixed endpoint.

DECLARED LIMITATION (Codex M20c C6 ruling): UCT_C=140 is NOT
recalibrated for this head — L1 normalization bounds the norm, not
per-branch Q-value dispersion (the artifact records the same note as
``uct_c_note``). Calibration is a future rung.

    uv run python scripts/weights_experiment.py --seeds 30 --budget 16
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import statistics
import time
from pathlib import Path

from civ_arena.agents.runtime import AgentProfile, build_runtime
from civ_arena.arena.coordinator import Arena
from civ_arena.canonical import assert_schema
from civ_arena.config import AgentSpec, MatchSpec
from civ_arena.game.sim.value import (
    DEFAULT_WEIGHTS,
    SCORE_COMPONENTS,
    score_differential,
)
from civ_arena.planner.runtime import PlannerRuntime

ARMS = ("learned", "base")
DEFAULT_WEIGHTS_PATH = Path("configs/learned-weights-m20c.json")
SEED_FIRST = 500_003
SEED_STEP = 7919
TARGET_L1 = sum(abs(v) for v in DEFAULT_WEIGHTS.values())  # == 161


def load_learned_weights(path: Path) -> tuple[dict[str, int], str, dict]:
    """The committed M20c artifact -> (weights, artifact sha256, fit
    metadata). Validates the EXACT contract at load (Codex M20c C2):
    schema 1, weights are ints over exactly the five components, and
    sum|w| == the DEFAULT L1 norm exactly. Fails LOUD — the learned arm
    is the point of the experiment; a silent fallback to defaults would
    run a base-vs-base experiment and report it as a null result."""
    raw = Path(path).read_bytes()
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise SystemExit(f"error: {path} is not a JSON object")
    try:
        assert_schema(int(doc["schema"]))
    except Exception as exc:
        raise SystemExit(f"error: {path} schema is not 1: {exc}") from exc
    weights = doc.get("weights")
    if (not isinstance(weights, dict)
            or set(weights) != set(SCORE_COMPONENTS)
            or not all(isinstance(v, int) and not isinstance(v, bool)
                       for v in weights.values())):
        raise SystemExit(
            f"error: {path} weights must be ints over {sorted(SCORE_COMPONENTS)}")
    l1 = sum(abs(v) for v in weights.values())
    if l1 != TARGET_L1:
        raise SystemExit(f"error: {path} weights sum|w|={l1} != the DEFAULT "
                         f"L1 norm {TARGET_L1} — the UCT scale contract is "
                         "broken")
    fit_meta = doc.get("fit")
    if not isinstance(fit_meta, dict):
        raise SystemExit(f"error: {path} carries no fit metadata block")
    return (dict(weights), hashlib.sha256(raw).hexdigest(), fit_meta)


def _binom_p_ge(n: int, k: int) -> float:
    """planner_analyze's exact one-sided binomial — loaded, not duplicated."""
    spec = importlib.util.spec_from_file_location(
        "planner_analyze", Path(__file__).with_name("planner_analyze.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.binom_p_ge(n, k)


def spec_for(match_id: str, seed: int, turns: int,
             policies: tuple[str, str]) -> MatchSpec:
    return MatchSpec(
        match_id=match_id, seed=seed, max_turns=turns, adapter="simulator",
        watchdog_mode="flag_and_continue", violation_limit=5, checkpoint_every=5,
        agents=[
            AgentSpec(agent_id=f"{policies[0]}-p0", player_id=0,
                      policy=policies[0], seed=7),
            AgentSpec(agent_id=f"{policies[1]}-p1", player_id=1,
                      policy=policies[1], seed=22),
        ],
    )


async def run_match(runs_root: Path, arm: str, seed: int, side: int,
                    budget: int, turns: int,
                    weights: dict[str, int] | None,
                    provenance: dict) -> dict:
    match_id = f"exp20c-{arm}-s{seed}-p{side}"
    bot = PlannerRuntime(side, 7 if side == 0 else 22, method="mcts",
                         budget=budget,
                         weights=weights if arm == "learned" else None)
    opp = build_runtime(AgentProfile(
        agent_id="turtler-opp", player_id=1 - side, policy="turtler",
        seed=22 if side == 0 else 7))
    policies = ("planner", "turtler") if side == 0 else ("turtler", "planner")
    run_dir = runs_root / match_id
    t0 = time.perf_counter()
    arena = Arena(run_dir, spec_for(match_id, seed, turns, policies),
                  runtimes={side: bot, 1 - side: opp})
    summary = await arena.run()
    wall_ms = int((time.perf_counter() - t0) * 1000)

    rejected = 0
    for line in (run_dir / "events.jsonl").read_text().splitlines():
        rec = json.loads(line)
        if (rec["kind"] == "TOOL_RESULT" and rec.get("status") == "rejected"
                and rec["player_id"] == side):
            rejected += 1

    planner_dir = run_dir / "planner"
    planner_dir.mkdir(exist_ok=True)
    (planner_dir / "trace.json").write_text(
        json.dumps(bot.trace, indent=2, sort_keys=True) + "\n")

    return {
        "match_id": match_id, "arm": arm, "seed": seed, "side": side,
        "budget": budget, "turns": summary["final_turn"],
        "violations": summary["violations_total"],
        "planner_rejections": rejected,
        # the endpoint is ALWAYS the fixed DEFAULT scorer — the learned
        # head lives inside the planner's search, never in the label
        "value_differential": score_differential(summary["scores"], side),
        "decisions": len(bot.trace),
        "chosen": [s["chosen"] for s in bot.trace],
        "wall_ms": wall_ms,
        # provenance (Codex M20c C2): every row binds WHAT it evaluated
        **provenance,
    }


def _validity_problems(rows: list[dict], requested_turns: int) -> list[str]:
    """Inference validity gate (Codex M20b B1 pattern, applied here per
    M20c C3): inference is refused unless ZERO dirty rows
    (violations/rejections), every row reached the requested horizon, and
    every (seed, side) carries EXACTLY one row per arm — otherwise the
    polluted rows would still flow into the diffs and the binomial as
    display-only numbers."""
    problems: list[str] = []
    dirty = [r for r in rows if r["violations"] or r.get("planner_rejections")]
    if dirty:
        problems.append(f"{len(dirty)} dirty row(s) (violations/rejections)")
    short = [r for r in rows if r["turns"] != requested_turns]
    if short:
        problems.append(f"{len(short)} row(s) short of the requested "
                        f"horizon {requested_turns}")
    per_pair: dict[tuple[int, int], dict[str, int]] = {}
    for r in rows:
        arms = per_pair.setdefault((r["seed"], r["side"]), {})
        arms[r["arm"]] = arms.get(r["arm"], 0) + 1
    for (seed, side), arms in sorted(per_pair.items()):
        bad_arms = sorted(arm for arm in ARMS if arms.get(arm, 0) != 1)
        if bad_arms:
            problems.append(f"pair (seed={seed}, side={side}): arms "
                            f"{bad_arms} not exactly-once")
    return problems


def paired_report(rows: list[dict], requested_turns: int) -> str:
    problems = _validity_problems(rows, requested_turns)
    bad = [r for r in rows if r["violations"] or r.get("planner_rejections")]
    turns_set = {r["turns"] for r in rows}
    by_key: dict[tuple[int, int], dict[str, dict]] = {}
    for r in rows:
        by_key.setdefault((r["seed"], r["side"]), {})[r["arm"]] = r
    pairs = [(k, v["learned"], v["base"]) for k, v in sorted(by_key.items())
             if "learned" in v and "base" in v]
    out = [f"matches={len(rows)} turns={sorted(turns_set)} "
           f"dirty={len(bad)} pairs={len(pairs)}"]
    for r in bad:
        out.append(f"  DIRTY: {r['match_id']} viol={r['violations']} "
                   f"rej={r.get('planner_rejections')}")
    if problems:
        # inference refused — the reason is printed, never a p-value or a
        # win count computed over polluted rows
        out.append("INFERENCE SUPPRESSED — validity gate failed: "
                   + "; ".join(problems))
    else:
        diffs = [learned["value_differential"] - base["value_differential"]
                 for _, learned, base in pairs]
        clean = [d for d in diffs if d != 0]
        wins = sum(1 for d in clean if d > 0)
        ties = len(diffs) - len(clean)
        out.append(f"learned wins {wins} / base wins {len(clean) - wins} "
                   f"/ ties {ties}")
        if clean:
            # descriptive over DECIDABLE pairs only (the exp3 house
            # convention) — labeled as such (Codex M20b B2 / M20c C4)
            out.append(f"paired diff decidable-only mean="
                       f"{statistics.mean(clean):+.0f} "
                       f"median={statistics.median(clean):+.0f} "
                       f"min={min(clean):+d} max={max(clean):+d}")
        out.append(f"paired diff all-pairs mean="
                   f"{statistics.mean(diffs):+.0f} (ties count as 0)")
        if clean:
            out.append(f"one-sided exact binomial p (H: learned>base) = "
                       f"{_binom_p_ge(len(clean), wins):.4f}")
        for side in sorted({k[1] for k, _, _ in pairs}):
            sd = [learned["value_differential"] - base["value_differential"]
                  for k, learned, base in pairs if k[1] == side]
            w = sum(1 for d in sd if d > 0)
            lost = sum(1 for d in sd if d < 0)
            out.append(f"side {side}: learned {w} / base {lost} "
                       f"/ tie {len(sd) - w - lost}")
    # descriptives under the FIXED DEFAULT endpoint (Codex M20c C10): the
    # scorer takes no weights — no generalization beyond this scorer and
    # the turtler opponent is claimed
    for arm in ARMS:
        arm_rows = [r for r in rows if r["arm"] == arm]
        if arm_rows:
            out.append(f"mean value_differential {arm}="
                       f"{statistics.mean(r['value_differential'] for r in arm_rows):+.0f}"
                       f" (fixed DEFAULT endpoint scorer, score_differential"
                       f" takes no weights)")
    return "\n".join(out)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=30)
    ap.add_argument("--budget", type=int, default=16)
    ap.add_argument("--turns", type=int, default=40)
    ap.add_argument("--runs-root", type=Path, default=Path("runs/exp20c"))
    ap.add_argument("--weights", type=Path, default=DEFAULT_WEIGHTS_PATH,
                    help="the learned-weights artifact the learned arm loads")
    opts = ap.parse_args()

    weights, artifact_sha256, fit_meta = load_learned_weights(opts.weights)
    print(f"learned weights {weights} "
          f"(sum|w|={sum(abs(v) for v in weights.values())}) from "
          f"{opts.weights} sha256={artifact_sha256[:12]}…", flush=True)
    # every results row binds what it evaluated (Codex M20c C2)
    provenance = {
        "artifact_sha256": artifact_sha256,
        "weights": dict(weights),
        "fit": fit_meta,
        "config": {
            "seeds": opts.seeds, "budget": opts.budget, "turns": opts.turns,
            "seed_first": SEED_FIRST, "seed_step": SEED_STEP,
            "weights_path": str(opts.weights),
            "runs_root": str(opts.runs_root),
        },
    }

    results = []
    for i in range(opts.seeds):
        seed = SEED_FIRST + i * SEED_STEP  # FIFTH block — exp3/M19b/league/M20b never see it
        for arm in ARMS:
            for side in (0, 1):
                r = await run_match(opts.runs_root, arm, seed, side,
                                    opts.budget, opts.turns, weights,
                                    provenance)
                results.append(r)
                print(f"{r['match_id']}: turns={r['turns']} "
                      f"viol={r['violations']} rej={r['planner_rejections']} "
                      f"diff={r['value_differential']} "
                      f"decisions={r['decisions']} wall={r['wall_ms']}ms",
                      flush=True)

    doc = {"results": results,
           "paired": paired_report(results, opts.turns)}
    out = opts.runs_root / "exp20c-results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}\n== paired analysis ==")
    print(doc["paired"])


if __name__ == "__main__":
    asyncio.run(main())
