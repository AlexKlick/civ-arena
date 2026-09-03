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
only).

Inference unit (Codex A1): the two seatings of one map seed are ONE
cluster, not two independent Bernoullis — a seed contributes a single
decidable observation iff BOTH complementary seatings are clean and the
first-named variant wins or loses both. Split seeds (1-1, any tie
combination, dirty or missing mate) are reported, never inferred; raw
per-seating W/L/T survives only as *_descriptive fields. The artifact
contract is INTEGERS (Codex A10): the binomial serializes as p_num/p_den
(the float p prints only in the human report), differentials as
diff_sum/diff_count, win-rates as wins/matches. Wall-clock never enters
the results doc (Codex A2): it prints to stdout and lands in
runs/<root>/telemetry.json, which is explicitly NON-contractual.

A match dir that already exists non-empty is refused BEFORE any spend
(Codex A3) — EventLog appends, so a re-run would double-execute into the
same log. Variant names reject '-vs-' and '/' at resolve time (Codex A5):
they are serialized into match ids and per_pair keys and must stay
injective. Every scheduled variant is normalized to its COMPLETE
effective constructor kwargs (Codex A4): an override patches the named
DEFAULT_POPULATION entry; an unknown name is a probe patched against the
EXPLICIT frozen PROBE_BASE below (never PlannerRuntime's module defaults —
a runtime-default drift must not silently reshape the league). The doc
carries the population manifest, and an armed case_base records its
artifact sha256.

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

# A4: the explicit, frozen base a NEW (probe) variant patches against.
# Deliberately NOT PlannerRuntime's module constants: a runtime default
# drift must never silently reshape the league population.
PROBE_BASE: dict[str, Any] = {"method": "mcts", "budget": 12}

SEAT_SEEDS = (7, 22)   # planner_experiment's per-seat seeds: p0=7, p1=22
SEED_BASE = 300_003    # THIRD block — exp3 owns 100_003+, exp19b 200_003+

TELEMETRY_NOTE = ("NON-CONTRACTUAL wall-clock telemetry; never part of the "
                  "league-results.json artifact contract (deterministic doc)")


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
        # Frozen M20 research harness: it intentionally remains on the V1
        # compatibility coordinator. Authoritative file-backed matches use
        # ArenaV2; this emitted config is consumed only by the V1 replay reader.
        schema=1,
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


def _validate_name(name: str) -> None:
    """A5: names serialize into match ids ('lg-a-vs-b-...') and per_pair
    keys — '-vs-' or '/' would collide distinct pairs/dirs. Refuse."""
    if "-vs-" in name or "/" in name:
        raise ValueError(
            f"variant name {name!r} contains '-vs-' or '/' — names must stay "
            "injective in match ids and per_pair keys")


def resolve_population(
        overrides: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """DEFAULT_POPULATION patched entry-by-entry, then normalized so every
    scheduled variant carries its COMPLETE effective constructor kwargs
    (an override UPDATES the named entry; unmentioned keys keep their
    values — a bare budget override must never silently flip the method).
    A --variant name that does not exist is ADDED as a probe patched
    against the explicit PROBE_BASE."""
    pop = {name: dict(kwargs) for name, kwargs in DEFAULT_POPULATION}
    order = [name for name, _ in DEFAULT_POPULATION]
    for name in order:
        _validate_name(name)
    for text in overrides:
        name, patch = parse_variant(text)
        _validate_name(name)
        base = pop[name] if name in pop else dict(PROBE_BASE)
        merged = {**base, **patch}
        merged.setdefault("method", PROBE_BASE["method"])
        merged.setdefault("budget", PROBE_BASE["budget"])
        if name not in pop:
            order.append(name)
        pop[name] = merged
    return [(name, pop[name]) for name in order]


def population_manifest(
        population: list[tuple[str, dict[str, Any]]]) -> list[dict]:
    """A4: the effective kwargs per variant, JSON-native — a CaseBase
    object serializes as its artifact sha256."""
    out = []
    for name, kw in population:
        entry = {"name": name, "kwargs": {}}
        for key, value in kw.items():
            if key == "case_base" and value is not None:
                entry["kwargs"][key] = {"artifact_sha256":
                                        value.artifact_sha256}
            else:
                entry["kwargs"][key] = value
        out.append(entry)
    return out


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
    # A3: EventLog APPENDS — a re-run into a non-empty dir double-executes
    # into the same log. Refuse BEFORE any spend (before Arena constructs).
    if run_dir.exists() and any(run_dir.iterdir()):
        raise SystemExit(
            f"refusing to run {match_id}: {run_dir} already exists and is "
            f"non-empty — a re-run would append into the existing event log; "
            f"use a fresh --runs-root")
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
        "budget_p0": kwargs_p0["budget"], "budget_p1": kwargs_p1["budget"],
        "method_p0": kwargs_p0["method"], "method_p1": kwargs_p1["method"],
        "turns": summary["final_turn"],
        "violations": summary["violations_total"],
        "rejections_p0": rejections[0], "rejections_p1": rejections[1],
        "value_differential_p0": score_differential(summary["scores"], 0),
        "value_differential_p1": score_differential(summary["scores"], 1),
        "decisions_p0": len(rt0.trace), "decisions_p1": len(rt1.trace),
        "chosen_p0": [s["chosen"] for s in rt0.trace],
        "chosen_p1": [s["chosen"] for s in rt1.trace],
        "wall_ms": wall_ms,  # in-memory only; stripped from the results doc
    }


