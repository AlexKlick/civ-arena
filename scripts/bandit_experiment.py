#!/usr/bin/env python3
"""exp-M20b harness: contextual bandit prior ON vs OFF, paired seeds + sides.

The online-learning question (program doc §4 M20 row): does priming the
planner's option search with a contextual bandit that learns ADVANTAGES
within the match raise the value differential at equal budget against
the turtler? PAIRED by (seed, side) — only the prior varies. Seeds come
from a FOURTH block (400_003 + i*7919) that never overlaps the exp3
corpus, the exp-M19b evaluation block (200_003), or the M20a league
seeds. H (pre-registered): bandit > base; a null is a result. Validity
gate before inference: 0 watchdog violations and 0 planner rejections
per planner match, same horizon.

BUDGET 4 is load-bearing — the M19b b16 lesson: every decision carries
5-8 candidates, so a 16-rollout budget explores them ALL under the
untried-first rule and any prior's ordering is INERT (60/60 structural
ties at b16). The budget must sit BELOW the candidate count or the
experiment measures nothing; b4 is where the prior actually allocates
scarce exploration.

The bandit arm starts EMPTY each match and learns online within the
match only (no cross-match carryover — both arms see identical priors
at the first decision). Diagnostics: per-match bandit update count and
the integer floor mean |advantage| the runtime realized.

    uv run python scripts/bandit_experiment.py --seeds 30 --budget 4
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
from civ_arena.planner.bandit import Bandit
from civ_arena.planner.runtime import PlannerRuntime

ARMS = ("bandit", "base")


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
                    budget: int, turns: int) -> dict:
    match_id = f"exp20b-{arm}-s{seed}-p{side}"
    bandit = Bandit() if arm == "bandit" else None
    bot = PlannerRuntime(side, 7 if side == 0 else 22, method="mcts",
                         budget=budget, bandit=bandit)
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

    # online-learning diagnostics: update count and floor mean |advantage|
    bdoc = bot.bandit.to_doc() if bot.bandit is not None else None
    updates = int(bdoc["updates"]) if bdoc else 0
    abs_adv_sum = int(bdoc["abs_adv_sum"]) if bdoc else 0
    return {
        "match_id": match_id, "arm": arm, "seed": seed, "side": side,
        "budget": budget, "turns": summary["final_turn"],
        "violations": summary["violations_total"],
        "planner_rejections": rejected,
        "value_differential": score_differential(summary["scores"], side),
        "decisions": len(bot.trace),
        "bandit_updates": updates,
        "bandit_abs_adv_sum": abs_adv_sum,
        # integer floor mean (the fixed-point discipline — no floats in rows)
        "mean_abs_advantage": abs_adv_sum // updates if updates else 0,
        "bandit_contexts": len(bot.bandit) if bot.bandit is not None else 0,
        "chosen": [s["chosen"] for s in bot.trace],
        "wall_ms": wall_ms,
    }


def _validity_problems(rows: list[dict], requested_turns: int) -> list[str]:
    """Inference validity gate (Codex M20b B1): inference is refused unless
    ZERO dirty rows (violations/rejections), every row reached the
    requested horizon, and every (seed, side) carries EXACTLY one row per
    arm — otherwise the polluted rows would still flow into the diffs and
    the binomial as display-only numbers."""
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
    pairs = [(k, v["bandit"], v["base"]) for k, v in sorted(by_key.items())
             if "bandit" in v and "base" in v]
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
        diffs = [bandit["value_differential"] - base["value_differential"]
                 for _, bandit, base in pairs]
        clean = [d for d in diffs if d != 0]
        wins = sum(1 for d in clean if d > 0)
        ties = len(diffs) - len(clean)
        out.append(f"bandit wins {wins} / base wins {len(clean) - wins} "
                   f"/ ties {ties}")
        if clean:
            # descriptive over DECIDABLE pairs only (the exp3 house
            # convention) — labeled as such (Codex M20b B2)
            out.append(f"paired diff decidable-only mean="
                       f"{statistics.mean(clean):+.0f} "
                       f"median={statistics.median(clean):+.0f} "
                       f"min={min(clean):+d} max={max(clean):+d}")
        out.append(f"paired diff all-pairs mean="
                   f"{statistics.mean(diffs):+.0f} (ties count as 0)")
        if clean:
            out.append(f"one-sided exact binomial p (H: bandit>base) = "
                       f"{_binom_p_ge(len(clean), wins):.4f}")
        for side in sorted({k[1] for k, _, _ in pairs}):
            sd = [bandit["value_differential"] - base["value_differential"]
                  for k, bandit, base in pairs if k[1] == side]
            w = sum(1 for d in sd if d > 0)
            lost = sum(1 for d in sd if d < 0)
            out.append(f"side {side}: bandit {w} / base {lost} "
                       f"/ tie {len(sd) - w - lost}")
    armed = [r for r in rows if r["arm"] == "bandit"]
    if armed:
        total_updates = sum(r["bandit_updates"] for r in armed)
        total_abs = sum(r["bandit_abs_adv_sum"] for r in armed)
        out.append(
            f"bandit updates={total_updates} "
            f"over {sum(r['decisions'] for r in armed)} decisions "
            f"({len(armed)} matches); pooled floor mean |adv|="
            f"{total_abs // total_updates if total_updates else 0}; "
            f"max contexts in one match="
            f"{max(r['bandit_contexts'] for r in armed)}")
    return "\n".join(out)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=30)
    ap.add_argument("--budget", type=int, default=4)
    ap.add_argument("--turns", type=int, default=40)
    ap.add_argument("--runs-root", type=Path, default=Path("runs/exp20b"))
    opts = ap.parse_args()

    results = []
    for i in range(opts.seeds):
        seed = 400_003 + i * 7919  # FOURTH block — exp3/M19b/league never see it
        for arm in ARMS:
            for side in (0, 1):
                r = await run_match(opts.runs_root, arm, seed, side,
                                    opts.budget, opts.turns)
                results.append(r)
                print(f"{r['match_id']}: turns={r['turns']} "
                      f"viol={r['violations']} rej={r['planner_rejections']} "
                      f"diff={r['value_differential']} "
                      f"updates={r['bandit_updates']} "
                      f"mean_abs_adv={r['mean_abs_advantage']} "
                      f"ctx={r['bandit_contexts']} wall={r['wall_ms']}ms",
                      flush=True)

    doc = {"results": results,
           "paired": paired_report(results, opts.turns)}
    out = opts.runs_root / "exp20b-results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}\n== paired analysis ==")
    print(doc["paired"])


if __name__ == "__main__":
    asyncio.run(main())
