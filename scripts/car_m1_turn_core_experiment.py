#!/usr/bin/env python3
"""Generate and run the immutable CAR-M1 V2 A/B/C/D fake corpus."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from civ_arena.experiments.turn_core import (
    build_fixture_manifest_v2,
    run_experiment_v2,
    validate_fixture_manifest_v2,
    write_immutable_json,
)


async def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--manifest",
        type=Path,
        default=Path("experiments/car-m1-v2/fixture-manifest.json"),
    )
    ap.add_argument(
        "--result",
        type=Path,
        default=Path("experiments/car-m1-v2/deterministic-results.json"),
    )
    ap.add_argument("--generate", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--limit", type=int)
    opts = ap.parse_args(argv)
    if not opts.generate and not opts.run:
        ap.error("select --generate and/or --run")

    if opts.generate:
        manifest = await build_fixture_manifest_v2()
        write_immutable_json(opts.manifest, manifest)
        print(
            f"fixture manifest: {manifest['manifest_sha256']} "
            f"({manifest['body']['fixture_counts']['total']} fixtures)"
        )
    else:
        manifest = json.loads(opts.manifest.read_text(encoding="utf-8"))
        validate_fixture_manifest_v2(manifest)

    if opts.run:
        result = await run_experiment_v2(manifest, limit=opts.limit)
        write_immutable_json(opts.result, result)
        totals = result["body"]["control_failure_totals"]
        print(f"result: {result['result_sha256']}")
        for treatment, total in sorted(totals.items()):
            print(f"  {treatment}: {total}")
        print(result["body"]["deterministic_verdict"])
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()

