"""Authoritative V2 match CLI.

    uv run python -m civ_arena.match configs/duel.yaml [--run-dir runs]
        [--resume]

New matches always write a fresh V2 episode. ``--resume`` verifies the
terminal parent read-only and creates a hash-named child; it never truncates,
appends to, or rewrites the parent ledger.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from civ_arena.config import load_config
from civ_arena.v2.arena import ArenaV2
from civ_arena.v2.ledger import verify_ledger_v2


async def _main_async(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="civ-arena-match")
    ap.add_argument("config", type=Path)
    ap.add_argument("--run-dir", type=Path, default=Path("runs"))
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--crash-after-turn", type=int, default=None,
                    help=argparse.SUPPRESS)
    opts = ap.parse_args(argv)

    spec = load_config(opts.config)
    if opts.crash_after_turn is not None and opts.resume:
        raise SystemExit("--crash-after-turn cannot be combined with --resume")
    if opts.crash_after_turn is not None and spec.scored:
        raise SystemExit("scored V2 matches cannot inject a recovered crash")

    parent_dir: Path | None = None
    if opts.resume:
        parent_dir = opts.run_dir / spec.match_id
        verified = verify_ledger_v2(parent_dir)
        child_id = f"{spec.match_id}-child-{verified.terminal_event_hash[:12]}"
        run_dir = opts.run_dir / child_id
        print(
            f"resuming {spec.match_id} as child {child_id} "
            f"of terminal {verified.terminal_event_hash[:12]}"
        )
    else:
        run_dir = opts.run_dir / spec.match_id

    summary = await ArenaV2(
        run_dir,
        spec,
        parent_episode_dir=parent_dir,
    ).run(recover_after_turn=opts.crash_after_turn)
    _print_summary(summary)
    return 0 if summary["termination_reason"] == "success" else 3


def _print_summary(summary: dict) -> None:
    print(f"\nmatch {summary['match_id']} finished at turn {summary['final_turn']}")
    print(f"episode: {summary['episode_id']}")
    print(f"termination: {summary['termination_reason']}")
    if summary.get("aborted"):
        print(f"ABORTED: {summary['aborted']}")
    print(f"violations: {summary['violations_total']}")
    print(f"terminal event hash: {summary['terminal_event_hash']}")
    if summary.get("parent_episode_id"):
        print(
            "parent: "
            f"{summary['parent_episode_id']}@"
            f"{summary['parent_terminal_event_hash']}"
        )
    print("telemetry:")
    for agent, doc in sorted(summary.get("telemetry", {}).items()):
        line = (f"  {agent}: calls={doc['total_calls']} "
                f"errors={doc['total_errors']} ms={doc['total_ms']}")
        if doc.get("model"):
            line += (f" model={doc['model']} tokens={doc['input_tokens']}in"
                     f"/{doc['output_tokens']}out")
        print(line)


def main() -> None:
    raise SystemExit(asyncio.run(_main_async()))


if __name__ == "__main__":
    main()
