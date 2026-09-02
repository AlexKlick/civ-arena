#!/usr/bin/env python3
"""exp-M19b harness: case-base prior ON vs OFF, paired seeds + sides.

The retrieval-as-evidence question (program doc §4 M19 row): does priming
the planner's option search with the case base's historical ranking raise
the value differential at EQUAL budget against the turtler? PAIRED by
(seed, side) — only the prior varies. Seeds come from a NEW block
(200_003 + i*7919) that never overlaps the exp3 mining corpus, so the
case base has never seen an evaluation world. H (pre-registered): case >
base; a null is a result. Validity gate before inference: 0 watchdog
violations and 0 planner rejections per planner match, same horizon.

    uv run python scripts/casebase_experiment.py --seeds 30 --budget 16
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
from civ_arena.config import AgentSpec, MatchSpec
from civ_arena.game.sim.value import score_differential
from civ_arena.planner.casebase import CaseBase
from civ_arena.planner.runtime import PlannerRuntime

ARMS = ("case", "base")


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
                    budget: int, turns: int, case_base: CaseBase | None) -> dict:
    match_id = f"exp19b-{arm}-s{seed}-p{side}"
    bot = PlannerRuntime(side, 7 if side == 0 else 22, method="mcts",
                         budget=budget,
                         case_base=case_base if arm == "case" else None)
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

    hits = sum(1 for t in bot.trace if t.get("case_prior", {}).get("hit") == 1)
    misses = sum(1 for t in bot.trace if t.get("case_prior", {}).get("hit") == 0)
    return {
        "match_id": match_id, "arm": arm, "seed": seed, "side": side,
        "budget": budget, "turns": summary["final_turn"],
        "violations": summary["violations_total"],
        "planner_rejections": rejected,
        "value_differential": score_differential(summary["scores"], side),
        "decisions": len(bot.trace),
        "case_hits": hits, "case_misses": misses,
        "chosen": [s["chosen"] for s in bot.trace],
        "wall_ms": wall_ms,
    }


def paired_report(rows: list[dict]) -> str:
    bad = [r for r in rows if r["violations"] or r.get("planner_rejections")]
    turns_set = {r["turns"] for r in rows}
    by_key: dict[tuple[int, int], dict[str, dict]] = {}
    for r in rows:
        by_key.setdefault((r["seed"], r["side"]), {})[r["arm"]] = r
    pairs = [(k, v["case"], v["base"]) for k, v in sorted(by_key.items())
             if "case" in v and "base" in v]
    out = [f"matches={len(rows)} turns={sorted(turns_set)} "
           f"dirty={len(bad)} pairs={len(pairs)}"]
    for r in bad:
        out.append(f"  DIRTY: {r['match_id']} viol={r['violations']} "
                   f"rej={r.get('planner_rejections')}")
    diffs = [case["value_differential"] - base["value_differential"]
             for _, case, base in pairs]
    clean = [d for d in diffs if d != 0]
    wins = sum(1 for d in clean if d > 0)
    ties = len(diffs) - len(clean)
    out.append(f"case wins {wins} / base wins {len(clean) - wins} / ties {ties}")
    if clean:
        out.append(f"paired diff mean={statistics.mean(clean):+.0f} "
                   f"median={statistics.median(clean):+.0f} "
                   f"min={min(clean):+d} max={max(clean):+d}")
        out.append(f"one-sided exact binomial p (H: case>base) = "
                   f"{_binom_p_ge(len(clean), wins):.4f}")
    for side in sorted({k[1] for k, _, _ in pairs}):
        sd = [case["value_differential"] - base["value_differential"]
              for k, case, base in pairs if k[1] == side]
        w = sum(1 for d in sd if d > 0)
        lost = sum(1 for d in sd if d < 0)
        out.append(f"side {side}: case {w} / base {lost} / tie {len(sd) - w - lost}")
    hit_rows = [r for r in rows if r["arm"] == "case"]
    if hit_rows:
        out.append(f"case hits={sum(r['case_hits'] for r in hit_rows)} "
                   f"misses={sum(r['case_misses'] for r in hit_rows)} "
                   f"over {sum(r['decisions'] for r in hit_rows)} decisions")
    return "\n".join(out)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=30)
    ap.add_argument("--budget", type=int, default=16)
    ap.add_argument("--turns", type=int, default=40)
    ap.add_argument("--case-artifact", type=Path,
                    default=Path("configs/casebase-m19b.json"))
    ap.add_argument("--runs-root", type=Path, default=Path("runs/exp19b"))
    opts = ap.parse_args()

    case_base = CaseBase.from_file(opts.case_artifact)
    print(f"case base from {opts.case_artifact}:\n{case_base.summary()}")

    results = []
    for i in range(opts.seeds):
        seed = 200_003 + i * 7919  # NEW block — never overlaps the exp3 corpus
        for arm in ARMS:
            for side in (0, 1):
                r = await run_match(opts.runs_root, arm, seed, side,
                                    opts.budget, opts.turns, case_base)
                results.append(r)
                print(f"{r['match_id']}: turns={r['turns']} "
                      f"viol={r['violations']} rej={r['planner_rejections']} "
                      f"diff={r['value_differential']} "
                      f"hits={r['case_hits']}/{r['case_hits'] + r['case_misses']} "
                      f"wall={r['wall_ms']}ms", flush=True)

    doc = {"results": results, "paired": paired_report(results)}
    out = opts.runs_root / "exp19b-results.json"
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}\n== paired analysis ==")
    print(doc["paired"])


if __name__ == "__main__":
    asyncio.run(main())
