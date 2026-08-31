"""Experiment-3 harness (M15d): option-MCTS vs transposition-aware MCGS.

Paired-seed matches of the planner (each method) against the turtler
through the real Arena. Per match: final scores, value differential,
referee rejections, and the per-decision search trace persisted as the
``runs/<match_id>/planner/trace.json`` side artifact (beside the event
log, never in it).

    uv run python scripts/planner_experiment.py --seeds 3 --budget 16 --turns 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from civ_arena.agents.runtime import AgentProfile, build_runtime
from civ_arena.arena.coordinator import Arena
from civ_arena.config import AgentSpec, MatchSpec
from civ_arena.planner.runtime import PlannerRuntime

METHODS = ("mcts", "mcgs")


def spec_for(match_id: str, seed: int, turns: int) -> MatchSpec:
    return MatchSpec(
        match_id=match_id, seed=seed, max_turns=turns, adapter="simulator",
        watchdog_mode="flag_and_continue", violation_limit=5, checkpoint_every=5,
        agents=[
            AgentSpec(agent_id="planner", player_id=0, policy="planner", seed=7),
            AgentSpec(agent_id="turtler", player_id=1, policy="turtler", seed=22),
        ],
    )


async def run_match(runs_root: Path, method: str, seed: int,
                    budget: int, turns: int) -> dict:
    match_id = f"planner-{method}-s{seed}"
    bot = PlannerRuntime(0, 7, method=method, budget=budget)
    turtler = build_runtime(AgentProfile(
        agent_id="turtler", player_id=1, policy="turtler", seed=22))
    run_dir = runs_root / match_id
    arena = Arena(run_dir, spec_for(match_id, seed, turns),
                  runtimes={0: bot, 1: turtler})
    summary = await arena.run()

    rejected = 0
    for line in (run_dir / "events.jsonl").read_text().splitlines():
        rec = json.loads(line)
        if (rec["kind"] == "TOOL_RESULT" and rec.get("status") == "rejected"
                and rec["player_id"] == 0):
            rejected += 1

    planner_dir = run_dir / "planner"
    planner_dir.mkdir(exist_ok=True)
    (planner_dir / "trace.json").write_text(
        json.dumps(bot.trace, indent=2, sort_keys=True) + "\n")

    searches = bot.trace
    return {
        "match_id": match_id, "method": method, "seed": seed,
        "budget": budget, "turns": summary["final_turn"],
        "violations": summary["violations_total"],
        "planner_rejections": rejected,
        "scores": summary["scores"],
        "decisions": len(searches),
        "transposition_hits": sum(s["transposition_hits"] for s in searches),
        "unique_states": sum(s["unique_states"] for s in searches),
        "chosen": [s["chosen"] for s in searches],
    }


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
            result = await run_match(opts.runs_root, method, seed,
                                     opts.budget, opts.turns)
            results.append(result)
            print(f"{result['match_id']}: turns={result['turns']} "
                  f"violations={result['violations']} "
                  f"rejections={result['planner_rejections']} "
                  f"tp_hits={result['transposition_hits']} "
                  f"chosen={result['chosen']}")

    out = opts.runs_root / "planner-experiment.json"
    out.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    asyncio.run(main())