def head_to_head(rows: list[dict]) -> tuple[dict[str, dict], str]:
    """Per UNORDERED pair. INFERENTIAL unit (Codex A1): the (pair, seed)
    CLUSTER — both complementary seatings clean, first-named variant wins
    both (for) or loses both (against); every other seed (1-1, any tie
    combination, dirty or missing mate) is SPLIT and never inferred. The
    exact one-sided binomial runs over the seed-level for/against counts
    and serializes as the integer pair p_num/p_den (the float prints only
    here). Per-seating and match W/L/T are DESCRIPTIVE only.
    Dirty matches are listed and excluded."""
    by_pair: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        by_pair.setdefault(
            tuple(sorted((r["variant_p0"], r["variant_p1"]))), []).append(r)

    per_pair: dict[str, dict] = {}
    lines: list[str] = ["== head-to-head =="]
    for (a, b), prows in sorted(by_pair.items()):
        dirty = [r for r in prows if is_dirty(r)]
        clean = [r for r in prows if not is_dirty(r)]

        # inferential: complete (pair, seed) clusters
        by_seed: dict[int, list[dict]] = {}
        for r in clean:
            by_seed.setdefault(r["seed"], []).append(r)
        for_a = against_a = split = 0
        for srows in by_seed.values():
            complete = (len(srows) == 2
                        and len({r["variant_p0"] for r in srows}) == 2)
            winners = [_winner(r) for r in srows]
            if complete and all(w == a for w in winners):
                for_a += 1
            elif complete and all(w == b for w in winners):
                against_a += 1
            else:
                split += 1

        # descriptive (non-inferential): raw per-seating and match W/L/T
        wins = {a: 0, b: 0}
        ties = 0
        seatings = {
            f"{a}@p0": {a: 0, b: 0, "ties": 0},
            f"{b}@p0": {a: 0, b: 0, "ties": 0},
        }
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

        diff_sum = {}
        for v in (a, b):
            diff_sum[v] = sum(
                r["value_differential_p0"] if r["variant_p0"] == v
                else r["value_differential_p1"] for r in clean)

        entry = {
            "matches": len(prows), "clean": len(clean),
            "dirty": [r["match_id"] for r in dirty],
            "hypothesis": f"{a}>{b}",
            "per_seed": {"for": for_a, "against": against_a, "split": split},
            "p_num": for_a, "p_den": for_a + against_a,
            "diff_sum": diff_sum, "diff_count": len(clean),
            "per_seating_descriptive": seatings,
            "match_wins_descriptive": dict(wins),
            "match_ties_descriptive": ties,
        }
        per_pair[f"{a}-vs-{b}"] = entry

        lines.append(f"== {a} vs {b} (H: {a}>{b}) ==")
        lines.append(f"   matches={len(prows)} clean={len(clean)} "
                     f"dirty={len(dirty)}")
        for r in dirty:
            lines.append(f"   DIRTY: {r['match_id']} viol={r['violations']} "
                         f"rej={r.get('rejections_p0')}/"
                         f"{r.get('rejections_p1')}")
        lines.append(f"   seed clusters (inferential): for={for_a} "
                     f"against={against_a} split={split}")
        if entry["p_den"]:
            p_text = _binom_p_ge(entry["p_den"], entry["p_num"])
            lines.append(f"   one-sided exact binomial p over decidable "
                         f"seeds (H: {a}>{b}) = {p_text:.4f} "
                         f"({for_a}/{entry['p_den']})")
        else:
            lines.append("   no decidable seed clusters (for=0 against=0)")
        lines.append(f"   match wins (descriptive): {a} {wins[a]} / "
                     f"{b} {wins[b]} / ties {ties}")
        for seat_key in (f"{a}@p0", f"{b}@p0"):
            s = seatings[seat_key]
            lines.append(f"   seating {seat_key} (descriptive): "
                         f"{a} {s[a]} / {b} {s[b]} / tie {s['ties']}")
        lines.append(f"   diff_sum (descriptive): {a}={diff_sum[a]:+d}/"
                     f"{len(clean)} {b}={diff_sum[b]:+d}/{len(clean)}")
    return per_pair, "\n".join(lines)


