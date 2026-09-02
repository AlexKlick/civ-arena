"""Experiment-3 harness (M15d): option-MCTS vs transposition-aware MCGS.

Per seed, per method, per SIDE (the planner plays seat 0 and, swapped,
seat 1) against the turtler, plus scripted reference arms
(turtler-vs-turtler and expansionist-vs-turtler) at the same seeds.
Equal rollout budgets; per-decision wall-clock is captured into the
results (an artifact, never the event log). Aggregates report the
value-differential mean per method, total node expansions, and realized
transposition reuse. An LLM arm is deliberately operator-gated (API
spend): run the llm-vs-turtler configs beside this at the same seeds.

    uv run python scripts/planner_experiment.py --seeds 3 --budget 16 --turns 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from civ_arena.agents.runtime import AgentProfile, build_runtime
from civ_arena.arena.coordinator import Arena
from civ_arena.config import AgentSpec, MatchSpec
from civ_arena.game.sim.value import score_differential
from civ_arena.planner.runtime import PlannerRuntime

METHODS = ("mcts", "mcgs")
BASELINES = (("turtler", "turtler"), ("expansionist", "turtler"))


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


async def run_planner_match(runs_root: Path, method: str, seed: int,
                            side: int, budget: int, turns: int) -> dict:
    match_id = f"planner-{method}-s{seed}-p{side}"
    bot = PlannerRuntime(side, 7 if side == 0 else 22,
                         method=method, budget=budget)
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
        "match_id": match_id, "arm": f"planner-{method}", "seed": seed,
        "side": side, "budget": budget, "turns": summary["final_turn"],
        "violations": summary["violations_total"],
        "planner_rejections": rejected,
        "value_differential": score_differential(summary["scores"], side),
        "decisions": len(bot.trace),
        "nodes_expanded": sum(s["nodes_expanded"] for s in bot.trace),
        "transposition_hits": sum(s["transposition_hits"] for s in bot.trace),
        "unique_states": sum(s["unique_states"] for s in bot.trace),
        "chosen": [s["chosen"] for s in bot.trace],
        "wall_ms": wall_ms,
    }


async def run_baseline_match(runs_root: Path, policies: tuple[str, str],
                             seed: int, turns: int) -> dict:
    match_id = f"base-{policies[0]}-{policies[1]}-s{seed}"
    arena = Arena(runs_root / match_id,
                  spec_for(match_id, seed, turns, policies))
    summary = await arena.run()
    return {
        "match_id": match_id, "arm": f"{policies[0]}-vs-{policies[1]}",
        "seed": seed, "side": 0, "turns": summary["final_turn"],
        "violations": summary["violations_total"],
        "value_differential": score_differential(summary["scores"], 0),
    }


def aggregate(results: list[dict]) -> dict:
    by_arm: dict[str, list[dict]] = {}
    for r in results:
        by_arm.setdefault(r["arm"], []).append(r)
    out = {}
    for arm, rows in sorted(by_arm.items()):
        diffs = [r["value_differential"] for r in rows]
        out[arm] = {
            "matches": len(rows),
            "value_differential_mean": sum(diffs) // len(diffs),
            "value_differential_all": sorted(diffs),
            "violations_total": sum(r["violations"] for r in rows),
            "rejections_total": sum(r.get("planner_rejections", 0) for r in rows),
            "nodes_expanded_total": sum(r.get("nodes_expanded", 0) for r in rows),
            "transposition_hits_total": sum(
                r.get("transposition_hits", 0) for r in rows),
            "wall_ms_total": sum(r.get("wall_ms", 0) for r in rows),
        }
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--budget", type=int, default=16)
    ap.add_argument("--turns", type=int, default=20)
    ap.add_argument("--runs-root", type=Path, default=Path("runs"))
    opts = ap.parse_args()

    results = []
    for i in range(opts.seeds):
        seed = 100_003 + i * 7919
        for method in METHODS:
            for side in (0, 1):
                r = await run_planner_match(
                    opts.runs_root, method, seed, side, opts.budget, opts.turns)
                results.append(r)
                print(f"{r['match_id']}: turns={r['turns']} "
                      f"viol={r['violations']} rej={r['planner_rejections']} "
                      f"diff={r['value_differential']} "
                      f"tp={r['transposition_hits']} wall={r['wall_ms']}ms")
        for policies in BASELINES:
            r = await run_baseline_match(opts.runs_root, policies, seed,
                                         opts.turns)
            results.append(r)
            print(f"{r['match_id']}: turns={r['turns']} "
                  f"diff={r['value_differential']}")

    doc = {"results": results, "aggregate": aggregate(results)}
    out = opts.runs_root / "planner-experiment.json"
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}")
    for arm, agg in doc["aggregate"].items():
        print(f"  {arm}: mean_diff={agg['value_differential_mean']} "
              f"tp={agg['transposition_hits_total']} "
              f"wall={agg['wall_ms_total']}ms")


if __name__ == "__main__":
    asyncio.run(main())
