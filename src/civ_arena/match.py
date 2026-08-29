"""Match CLI: run or resume a match from a config file.

    uv run python -m civ_arena.match configs/duel.yaml [--run-dir runs]
        [--crash-after-turn K] [--resume]
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from civ_arena.arena.checkpoints import CheckpointManager
from civ_arena.arena.coordinator import Arena
from civ_arena.config import load_config


async def _main_async(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="civ-arena-match")
    ap.add_argument("config", type=Path)
    ap.add_argument("--run-dir", type=Path, default=Path("runs"))
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--crash-after-turn", type=int, default=None,
                    help="test-only: SIGKILL self just after the turn-K+1 lease")
    opts = ap.parse_args(argv)

    spec = load_config(opts.config)
    run_dir = opts.run_dir / spec.match_id
    arena = Arena(run_dir, spec)

    resume_state = None
    if opts.resume:
        ckpt = CheckpointManager(run_dir / "checkpoints",
                                 spec.checkpoint_every).latest()
        if ckpt is None:
            raise SystemExit(f"no checkpoint found under {run_dir}/checkpoints")
        if not CheckpointManager.verify_log_prefix(arena.log, ckpt):
            raise SystemExit("log prefix does not match the latest checkpoint — "
                             "refusing to resume")
        resume_state = ckpt
        print(f"resuming {spec.match_id} from turn {ckpt.turn} "
              f"(seq {ckpt.seq}, instance {ckpt.game_instance_id_of_origin})")

    summary = await arena.run(resume_state=resume_state,
                              crash_after_turn=opts.crash_after_turn)
    _print_summary(summary)
    return 0 if not summary.get("aborted") else 3


def _print_summary(summary: dict) -> None:
    print(f"\nmatch {summary['match_id']} finished at turn {summary['final_turn']}")
    if summary.get("aborted"):
        print(f"ABORTED: {summary['aborted']}")
    print(f"violations: {summary['violations_total']}")
    print(f"final state hash: {summary['final_state_hash']}")
    print("scores:")
    for civ, score in sorted(summary.get("scores", {}).items()):
        print(f"  {civ}: {score}")
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