def aggregate(rows: list[dict], names: list[str]) -> dict[str, dict]:
    """Per variant over CLEAN matches, integers only (Codex A10): the
    win-rate serializes as wins/matches, the differential as diff_sum/
    matches — floats are a stdout concern. Dirty match counts are
    reported, never inferred from."""
    clean = [r for r in rows if not is_dirty(r)]
    out: dict[str, dict] = {}
    for v in sorted(names):
        mine = [r for r in clean if v in (r["variant_p0"], r["variant_p1"])]
        winners = [_winner(r) for r in mine]
        wins = winners.count(v)
        ties = winners.count(None)
        out[v] = {
            "matches": len(mine), "wins": wins,
            "losses": len(mine) - wins - ties, "ties": ties,
            "diff_sum": sum(
                r["value_differential_p0"] if r["variant_p0"] == v
                else r["value_differential_p1"] for r in mine),
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
        }
    return out


def _aggregate_text(agg: dict[str, dict]) -> str:
    lines = ["== aggregate (clean matches only) =="]
    for v, a in sorted(agg.items()):
        wr = (f"{a['wins'] / a['matches']:.3f}" if a["matches"] else "n/a")
        md = (f"{a['diff_sum'] / a['matches']:+.1f}"
              if a["matches"] else "n/a")
        lines.append(f"  {v}: matches={a['matches']} W{a['wins']}/"
                     f"L{a['losses']}/T{a['ties']} win_rate={wr} "
                     f"mean_diff={md} diff_sum={a['diff_sum']:+d} "
                     f"decisions={a['decisions_total']} "
                     f"dirty={a['dirty_matches']}")
    return "\n".join(lines)


async def run_league(runs_root: Path,
                     population: list[tuple[str, dict[str, Any]]],
                     seeds: int, turns: int) -> dict:
    """Every unordered pair, both seatings, every seed; writes
    runs_root/league-results.json {population, results, per_pair,
    aggregate} (deterministic, integer-valued) plus the NON-contractual
    runs_root/telemetry.json wall-clock sidecar."""
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
    # A2: wall_ms never enters the deterministic doc — it lives in stdout
    # and the non-contractual telemetry sidecar
    doc = {
        "population": population_manifest(population),
        "results": [{k: v for k, v in r.items() if k != "wall_ms"}
                    for r in results],
        "per_pair": per_pair,
        "aggregate": agg,
    }
    out = runs_root / "league-results.json"
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    telemetry = runs_root / "telemetry.json"
    telemetry.write_text(json.dumps(
        {"note": TELEMETRY_NOTE,
         "wall_ms": {r["match_id"]: r["wall_ms"] for r in results}},
        indent=2, sort_keys=True) + "\n")
    print(f"wrote {out} (+ {telemetry}, non-contractual)")
    print(text)
    print(_aggregate_text(agg))
    return doc


def _positive_int(text: str) -> int:
    """A6: a zero-turn run emits an unreplayable config; refuse at parse."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") \
            from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text!r} must be >= 1")
    return value


async def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=_positive_int, default=5)
    ap.add_argument("--turns", type=_positive_int, default=40)
    ap.add_argument("--runs-root", type=Path, default=Path("runs/league"))
    ap.add_argument("--variant", action="append", default=[], metavar="NAME:K=V,...",
                    help="override/add a population entry (values parsed as "
                         "int when possible; case_base=<path> loads the "
                         "artifact)")
    ap.add_argument("--population", "--only", dest="population", default=None,
                    metavar="A,B,...",
                    help="run ONLY these population members (comma list of "
                         "names; unknown names refuse)")
    opts = ap.parse_args(argv)
    try:
        population = resolve_population(opts.variant)
    except ValueError as exc:  # A5: name collisions refuse via usage error
        ap.error(str(exc))
    if opts.population is not None:
        keep = [n.strip() for n in opts.population.split(",") if n.strip()]
        known = {n for n, _ in population}
        unknown = [n for n in keep if n not in known]
        if unknown:
            ap.error(f"--population names not in the population: {unknown}")
        population = [(n, kw) for n, kw in population if n in keep]
    print("population: " + ", ".join(
        f"{n}({','.join(f'{k}={v}' for k, v in sorted(kw.items()))})"
        for n, kw in population))
    await run_league(opts.runs_root, population, opts.seeds, opts.turns)


if __name__ == "__main__":
    asyncio.run(main())
