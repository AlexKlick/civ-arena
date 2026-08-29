"""Replay: re-run a match from its event log through the SAME machinery.

Replay is a deterministic re-execution, not a log applier: the recorded tool
calls are re-issued through a fresh Arena (same seed, same chaos schedule,
same watchdog config), and EVERY intermediate ``after_state_hash`` in the
replayed log must match the live one. A tampered log diverges loudly.

    uv run python -m civ_arena.replay runs/<match_id> --config configs/duel.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from civ_arena.arena.coordinator import Arena
from civ_arena.config import MatchSpec

# positional argument order per tool (mirrors session.tools signatures)
ARG_ORDER: dict[str, list[str]] = {
    "get_overview": [],
    "get_units": [],
    "get_cities": [],
    "get_visible_map": [],
    "get_available_research": [],
    "get_available_production": ["city_id"],
    "move_unit": ["unit_id", "dest"],
    "attack": ["unit_id", "target_id"],
    "fortify": ["unit_id"],
    "found_city": ["unit_id", "name"],
    "set_research": ["tech_id"],
    "set_city_production": ["city_id", "item_id"],
    "purchase": ["city_id", "item_id"],
    "end_turn": [],
    "write_diary": ["text"],
}


@dataclass
class RecordedCall:
    tool: str
    args: dict[str, Any]
    key: str | None


class ReplayRuntime:
    """Re-issues the recorded tool calls, in order, per turn."""

    def __init__(self, calls: list[RecordedCall]) -> None:
        self._queue: deque[RecordedCall] = deque(calls)
        self.issued = 0
        # checkpoint plumbing expects an rng on every runtime; replay draws none
        self.rng = random.Random(0)

    async def take_turn(self, facade: Any) -> None:
        while self._queue:
            call = self._queue.popleft()
            fn = getattr(facade, call.tool)
            pos = [call.args[k] for k in ARG_ORDER.get(call.tool, []) if k in call.args]
            if call.tool == "purchase" or call.key is not None:
                await fn(*pos, idempotency_key=call.key)
            else:
                await fn(*pos)
            self.issued += 1
            if call.tool == "end_turn":
                return
        # queue exhausted without a recorded end_turn (torn tail, or an agent
        # that never completed): close the phase anyway so replay reports a
        # clean DIVERGENCE instead of crashing the next begin_turn
        await facade.end_turn()


def _strip(records: list[dict[str, Any]]) -> list[tuple]:
    """Replay-comparable projection: envelope fields dropped."""
    out: list[tuple] = []
    for rec in records:
        kind = rec["kind"]
        if kind in ("TOOL_CALL", "TOOL_RESULT"):
            out.append((
                kind, rec.get("turn"), rec.get("player_id"), rec.get("agent_id"),
                rec.get("tool"), rec.get("args_digest"), rec.get("status"),
                rec.get("rejection"), rec.get("after_state_hash"),
            ))
        elif kind == "VIOLATION":
            out.append((kind, rec.get("turn"), rec.get("agent_id"),
                        json.dumps(rec.get("watchdog"), sort_keys=True)))
        elif kind in ("AMBIENT",):
            out.append((kind, rec.get("turn"), rec.get("player_id"),
                        json.dumps(rec.get("manifest"), sort_keys=True)))
        elif kind == "TURN_END":
            out.append((kind, rec.get("turn"), rec.get("state_hash")))
        else:
            continue  # MATCH_START/END, LEASE_*, CHECKPOINT, HEARTBEAT, UNAUTHORIZED
    return out


def load_calls(records: list[dict[str, Any]],
               agents_by_id: dict[str, int]) -> dict[int, list[RecordedCall]]:
    calls: dict[int, list[RecordedCall]] = {}
    for rec in records:
        if rec["kind"] != "TOOL_CALL" or not rec.get("agent_id"):
            continue
        pid = agents_by_id[rec["agent_id"]]
        calls.setdefault(pid, []).append(RecordedCall(
            tool=rec["tool"], args=dict(rec.get("args") or {}),
            key=rec.get("idempotency_key"),
        ))
    return calls


async def replay_run(run_dir: Path, spec: MatchSpec,
                     replay_dir: Path) -> dict[str, Any]:
    records = _load_records(Path(run_dir) / "events.jsonl")
    agents_by_id = {a.agent_id: a.player_id for a in spec.agents}
    calls = load_calls(records, agents_by_id)
    runtimes = {pid: ReplayRuntime(queue) for pid, queue in calls.items()}
    # EVERY configured agent gets a ReplayRuntime — an agent with zero
    # recorded calls (aborted before its first tool landed) must never fall
    # back to its configured LIVE runtime, or replay would touch the network
    for agent in spec.agents:
        if agent.player_id not in runtimes:
            runtimes[agent.player_id] = ReplayRuntime([])

    arena = Arena(replay_dir, spec, runtimes=runtimes)
    summary = await arena.run()

    live = _strip(records)
    replayed = _strip(arena.log.records())
    first_divergence: int | None = None
    for i, (a, b) in enumerate(zip(live, replayed, strict=False)):
        if a != b:
            first_divergence = i
            break
    if first_divergence is None and len(live) != len(replayed):
        first_divergence = min(len(live), len(replayed))

    return {
        "summary": summary,
        "identical": first_divergence is None,
        "first_divergence": first_divergence,
        "live_events": len(live),
        "replayed_events": len(replayed),
        "live_final_hash": _final_hash(run_dir),
        "replayed_final_hash": summary.get("final_state_hash"),
    }


def _final_hash(run_dir: Path) -> str | None:
    summary_path = Path(run_dir) / "summary.json"
    if not summary_path.exists():
        return None
    return json.loads(summary_path.read_text()).get("final_state_hash")


def _load_records(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            break
    return out


async def _main_async(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="civ-arena-replay")
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--replay-dir", type=Path, default=None)
    opts = ap.parse_args(argv)

    from civ_arena.config import load_config

    spec = load_config(opts.config)
    replay_dir = opts.replay_dir or (opts.run_dir.parent / f"{opts.run_dir.name}-replay")
    result = await replay_run(opts.run_dir, spec, replay_dir)
    if result["identical"]:
        print(f"REPLAY OK: {result['live_events']} comparable events identical; "
              f"final hash {result['replayed_final_hash']}")
        return 0
    print(f"REPLAY DIVERGED at comparable-event {result['first_divergence']} "
          f"(live {result['live_events']} vs replayed {result['replayed_events']})")
    return 4


def main() -> None:
    raise SystemExit(asyncio.run(_main_async()))


if __name__ == "__main__":
    main()
