#!/usr/bin/env python
"""Per-bot deep-dive audit CLI: one glm-5.3-flash analyst per seat label.

    uv run python scripts/research_deep_dive.py \
        --results runs/research/batch-003/results.json \
        --runs-root runs/research/batch-003 \
        --config configs/research/batch-003.yaml \
        --out research/iterations/003/deep

Writes <out>/<label>.json per seat label (verdict or typed failure) and
prints one line per label. Fail-soft per label: one transport failure
never stops the other audits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from civ_arena.research.deepdive import (
    audit_facts,
    audit_prompt,
    doctrine_docs_for,
    validate_verdict,
)
from civ_arena.research.flash import FlashClient, spec_from_env


async def main_async(args: argparse.Namespace) -> int:
    results = json.loads(Path(args.results).read_text())
    rows = results["rows"]
    batch = yaml.safe_load(Path(args.config).read_text())["batch"]
    roster = batch["roster"]
    labels: list[str] = [entry if isinstance(entry, str) else entry["spec_id"]
                         for entry in roster]
    if args.label:
        labels = [label for label in labels if label == args.label]
        if not labels:
            print(f"no seat label {args.label!r} in {args.config}")
            return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    flash_dir = out_dir / "flash"
    flash_dir.mkdir(exist_ok=True)

    client = FlashClient(spec_from_env(),
                         spend_path=flash_dir / "spend.jsonl")
    failures = 0
    try:
        if not await client.probe():
            print("probe failed — flash lane unreachable; writing typed "
                  "no_key/http failures per label")
        for label in labels:
            facts = audit_facts(label, rows, Path(args.runs_root))
            docs = doctrine_docs_for(label, roster)
            prompt = audit_prompt(facts, docs)
            result = await client.call_json(prompt, purpose=f"deep:{label}")
            verdict: dict[str, Any] = {}
            if result.status == "ok":
                clean, problem = validate_verdict(result.data, label)
                if clean is not None:
                    verdict = {"status": "ok", "facts_summary": {
                        "games": facts["games"], "wins": facts["wins"],
                        "mean_rank": facts["mean_rank"]},
                        "verdict": clean,
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                        "model": result.model}
                else:
                    failures += 1
                    verdict = {"status": "parse_error", "problem": problem,
                               "raw": result.raw}
            else:
                failures += 1
                verdict = {"status": result.status,
                           "input_tokens": result.input_tokens,
                           "output_tokens": result.output_tokens,
                           "model": result.model}
            path = out_dir / f"{label}.json"
            path.write_text(json.dumps(verdict, sort_keys=True, indent=1))
            score = (verdict.get("verdict", {})
                     .get("execution_score_0_10", "-"))
            executing = verdict.get("verdict", {}).get("executing_strategy")
            print(f"{label:16s} status={verdict['status']:13s} "
                  f"executing={executing!s:5s} score={score} -> {path.name}")
    finally:
        await client.aclose()
    return 1 if failures == len(labels) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True)
    parser.add_argument("--runs-root", required=True)
    parser.add_argument("--config", required=True,
                        help="batch yaml (roster = labels + pivot specs)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--label", default=None,
                        help="audit a single seat label only")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
