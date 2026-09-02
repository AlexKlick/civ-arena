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
null is a result. Validity gate before inference: 0 watchdog violations
and 0 planner rejections per planner match, same horizon.

BUDGET 16 (not the exp-M20b b4 constraint): the value head changes LEAF
EVALUATION, which affects the UCT backup at ANY budget — there is no
"prior is inert at full exploration" hazard here, because the weights
retire nothing and reorder nothing directly; they change what every
finished rollout is worth.

METRIC-INFLATION SANITY CHECK (pre-registered): the report prints BOTH
arms' mean value_differential beside the paired diff. The match label is
computed by the RUNNER with DEFAULT_WEIGHTS (score_differential), not by
the players — but if the learned weights merely inflated the scoring
function itself, BOTH arms' differentials would shift together and the
PAIRED test would still read honestly (the shared inflation cancels in
the difference). A learned arm whose raw mean rises no more than base's
is inflation, not skill; the paired stats are the claim.

    uv run python scripts/weights_experiment.py --seeds 30 --budget 16
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import statistics
import time
from pathlib import Path

from civ_arena.agents.runtime import AgentProfile, build_runtime
from civ_arena.arena.coordinator import Arena
from civ_arena.canonical import assert_schema
from civ_arena.config import AgentSpec, MatchSpec
from civ_arena.game.sim.value import SCORE_COMPONENTS, score_differential
from civ_arena.planner.runtime import PlannerRuntime

ARMS = ("learned", "base")
DEFAULT_WEIGHTS_PATH = Path("configs/learned-weights-m20c.json")


def load_learned_weights(path: Path) -> dict[str, int]:
    """The committed M20c artifact -> the integer weights dict. Fails
    LOUD (the learned arm is the point of the experiment; a silent
    fallback to defaults would run a base-vs-base experiment and report
    it as a null result)."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert_schema(int(doc["schema"]))
    weights = doc["weights"]
    if (not isinstance(weights, dict)
            or set(weights) != set(SCORE_COMPONENTS)
            or not all(isinstance(v, int) and not isinstance(v, bool)
                       for v in weights.values())):
        raise SystemExit(
            f"error: {path} weights must be ints over {sorted(SCORE_COMPONENTS)}")
    return dict(weights)


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
                    weights: dict[str, int] | None) -> dict:
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
        "value_differential": score_differential(summary["scores"], side),
        "decisions": len(bot.trace),
        "chosen": [s["chosen"] for s in bot.trace],
        "wall_ms": wall_ms,
    }


def paired_report(rows: list[dict]) -> str:
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
    diffs = [learned["value_differential"] - base["value_differential"]
             for _, learned, base in pairs]
    clean = [d for d in diffs if d != 0]
    wins = sum(1 for d in clean if d > 0)
    ties = len(diffs) - len(clean)
    out.append(f"learned wins {wins} / base wins {len(clean) - wins} "
               f"/ ties {ties}")
    if clean:
        out.append(f"paired diff mean={statistics.mean(clean):+.0f} "
                   f"median={statistics.median(clean):+.0f} "
                   f"min={min(clean):+d} max={max(clean):+d}")
        out.append(f"one-sided exact binomial p (H: learned>base) = "
                   f"{_binom_p_ge(len(clean), wins):.4f}")
    for side in sorted({k[1] for k, _, _ in pairs}):
        sd = [learned["value_differential"] - base["value_differential"]
              for k, learned, base in pairs if k[1] == side]
        w = sum(1 for d in sd if d > 0)
        lost = sum(1 for d in sd if d < 0)
        out.append(f"side {side}: learned {w} / base {lost} "
                   f"/ tie {len(sd) - w - lost}")
    # METRIC-INFLATION SANITY CHECK (pre-registered): both arms' raw means
    # beside the paired claim — a shared inflation moves both together and
    # cancels in the paired difference; the paired stats are the claim.
    for arm in ARMS:
        arm_rows = [r for r in rows if r["arm"] == arm]
        if arm_rows:
            out.append(f"mean value_differential {arm}="
                       f"{statistics.mean(r['value_differential'] for r in arm_rows):+.0f}"
                       f" (metric-inflation check: inflation shifts both arms"
                       f" together; the paired diff cancels it)")
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

    weights = load_learned_weights(opts.weights)
    print(f"learned weights {weights} "
          f"(sum|w|={sum(abs(v) for v in weights.values())}) from "
          f"{opts.weights}", flush=True)

    results = []
    for i in range(opts.seeds):
        seed = 500_003 + i * 7919  # FIFTH block — exp3/M19b/league/M20b never see it
        for arm in ARMS:
            for side in (0, 1):
                r = await run_match(opts.runs_root, arm, seed, side,
                                    opts.budget, opts.turns, weights)
                results.append(r)
                print(f"{r['match_id']}: turns={r['turns']} "
                      f"viol={r['violations']} rej={r['planner_rejections']} "
                      f"diff={r['value_differential']} "
                      f"decisions={r['decisions']} wall={r['wall_ms']}ms",
                      flush=True)

    doc = {"results": results, "paired": paired_report(results)}
    out = opts.runs_root / "exp20c-results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}\n== paired analysis ==")
    print(doc["paired"])


if __name__ == "__main__":
    asyncio.run(main())
