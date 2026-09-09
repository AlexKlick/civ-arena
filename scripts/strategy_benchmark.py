#!/usr/bin/env python3
"""Bounded legacy-simulator diagnostic; never authorizes a learning campaign.

Each map is one cluster containing both seatings. The artifact records exact
integer descriptives and validity, not significance or agent promotion claims.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import time
from pathlib import Path

import yaml

from civ_arena.agents.runtime import AgentProfile, build_runtime
from civ_arena.arena.coordinator import Arena
from civ_arena.config import AgentSpec, MatchSpec
from civ_arena.game.sim.value import DEFAULT_WEIGHTS, score_differential
from civ_arena.planner.runtime import PlannerRuntime

ROOT = Path(__file__).resolve().parents[1]
VARIANTS = {
    "turtler": {"policy": "turtler", "seed": 22},
    "expansionist": {"policy": "expansionist", "seed": 37},
    "mcts-b4": {"policy": "planner", "seed": 7, "method": "mcts", "budget": 4},
    "mcts-b8": {"policy": "planner", "seed": 7, "method": "mcts", "budget": 8},
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n")


def validate_config(config: dict) -> None:
    if set(config) != {"schema", "purpose", "turns", "seeds", "pairs"}:
        raise ValueError("unexpected config keys")
    if config["schema"] != 1 or config["purpose"] != "legacy-simulator-diagnostic":
        raise ValueError("only the diagnostic scope is supported")
    if type(config["turns"]) is not int or not 1 <= config["turns"] <= 40:
        raise ValueError("turns must be in 1..40")
    seeds = config["seeds"]
    if not isinstance(seeds, dict) or set(seeds) != {"development", "held_out"}:
        raise ValueError("explicit development and held_out splits required")
    all_seeds = []
    for block in seeds.values():
        if not isinstance(block, list) or not block:
            raise ValueError("seed splits must be nonempty lists")
        if any(type(seed) is not int or seed < 0 for seed in block):
            raise ValueError("seeds must be nonnegative integers")
        all_seeds.extend(block)
    if len(set(all_seeds)) != len(all_seeds):
        raise ValueError("duplicate or overlapping seeds")
    pairs = config["pairs"]
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("pairs must be nonempty")
    identities = set()
    for pair in pairs:
        if (not isinstance(pair, list) or len(pair) != 2
                or any(not isinstance(v, str) or v not in VARIANTS for v in pair)
                or pair[0] == pair[1]):
            raise ValueError("each pair requires two distinct frozen variants")
        identity = tuple(sorted(pair))
        if identity in identities:
            raise ValueError("duplicate pair")
        identities.add(identity)
    if len(all_seeds) * len(pairs) * 2 > 24:
        raise ValueError("diagnostic capped at 24 scheduled matches; campaign gate remains closed")


def schedule(config: dict, split: str) -> list[dict]:
    validate_config(config)
    if split not in {"development", "held_out", "all"}:
        raise ValueError("invalid split")
    rows = []
    for block, seeds in config["seeds"].items():
        if split != "all" and split != block:
            continue
        for seed in seeds:
            for a, b in config["pairs"]:
                for seating, variants in (("ab", [a, b]), ("ba", [b, a])):
                    rows.append({"match_id": f"{block}-{seed}-{a}-vs-{b}-{seating}",
                                 "split": block, "seed": seed, "pair": [a, b],
                                 "seating": seating, "variants": variants,
                                 "turns": config["turns"]})
    return rows


def analyze(expected: list[dict], rows: list[dict]) -> dict:
    """Any dirty, absent, duplicated, or schedule-mismatched row blocks aggregates."""
    errors = []
    indexed = {}
    for row in rows:
        key = row["match_id"]
        if key in indexed:
            errors.append(f"duplicate match: {key}")
        indexed[key] = row
    if set(indexed) != {row["match_id"] for row in expected}:
        errors.append("observed match inventory differs from schedule")
    for item in expected:
        row = indexed.get(item["match_id"])
        if row is None:
            continue
        if any(row.get(key) != value for key, value in item.items()):
            errors.append(f"schedule mismatch: {item['match_id']}")
        if row.get("errors") or row.get("violations") or row.get("rejections"):
            errors.append(f"dirty match: {item['match_id']}")
        if row.get("completed_turns") != item["turns"]:
            errors.append(f"incomplete horizon: {item['match_id']}")
    if errors:
        return {"valid": False, "errors": errors, "descriptives": None}
    grouped = {}
    for row in rows:
        key = (row["split"], tuple(row["pair"]), row["seed"])
        grouped.setdefault(key, {})[row["seating"]] = row
    descriptives = {}
    for (split, pair, _seed), seats in grouped.items():
        if set(seats) != {"ab", "ba"}:
            return {"valid": False, "errors": ["incomplete seating cluster"],
                    "descriptives": None}
        key = f"{split}:{pair[0]}-vs-{pair[1]}"
        d = descriptives.setdefault(key, {
            "seed_clusters": 0, "a_wins_both": 0, "b_wins_both": 0,
            "seat_split": 0, "tie_in_cluster": 0, "p0_wins": 0, "p1_wins": 0,
            "ties": 0, "a_diff_sum": 0, "a_diff_count": 0,
            "p0_diff_sum": 0, "matches": 0,
        })
        ab, ba = (seats[s]["value_differential_p0"] for s in ("ab", "ba"))
        d["seed_clusters"] += 1
        category = ("a_wins_both" if ab > 0 > ba else "b_wins_both" if ab < 0 < ba
                    else "tie_in_cluster" if not ab or not ba else "seat_split")
        d[category] += 1
        d["a_diff_sum"] += ab - ba
        d["a_diff_count"] += 2
        for diff in (ab, ba):
            d["p0_wins" if diff > 0 else "p1_wins" if diff < 0 else "ties"] += 1
            d["p0_diff_sum"] += diff
            d["matches"] += 1
    return {"valid": True, "errors": [], "descriptives": descriptives}


def audit_match(run_dir: Path, item: dict) -> dict:
    records = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    summary = json.loads((run_dir / "summary.json").read_text())
    errors = []
    if any(rec.get("seq") != i or rec.get("match_id") != item["match_id"]
           for i, rec in enumerate(records)):
        errors.append("event sequence or identity mismatch")
    starts = [r for r in records if r["kind"] == "MATCH_START"]
    ends = [r for r in records if r["kind"] == "MATCH_END"]
    if len(starts) != 1 or len(ends) != 1 or records[-1]["kind"] != "MATCH_END":
        errors.append("missing or duplicate terminal/start event")
    elif ends[0].get("summary") != summary:
        errors.append("event/summary mismatch")
    if summary.get("aborted") or not summary.get("final_state_hash"):
        errors.append("aborted or unavailable final state")
    expected_order = [(turn, pid) for turn in range(1, item["turns"] + 1) for pid in (0, 1)]
    for kind in ("LEASE_GRANT", "LEASE_RELEASE", "TURN_END"):
        observed = [(r["turn"], r["player_id"]) for r in records if r["kind"] == kind]
        if observed != expected_order:
            errors.append(f"incomplete or unordered {kind}")
    violations = sum(r["kind"] == "VIOLATION" for r in records)
    if violations != summary.get("violations_total"):
        errors.append("violation summary mismatch")
    rejected = [r for r in records
                if r["kind"] == "TOOL_RESULT" and r.get("status") == "rejected"]
    rejections = len(rejected)
    rejection_rows = [{k: r.get(k) for k in ("seq", "turn", "player_id", "tool", "rejection")}
                      for r in rejected]
    return {**item, "errors": errors, "completed_turns": summary["final_turn"],
            "violations": violations, "rejections": rejections,
            "rejection_rows": rejection_rows,
            "rejections_per_seat": {str(pid): sum(r["player_id"] == pid for r in rejected)
                                    for pid in (0, 1)},
            "value_differential_p0": score_differential(summary["scores"], 0),
            "final_state_hash": summary["final_state_hash"],
            "artifacts": {name: sha(run_dir / name)
                          for name in ("events.jsonl", "summary.json", "config.yaml")}}


def source_identity() -> dict:
    files = sorted(p for folder in ("src", "scripts") for p in (ROOT / folder).rglob("*.py"))
    files += [ROOT / "pyproject.toml", ROOT / "uv.lock"]
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
    return {"dirty": bool(status.strip()), "git_status": status.splitlines(),
            "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                            text=True).strip(),
            "files": {str(p.relative_to(ROOT)): sha(p) for p in files if p.exists()}}


async def run_match(run_dir: Path, item: dict) -> dict:
    specs, runtimes = [], {}
    for pid, variant in enumerate(item["variants"]):
        kw = VARIANTS[variant]
        agent_id = f"{variant}-p{pid}"
        specs.append(AgentSpec(agent_id=agent_id, player_id=pid,
                               policy=kw["policy"], seed=kw["seed"]))
        if kw["policy"] == "planner":
            runtimes[pid] = PlannerRuntime(pid, kw["seed"], method=kw["method"],
                                           budget=kw["budget"], proposer=None,
                                           case_base=None, bandit=None, weights=None)
        else:
            runtimes[pid] = build_runtime(AgentProfile(
                agent_id=agent_id, player_id=pid, policy=kw["policy"], seed=kw["seed"]))
    spec = MatchSpec(match_id=item["match_id"], seed=item["seed"],
                     max_turns=item["turns"], adapter="simulator",
                     watchdog_mode="abort", violation_limit=1, checkpoint_every=5,
                     agents=specs)
    run_dir.mkdir(exist_ok=False)
    (run_dir / "config.yaml").write_text(yaml.safe_dump({
        "match": {"match_id": spec.match_id, "seed": spec.seed,
                  "max_turns": spec.max_turns, "adapter": "simulator",
                  "watchdog_mode": spec.watchdog_mode, "violation_limit": 1,
                  "checkpoint_every": 5},
        "agents": [{"agent_id": a.agent_id, "player_id": a.player_id,
                    "policy": a.policy, "seed": a.seed} for a in specs],
    }, sort_keys=True))
    arena = Arena(run_dir, spec, runtimes=runtimes)
    try:
        await arena.run()
    finally:
        arena.log.close()
    row = audit_match(run_dir, item)
    row["planner_work"] = {}
    for pid, rt in runtimes.items():
        if isinstance(rt, PlannerRuntime):
            trace_path = run_dir / f"planner-p{pid}-trace.json"
            trace_path.write_text(json.dumps(rt.trace, sort_keys=True, indent=2) + "\n")
            row["artifacts"][trace_path.name] = sha(trace_path)
            row["planner_work"][str(pid)] = {
                "decisions": len(rt.trace),
                "nodes_expanded": sum(t["nodes_expanded"] for t in rt.trace),
                "rollout_budget_per_decision": rt.budget,
            }
    return row


async def run_pilot(config: dict, out: Path, split: str) -> dict:
    expected = schedule(config, split)
    # A new directory, including an empty existing one, must never be reused.
    # Resolve aliases before creation; source trees are never an output target.
    out = out.resolve()
    if out.is_relative_to(ROOT) and not out.is_relative_to(ROOT / "runs"):
        raise ValueError("output inside repository must be under runs/")
    out.mkdir(parents=True, exist_ok=False)
    source = source_identity()
    write_json(out / "manifest.json", {"config": config, "split": split,
               "schedule": expected, "variants": VARIANTS, "weights": DEFAULT_WEIGHTS,
               "source": source, "authority": "events.jsonl; legacy V1 simulator",
               "campaign_gate": "not satisfied by this diagnostic"})
    rows, telemetry = [], []
    failure = None
    try:
        for item in expected:
            started = time.monotonic()
            row = await run_match(out / item["match_id"], item)
            rows.append(row)
            write_json(out / "partial-results.json", {"rows": rows})
            telemetry.append({"match_id": item["match_id"],
                              "wall_ms": int((time.monotonic() - started) * 1000)})
            print(f"{item['match_id']}: turns={row['completed_turns']} "
                  f"diff_p0={row['value_differential_p0']} rejections={row['rejections']} "
                  f"violations={row['violations']} errors={row['errors']}", flush=True)
            if row["errors"] or row["violations"] or row["rejections"]:
                failure = "dirty match; stopped and preserved"
                break
    except (Exception, asyncio.CancelledError) as exc:
        failure = f"{type(exc).__name__}: {exc}"
    audit = analyze(expected, rows)
    final_source = source_identity()
    if source["files"] != final_source["files"]:
        failure = "source changed during execution"
    if failure:
        audit = {"valid": False, "errors": [*audit["errors"], failure], "descriptives": None}
    result = {"schema": 1, "scope": "legacy-simulator-diagnostic",
              "promotion_allowed": False, "rows": rows, "audit": audit,
              "audit_scope": "immediate execution only; no offline revalidation or replay",
              "final_source": final_source,
              "manifest_sha256": sha(out / "manifest.json")}
    write_json(out / "results.json", result)
    write_json(out / "telemetry.json", {"non_contractual": True, "matches": telemetry})
    print(json.dumps({"valid": audit["valid"], "scheduled": len(expected),
                      "completed": len(rows), "errors": audit["errors"]}), flush=True)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=ROOT / "configs/strategy-benchmark-pilot.json")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--split", choices=("development", "held_out", "all"), default="development")
    args = ap.parse_args()
    config = json.loads(args.config.read_text())
    result = asyncio.run(run_pilot(config, args.out, args.split))
    return 0 if result["audit"]["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
