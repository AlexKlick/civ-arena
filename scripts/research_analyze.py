#!/usr/bin/env python
"""Flash analysis legs: propose a roster | analyze a finished batch.

    uv run python scripts/research_analyze.py brief-roster \
        --results runs/research/batch-001/results.json \
        --briefs-dir research/briefs --out research/iterations/000
    uv run python scripts/research_analyze.py analyze \
        --results runs/research/batch-001/results.json \
        --runs-root runs/research/batch-001 \
        --briefs-dir research/briefs --hypotheses research/hypotheses.json \
        --out research/iterations/001

Both legs call the provider-portable flash client (research/flash.py),
fail soft to typed-empty outputs, and persist the raw model text on
parse errors for diagnosis. Everything printed here is what the supervised
loop reviews before the next batch.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from civ_arena.game.sim.value import DEFAULT_WEIGHTS
from civ_arena.research.digest import digest_batch
from civ_arena.research.flash import FlashClient, FlashResult, spec_from_env
from civ_arena.research.roster import PROPOSER_CONTRACT, proposer_rules, validate_proposal

MECHANICS_NOTES = """Sim mechanics the doctrines act within (ancient-era spike sim):
- Score = 100*cities + 20*population + 30*techs + 10*units + 1*gold; matches
  are scored own-minus-strongest-rival, and 4-seat games rank all four seats.
- There is NO city capture: military can only kill units (10 pts each).
- Techs do not change yields; each tech is +30 and the 8-tech tree totals the
  same for everyone — research ORDER only shifts unit unlocks
  (SPEARMAN needs BRONZE_WORKING, ARCHER needs ARCHERY).
- Production ~5/turn per pop-1 city; SETTLER 80, WARRIOR 40, SCOUT 25,
  SPEARMAN 50, ARCHER 50, MONUMENT 50 (+2 gold/turn), GRANARY 60 (+1 gold,
  raises per-city pop cap 7->9), WALLS 70 (defhp read NOWHERE — inert).
- Purchases cost 2x and are gated turn%3==0 and gold>=120.
- Units: mv 2 (scout 3), attack zeroes movement; fortified units heal 10/turn
  (movement must be full at phase start), melee defenders counter, archers
  hit at range 2. Fortify gives +4 defense.
- Founding needs dist > 2 from every city; each new city compounds pop and
  science (science = 2*pop per city), so cities dominate everything.
