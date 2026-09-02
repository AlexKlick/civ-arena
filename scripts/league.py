#!/usr/bin/env python3
"""M20a league harness: planner-vs-planner self-play round robin.

The exp3-recorded gap was planner-vs-baseline; this lane fills the
planner-vs-planner SELF-PLAY arm — every UNORDERED pair of population
variants meets at BOTH seatings (a@p0/b@p1 and b@p0/a@p1) at every seed,
so seat asymmetry can never masquerade as variant strength. Seeds come
from a THIRD block (300_003 + i*7919) — exp3 owns 100_003+, exp19b owns
200_003+ — so no lane ever evaluates on another lane's mining corpus.

Per match one row carries BOTH seats (variant_p0/variant_p1, rejections
and value_differential per seat); per-seat planner traces persist as
planner/p0-trace.json + p1-trace.json (the M18 hotseat journal naming
precedent), and the run dir carries config.yaml so
``uv run python -m civ_arena.replay runs/<id> --config runs/<id>/config.yaml``
replays it (the config declares policy planner on both seats; the live
harness injects the runtimes programmatically — the config is for REPLAY
only). Validity gate before any inference: a match is dirty on ANY
watchdog violation or ANY seat's rejection; dirty matches are listed and
excluded from the head-to-head and the aggregate.

    uv run python scripts/league.py --seeds 5 --turns 40
    uv run python scripts/league.py --variant mcts-b8:budget=4 \
        --variant probe:method=mcgs,budget=4
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import itertools
import json
import statistics
import time
from pathlib import Path
from typing import Any

import yaml

from civ_arena.arena.coordinator import Arena
from civ_arena.config import AgentSpec, MatchSpec
from civ_arena.game.sim.value import score_differential
from civ_arena.planner.runtime import PlannerRuntime

# kwargs are PlannerRuntime constructor args (method, budget, optionally
# case_base); overrides patch by name via --variant.
DEFAULT_POPULATION: list[tuple[str, dict[str, Any]]] = [
    ("mcts-b8", {"method": "mcts", "budget": 8}),
    ("mcts-b16", {"method": "mcts", "budget": 16}),
    ("mcts-b32", {"method": "mcts", "budget": 32}),
    ("mcgs-b32", {"method": "mcgs", "budget": 32}),
]

SEAT_SEEDS = (7, 22)   # planner_experiment's per-seat seeds: p0=7, p1=22
SEED_BASE = 300_003    # THIRD block — exp3 owns 100_003+, exp19b 200_003+


def _binom_p_ge(n: int, k: int) -> float:
    """planner_analyze's exact one-sided binomial — loaded, not duplicated."""
    spec = importlib.util.spec_from_file_location(
        "planner_analyze", Path(__file__).with_name("planner_analyze.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.binom_p_ge(n, k)


def spec_for(match_id: str, seed: int, turns: int) -> MatchSpec:
    """planner_experiment's MatchSpec shape, planner policy on BOTH seats."""
    return MatchSpec(
        match_id=match_id, seed=seed, max_turns=turns, adapter="simulator",
        watchdog_mode="flag_and_continue", violation_limit=5, checkpoint_every=5,
        agents=[
            AgentSpec(agent_id="planner-p0", player_id=0, policy="planner",
                      seed=SEAT_SEEDS[0]),
            AgentSpec(agent_id="planner-p1", player_id=1, policy="planner",
                      seed=SEAT_SEEDS[1]),
        ],
    )


def write_config(run_dir: Path, spec: MatchSpec) -> None:
    """The run's own MatchSpec as YAML — the REPLAY entry point (the live
    harness injects runtimes programmatically; the config re-describes the
    match for `python -m civ_arena.replay --config`)."""
    doc = {
        "match": {
            "match_id": spec.match_id, "seed": spec.seed,
            "max_turns": spec.max_turns, "checkpoint_every": spec.checkpoint_every,
            "adapter": spec.adapter, "watchdog_mode": spec.watchdog_mode,
            "violation_limit": spec.violation_limit,
        },
        "agents": [
            {"agent_id": a.agent_id, "player_id": a.player_id,
             "policy": a.policy, "seed": a.seed}
            for a in spec.agents
        ],
        "chaos": [{"spec": c.spec, "hook": c.hook, "offset": c.offset}
                  for c in spec.chaos],
    }
    (run_dir / "config.yaml").write_text(yaml.safe_dump(doc, sort_keys=True))


def parse_variant(text: str) -> tuple[str, dict[str, Any]]:
    """--variant NAME:key=value,... -> (name, kwargs). Values parse as int
    when possible, else str; a case_base value loads the artifact."""
    from civ_arena.planner.casebase import CaseBase

    name, sep, rest = text.partition(":")
    name = name.strip()
    if not sep or not name or not rest:
        raise SystemExit(
            f"--variant {text!r}: expected NAME:KEY=VALUE[,KEY=VALUE...]")
    kwargs: dict[str, Any] = {}
    for part in rest.split(","):
        key, eq, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if not eq or not key:
            raise SystemExit(f"--variant {text!r}: bad chunk {part!r}")
        if key == "case_base":
            kwargs[key] = CaseBase.from_file(Path(value))
        else:
            try:
                kwargs[key] = int(value)
            except ValueError:
                kwargs[key] = value
    return name, kwargs


def is_dirty(row: dict) -> bool:
    """Validity gate: any watchdog violation or ANY seat's rejection."""
    return bool(row["violations"] or row.get("rejections_p0")
                or row.get("rejections_p1"))


def _winner(row: dict) -> str | None:
    d = row["value_differential_p0"]
    return row["variant_p0"] if d > 0 else (row["variant_p1"] if d < 0 else None)


async def run_league_match(runs_root: Path, name_p0: str,
                           kwargs_p0: dict[str, Any], name_p1: str,
                           kwargs_p1: dict[str, Any], seed: int,
                           turns: int) -> dict:
    """One self-play match: name_p0 at seat 0, name_p1 at seat 1."""
    pair = tuple(sorted((name_p0, name_p1)))
    arm = f"{pair[0]}-vs-{pair[1]}"
    seating = "ab" if name_p0 == pair[0] else "ba"
    match_id = f"lg-{arm}-s{seed}-{seating}"
    spec = spec_for(match_id, seed, turns)
    run_dir = runs_root / match_id
    rt0 = PlannerRuntime(0, SEAT_SEEDS[0], **kwargs_p0)
    rt1 = PlannerRuntime(1, SEAT_SEEDS[1], **kwargs_p1)
    t0 = time.perf_counter()
    arena = Arena(run_dir, spec, runtimes={0: rt0, 1: rt1})
    summary = await arena.run()
    wall_ms = int((time.perf_counter() - t0) * 1000)

    rejections = {0: 0, 1: 0}
    for line in (run_dir / "events.jsonl").read_text().splitlines():
        rec = json.loads(line)
        if (rec["kind"] == "TOOL_RESULT" and rec.get("status") == "rejected"
                and rec["player_id"] in rejections):
            rejections[rec["player_id"]] += 1

    planner_dir = run_dir / "planner"
    planner_dir.mkdir(exist_ok=True)
    (planner_dir / "p0-trace.json").write_text(
        json.dumps(rt0.trace, indent=2, sort_keys=True) + "\n")
    (planner_dir / "p1-trace.json").write_text(
        json.dumps(rt1.trace, indent=2, sort_keys=True) + "\n")
    write_config(run_dir, spec)

    return {
        "match_id": match_id, "arm": arm, "seating": seating,
        "variant_p0": name_p0, "variant_p1": name_p1, "seed": seed,
        "budget_p0": kwargs_p0.get("budget"), "budget_p1": kwargs_p1.get("budget"),
        "method_p0": kwargs_p0.get("method"), "method_p1": kwargs_p1.get("method"),
        "turns": summary["final_turn"],
        "violations": summary["violations_total"],
        "rejections_p0": rejections[0], "rejections_p1": rejections[1],
        "value_differential_p0": score_differential(summary["scores"], 0),
        "value_differential_p1": score_differential(summary["scores"], 1),
        "decisions_p0": len(rt0.trace), "decisions_p1": len(rt1.trace),
        "chosen_p0": [s["chosen"] for s in rt0.trace],
        "chosen_p1": [s["chosen"] for s in rt1.trace],
        "wall_ms": wall_ms,
    }


def head_to_head(rows: list[dict]) -> tuple[dict[str, dict], str]:
    """Per UNORDERED pair: W/L/T across (seed x seating), exact one-sided
    binomial (H: first-named > second-named, canonical name order),
    per-seating breakdown, per-seed aggregation for the first-named
    variant (for = wins BOTH seatings, against = loses both, else split).
    Dirty matches are listed and excluded."""
    by_pair: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        by_pair.setdefault(
            tuple(sorted((r["variant_p0"], r["variant_p1"]))), []).append(r)

    per_pair: dict[str, dict] = {}
    lines: list[str] = ["== head-to-head (clean matches only) =="]
    for (a, b), prows in sorted(by_pair.items()):
        dirty = [r for r in prows if is_dirty(r)]
        clean = [r for r in prows if not is_dirty(r)]
        wins = {a: 0, b: 0}
        ties = 0
        seatings = {
            f"{a}@p0": {a: 0, b: 0, "ties": 0},
            f"{b}@p0": {a: 0, b: 0, "ties": 0},
        }
        by_seed: dict[int, list[dict]] = {}
        for r in clean:
            winner = _winner(r)
            if winner is None:
                ties += 1
            else:
                wins[winner] += 1
            seat = seatings[f"{a}@p0"] if r["variant_p0"] == a \
                else seatings[f"{b}@p0"]
            if winner is None:
                seat["ties"] += 1
            else:
                seat[winner] += 1
            by_seed.setdefault(r["seed"], []).append(r)

        decidable = wins[a] + wins[b]
        mean_diff = {}
        for v in (a, b):
            diffs = [r["value_differential_p0"] if r["variant_p0"] == v
                     else r["value_differential_p1"] for r in clean]
            mean_diff[v] = statistics.mean(diffs) if diffs else None

        seed_out = {"for": 0, "against": 0, "split": 0}
        for srows in by_seed.values():
            w = [_winner(r) for r in srows]
            if len(srows) == 2 and all(x == a for x in w):
                seed_out["for"] += 1
            elif len(srows) == 2 and all(x == b for x in w):
                seed_out["against"] += 1
            else:
                seed_out["split"] += 1

        entry = {
            "matches": len(prows), "clean": len(clean),
            "dirty": [r["match_id"] for r in dirty],
            "hypothesis": f"{a}>{b}",
            "wins": dict(wins), "ties": ties,
            "p_one_sided": (_binom_p_ge(decidable, wins[a])
                            if decidable else None),
            "per_seating": seatings,
            "per_seed": seed_out,
            "mean_differential": mean_diff,
        }
        per_pair[f"{a}-vs-{b}"] = entry

        lines.append(f"== {a} vs {b} (H: {a}>{b}) ==")
        lines.append(f"   matches={len(prows)} clean={len(clean)} "
                     f"dirty={len(dirty)}")
        for r in dirty:
            lines.append(f"   DIRTY: {r['match_id']} viol={r['violations']} "
                         f"rej={r.get('rejections_p0')}/"
                         f"{r.get('rejections_p1')}")
        lines.append(f"   {a} wins {wins[a]} / {b} wins {wins[b]} / "
                     f"ties {ties}")
        if decidable:
            lines.append(f"   one-sided exact binomial p (H: {a}>{b}) = "
                         f"{entry['p_one_sided']:.4f}")
        else:
            lines.append("   no decidable matches (all ties)")
        for seat_key in (f"{a}@p0", f"{b}@p0"):
            s = seatings[seat_key]
            lines.append(f"   seating {seat_key}: {a} {s[a]} / {b} {s[b]} / "
                         f"tie {s['ties']}")
        lines.append(f"   seeds for {a}: for={seed_out['for']} "
                     f"against={seed_out['against']} "
                     f"split={seed_out['split']}")
        if clean:
            lines.append(f"   mean differential: {a}={mean_diff[a]:+.1f} "
                         f"{b}={mean_diff[b]:+.1f}")
        else:
            lines.append(f"   mean differential: no clean matches "
                         f"for {a}-vs-{b}")
    return per_pair, "\n".join(lines)


def aggregate(rows: list[dict], names: list[str]) -> dict[str, dict]:
    """Per variant over CLEAN matches: win-rate, mean differential, totals;
    dirty match counts are reported, never inferred from."""
    clean = [r for r in rows if not is_dirty(r)]
    out: dict[str, dict] = {}
    for v in sorted(names):
        mine = [r for r in clean if v in (r["variant_p0"], r["variant_p1"])]
        diffs = [r["value_differential_p0"] if r["variant_p0"] == v
                 else r["value_differential_p1"] for r in mine]
        winners = [_winner(r) for r in mine]
        wins = winners.count(v)
        ties = winners.count(None)
        out[v] = {
            "matches": len(mine), "wins": wins,
            "losses": len(mine) - wins - ties, "ties": ties,
            "win_rate": (wins / len(mine)) if mine else None,
            "value_differential_mean": (statistics.mean(diffs)
                                        if diffs else None),
            "decisions_total": sum(
                r["decisions_p0"] if r["variant_p0"] == v else r["decisions_p1"]
                for r in mine),
            "violations_total": sum(r["violations"] for r in mine),
            "rejections_total": sum(
                r["rejections_p0"] if r["variant_p0"] == v
                else r["rejections_p1"] for r in mine),
            "dirty_matches": sum(
                1 for r in rows
                if v in (r["variant_p0"], r["variant_p1"]) and is_dirty(r)),
            "wall_ms_total": sum(r["wall_ms"] for r in mine),
        }
    return out


def _aggregate_text(agg: dict[str, dict]) -> str:
    lines = ["== aggregate (clean matches only) =="]
    for v, a in sorted(agg.items()):
        wr = f"{a['win_rate']:.3f}" if a["win_rate"] is not None else "n/a"
        md = (f"{a['value_differential_mean']:+.1f}"
              if a["value_differential_mean"] is not None else "n/a")
        lines.append(f"  {v}: matches={a['matches']} W{a['wins']}/"
                     f"L{a['losses']}/T{a['ties']} win_rate={wr} "
                     f"mean_diff={md} decisions={a['decisions_total']} "
                     f"dirty={a['dirty_matches']} wall={a['wall_ms_total']}ms")
    return "\n".join(lines)


async def run_league(runs_root: Path,
                     population: list[tuple[str, dict[str, Any]]],
                     seeds: int, turns: int) -> dict:
    """Every unordered pair, both seatings, every seed; writes
    runs_root/league-results.json {results, per_pair, aggregate}."""
    runs_root.mkdir(parents=True, exist_ok=True)
    pop = dict(population)
    names = [n for n, _ in population]
    results = []
    for i in range(seeds):
        seed = SEED_BASE + i * 7919
        for a, b in itertools.combinations(names, 2):
            for p0, p1 in ((a, b), (b, a)):
                r = await run_league_match(runs_root, p0, pop[p0], p1, pop[p1],
                                           seed, turns)
                results.append(r)
                print(f"{r['match_id']}: turns={r['turns']} "
                      f"viol={r['violations']} "
                      f"rej={r['rejections_p0']}/{r['rejections_p1']} "
                      f"diff={r['value_differential_p0']:+d}/"
                      f"{r['value_differential_p1']:+d} "
                      f"wall={r['wall_ms']}ms", flush=True)

    per_pair, text = head_to_head(results)
    agg = aggregate(results, names)
    doc = {"results": results, "per_pair": per_pair, "aggregate": agg}
    out = runs_root / "league-results.json"
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}")
    print(text)
    print(_aggregate_text(agg))
    return doc


def resolve_population(
        overrides: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """DEFAULT_POPULATION patched entry-by-entry — an override UPDATES the
    named entry's kwargs (unmentioned keys keep their defaults; a bare
    budget override must never silently flip the method) — while a --variant
    name that does not exist is ADDED (a probe variant) at the end."""
    pop = {name: dict(kwargs) for name, kwargs in DEFAULT_POPULATION}
    order = [name for name, _ in DEFAULT_POPULATION]
    for text in overrides:
        name, kwargs = parse_variant(text)
        if name in pop:
            pop[name].update(kwargs)
        else:
            order.append(name)
            pop[name] = kwargs
    return [(name, pop[name]) for name in order]


async def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--turns", type=int, default=40)
    ap.add_argument("--runs-root", type=Path, default=Path("runs/league"))
    ap.add_argument("--variant", action="append", default=[], metavar="NAME:K=V,...",
                    help="override/add a population entry (values parsed as "
                         "int when possible; case_base=<path> loads the "
                         "artifact)")
    opts = ap.parse_args(argv)
    population = resolve_population(opts.variant)
    print("population: " + ", ".join(
        f"{n}({','.join(f'{k}={v}' for k, v in sorted(kw.items()))})"
        for n, kw in population))
    await run_league(opts.runs_root, population, opts.seeds, opts.turns)


if __name__ == "__main__":
    asyncio.run(main())