- 4-seat map: radius 7, starts at hex_dist 6, every seat has one rival at
  distance 6 and two at 12; corridors carved between all neighbouring starts."""


def _read_briefs(briefs_dir: Path) -> str:
    parts = []
    for path in sorted(briefs_dir.glob("*.md")):
        parts.append(f"=== BRIEF: {path.stem} ===\n{path.read_text()}")
    return "\n\n".join(parts)


def _load_hypotheses(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text())


async def _call(client: FlashClient, prompt: str, purpose: str,
                out_dir: Path, system: str | None = None) -> FlashResult:
    result = await client.call_json(prompt, purpose=purpose, system=system)
    if result.status == "parse_error" and result.raw:
        raw_dir = out_dir / "flash" / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"{result.call_id}.txt").write_text(result.raw)
    return result


def cmd_brief_roster(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = json.loads(Path(args.results).read_text())
    agg = results["aggregates"]
    prompt = "\n".join([
        "You are the strategy proposer in a civ-arena research loop.",
        "Task: propose EXACTLY 4 novel scripted doctrines for 4-player games.",
        "The roster will be implemented literally from your JSON — be precise.",
        "", MECHANICS_NOTES,
        f"\nScore weights: {DEFAULT_WEIGHTS}",
        f"\nBaseline batch aggregates (known doctrines, 16 games):\n"
        f"{json.dumps(agg['per_strategy'], indent=1)}",
        f"Seat decomposition (seat-luck detector):\n"
        f"{json.dumps(agg['seat_decomposition'], indent=1)}",
        "\nStrategy review briefs follow; mine them for what works, what "
        "fails, and which parameter axes are unexplored.",
        _read_briefs(Path(args.briefs_dir)),
        "\nRespond with ONLY this JSON object (no prose):",
        PROPOSER_CONTRACT,
        proposer_rules(),
        "Design intent: the 4 doctrines should be DISTINCT along the "
        "expansion/economy/military/tempo axes, each with a different "
        "failure mode, and none should be a copy of the baseline doctrines.",
    ])
    result = asyncio.run(_dispatch(prompt, "brief-roster", out_dir))
    accepted, dropped = ([], ["no data"])
    if result.status == "ok":
        accepted, dropped = validate_proposal(result.data)
    doc = {
        "status": result.status,
        "accepted": accepted,
        "dropped": dropped,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "model": result.model,
    }
    (out_dir / "proposal.json").write_text(json.dumps(doc, indent=1))
    print(json.dumps(doc, indent=1))


async def _dispatch(prompt: str, purpose: str, out_dir: Path) -> FlashResult:
    client = FlashClient(spec_from_env(),
                         spend_path=out_dir / "flash" / "spend.jsonl")
    try:
        return await _call(client, prompt, purpose, out_dir)
    finally:
        await client.aclose()


ANALYSIS_CONTRACT = """{
  "findings": [{"id": "F1", "kind": "strategy|variance|bug",
                "claim": "...", "evidence": ["<match_id>"],
                "confidence": "low|med|high"}],
  "hypotheses": [{"id": "H1", "statement": "...", "prediction": "...",
                  "test": {"kind": "param_change|roster_swap|seed_plan",
                           "doctrine": "...", "param": "...", "value": 0}}],
  "next_batch": {"roster": ["d1", "d2", "d3", "d4"],
                 "param_changes": [{"doctrine": "d1", "param": "max_cities",
                                    "value": 5}],
                 "seeds": 4, "games": 16, "rationale": "..."},
  "ledger_updates": [{"hypothesis_id": "H0",
                      "status": "supported|refuted|unclear", "note": "..."}]
}"""


def cmd_analyze(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = json.loads(Path(args.results).read_text())
    runs_root = Path(args.runs_root)
    rows = results["rows"]
    digests = digest_batch(rows, runs_root)
    hypotheses = _load_hypotheses(Path(args.hypotheses))
    prompt = "\n".join([
        "You are the analyst in a civ-arena research loop. Study the game",
        "records below and evolve our understanding of the strategies.",
        "", MECHANICS_NOTES,
        f"\nBatch aggregates:\n{json.dumps(results['aggregates'], indent=1)}",
        "\nPer-match digests (score trajectory sampled every 5 turns; "
        "c=cities p=population t=techs u=units g=gold, =scalar):",
        digests,
        "\nOpen hypotheses from the ledger:",
        json.dumps(hypotheses, indent=1) if hypotheses else "[]",
        "\nStrategy briefs (doctrine mechanics):",
        _read_briefs(Path(args.briefs_dir)),
        "\nRespond with ONLY this JSON object (no prose):",
        ANALYSIS_CONTRACT,
        "Rules: findings must cite match_ids from the digests as evidence; "
        "hypotheses must be falsifiable with a concrete prediction; "
        "next_batch must stay within 16 games and 4 seeds; variance "
        "findings should name the axis (seat, seed, or matchup); ranks in "
        "4-seat games are descriptive — do not claim significance, propose "
        "a paired 2-seat follow-up instead when a gap looks decisive.",
    ])
    result = asyncio.run(_dispatch(prompt, "analyze", out_dir))
    doc = {
        "status": result.status,
        "data": result.data if result.status == "ok" else {},
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "model": result.model,
    }
    (out_dir / "analysis.json").write_text(json.dumps(doc, indent=1))
    print(json.dumps(doc, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    roster_p = sub.add_parser(
        "brief-roster", help="flash proposes a 4-doctrine roster")
    roster_p.add_argument("--results", required=True)
    roster_p.add_argument("--briefs-dir", default="research/briefs")
    roster_p.add_argument("--out", required=True)

    analyze_p = sub.add_parser(
        "analyze", help="flash analyzes a finished batch")
    analyze_p.add_argument("--results", required=True)
    analyze_p.add_argument("--runs-root", required=True)
    analyze_p.add_argument("--briefs-dir", default="research/briefs")
    analyze_p.add_argument("--hypotheses", default="research/hypotheses.json")
    analyze_p.add_argument("--out", required=True)

    args = parser.parse_args()
    if args.command == "brief-roster":
        cmd_brief_roster(args)
    else:
        cmd_analyze(args)


if __name__ == "__main__":
    sys.exit(main())
