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
    "get_strategy": [],
    "move_unit": ["unit_id", "dest"],
    "attack": ["unit_id", "target_id"],
    "fortify": ["unit_id"],
    "found_city": ["unit_id", "name"],
    "set_research": ["tech_id"],
    "set_city_production": ["city_id", "item_id", "dest"],
    "purchase": ["city_id", "item_id"],
    "end_turn": [],
    "write_diary": ["text"],
    "set_goal": ["text", "goal_id", "by_turn", "metric", "target", "status",
                 "confidence"],
    "record_prediction": ["text", "review_turn", "prediction_id", "subject_id",
                          "metric", "target", "confidence"],
    "record_lesson": ["text", "about"],
    "recall_lessons": ["query"],
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
            if call.tool == 'get_available_research' and 'research_building_briefing' in call.args:
                result = await fn(
                    research_building_briefing=call.args['research_building_briefing'])
            elif call.tool == "purchase" or call.key is not None:
                result = await fn(*pos, idempotency_key=call.key)
            else:
                result = await fn(*pos)
            self.issued += 1
            if (call.tool == "end_turn" and isinstance(result, dict)
                    and result.get("status") == "accepted"):
                return
        # queue exhausted without a recorded end_turn (torn tail, or an agent
        # that never completed): close the phase anyway so replay reports a
        # clean DIVERGENCE instead of crashing the next begin_turn
        await facade.end_turn()


def _strip(records: list[dict[str, Any]], live: bool = False) -> list[tuple]:
    """Replay-comparable projection: envelope fields dropped.

    ``recalled`` is compared, unlike the observation ``observed`` digest:
    observations re-derive from replayed game state (identical by
    construction), but a recall re-queries an EXTERNAL corpus — if the
    corpus changed between run and replay, the model would have been fed
    different lessons, and the certificate must say so, not stay green.
    ``.get`` keeps pre-M13 records comparable (None == None).

    ``live=True`` (B1, live-run mode): the live digest hashes
    (``after_state_hash`` / ``state_hash``) are sha over Civ VI engine
    digests — a domain the SIMULATOR cannot re-derive, so elementwise
    hash equality is unsatisfiable by construction. Live mode compares
    the STRUCTURAL SKELETON (kind, turn, player, agent, tool,
    args_digest, status, rejection, recalled) and excludes the hash
    fields; the declared limitation is that the live certificate is
    structural re-execution, not state equality."""
    out: list[tuple] = []
    for rec in records:
        kind = rec["kind"]
        if kind in ("TOOL_CALL", "TOOL_RESULT"):
            row = (
                kind, rec.get("turn"), rec.get("player_id"), rec.get("agent_id"),
                rec.get("tool"), rec.get("args_digest"), rec.get("status"),
                rec.get("rejection"),
            )
            if not live:
                row = row + (rec.get("after_state_hash"),)
            out.append(row + (rec.get("recalled"),))
        elif kind == "VIOLATION":
            out.append((kind, rec.get("turn"), rec.get("agent_id"),
                        json.dumps(rec.get("watchdog"), sort_keys=True)))
        elif kind in ("AMBIENT",):
            out.append((kind, rec.get("turn"), rec.get("player_id"),
                        json.dumps(rec.get("manifest"), sort_keys=True)))
        elif kind == "TURN_END":
            row: tuple = (kind, rec.get("turn"))
            if not live:
                row = row + (rec.get("state_hash"),)
            out.append(row)
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
                     replay_dir: Path, live: bool | None = None,
                     ) -> dict[str, Any]:
    """``live`` (B1): None auto-detects by the run's own summary — the
    live driver writes a ``"phase"`` key, a simulated Arena.run never
    does. Live mode (a) normalizes the live turn axis onto the replay's
    (a live dispatch attaches mid-game at engine turn N; the sim always
    starts at 1), (b) excludes undriven seats from the REPLAYED strip
    (live seat 1 is the engine's own AI — outside the referee — so its
    replayed synthetic end_turn pair is not comparable), and (c) uses
    _strip's structural skeleton (see its docstring)."""
    if live is None:
        live = _is_live_run(run_dir)
    # Codex r1 P1-6: the replay dir must never BE the source dir — the
    # wipe below would destroy the trust-root log. Refuse loudly.
    if Path(run_dir).resolve() == Path(replay_dir).resolve():
        raise ValueError(
            f"replay dir {replay_dir} is the SOURCE run dir — refusing "
            "to replay into it (the wipe would delete the trust root)")
    # A spectate run has ZERO driven tool calls (a human played the seat;
    # the harness only observed) — re-executing through an Arena would
    # fabricate a synthetic sim match. Return a structural certificate
    # before any Arena construction.
    if _is_spectate_run(run_dir):
        return _spectate_certificate(run_dir)
    # a replay dir is a DERIVED artifact, never a trust root: a stale one
    # from an earlier replay would have the Arena APPEND a second match
    # into the same events.jsonl (observed 2026-09-03: 388+308 'identical'
    # rows reported as 696) — wipe it so every replay starts from zero
    stale = Path(replay_dir) / "events.jsonl"
    if stale.exists():
        stale.unlink()
        for leftover in Path(replay_dir).glob("*-replay*"):
            leftover.unlink()
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

    # the replay Arena writes to replay_dir but resolves the recall corpus
    # from the SOURCE run's root — a custom --replay-dir must not move or
    # shadow the corpus (the replayed recalls must re-query the same one)
    replay_spec = spec
    if live:
        # Codex r1 P2-12: bound the replay to the RECORDED horizon. A
        # live dispatch stops at --turns (often well under the config's
        # max_turns); an unbounded replay would run the config's full
        # length on synthetic end_turns and falsely diverge.
        turns = [rec.get("turn") for rec in records
                 if rec["kind"] == "TOOL_CALL" and rec.get("turn")]
        if turns:
            import dataclasses

            horizon = max(turns) - _live_turn_offset(records)
            if 0 < horizon < spec.max_turns:
                replay_spec = dataclasses.replace(spec, max_turns=horizon)
    arena = Arena(replay_dir, replay_spec, runtimes=runtimes,
                  recall_root=Path(run_dir).parent)
    summary = await arena.run()

    strip_kwargs = {"live": True} if live else {}
    live_strip = _strip(records, **strip_kwargs)
    replayed_records = arena.log.records()
    if live:
        driven = set(calls)  # pids with recorded TOOL_CALLs
        replayed_records = [
            rec for rec in replayed_records
            # undriven seats (live seat 1 = the engine's own AI, outside
            # the referee) contribute nothing comparable; VIOLATIONS are
            # always kept — a replay violation against a clean live log
            # must diverge loudly
            if rec.get("kind") == "VIOLATION"
            or rec.get("player_id") in driven]
        offset = _live_turn_offset(records)
        if offset:
            live_strip = [_shift_turns(row, -offset) for row in live_strip]
    replayed = _strip(replayed_records, **strip_kwargs)
    first_divergence: int | None = None
    divergences: list[str] = []
    for i, (a, b) in enumerate(zip(live_strip, replayed, strict=False)):
        if a != b:
            if first_divergence is None:
                first_divergence = i
            if live and len(divergences) < 20:
                divergences.append(f"#{i}: live={_row_str(a)} "
                                   f"replayed={_row_str(b)}")
            elif not live:
                break
    if first_divergence is None and len(live_strip) != len(replayed):
        first_divergence = min(len(live_strip), len(replayed))

    return {
        "summary": summary,
        "identical": first_divergence is None,
        "first_divergence": first_divergence,
        "divergences": divergences,
        "live_events": len(live_strip),
        "replayed_events": len(replayed),
        "live_final_hash": _final_hash(run_dir),
        "replayed_final_hash": summary.get("final_state_hash"),
        "live_mode": live,
    }


def _row_str(row: tuple) -> str:
    """One strip row, compact for the itemized divergence report."""
    kind = row[0]
    if kind in ("TOOL_CALL", "TOOL_RESULT"):
        return (f"{kind}[t{row[1]}p{row[2]} {row[4]} "
                f"{row[6] or ''}{('/' + row[7]) if row[7] else ''}]")
    return f"{kind}[t{row[1]}]"


def _is_live_run(run_dir: Path) -> bool:
    summary = Path(run_dir) / "summary.json"
    if not summary.exists():
        return False
    try:
        return "phase" in json.loads(summary.read_text())
    except (json.JSONDecodeError, OSError):
        return False


def _is_spectate_run(run_dir: Path) -> bool:
    summary = Path(run_dir) / "summary.json"
    if not summary.exists():
        return False
    try:
        return json.loads(summary.read_text()).get("phase") == "spectate"
    except (json.JSONDecodeError, OSError):
        return False


_SPECTATE_FORBIDDEN_KINDS = frozenset({
    "TOOL_CALL", "TOOL_RESULT", "LEASE_GRANT", "LEASE_RELEASE",
    "LEASE_EXPIRED", "VIOLATION", "UNAUTHORIZED_TOOL_CALL",
})


def _spectate_certificate(run_dir: Path) -> dict[str, Any]:
    """Structural certificate for a spectate run: there is nothing to
    re-execute (no driven tool calls), so the certificate asserts the
    envelope invariants instead — seq contiguity, MATCH_START first /
    MATCH_END last, no action kinds, and strict HUMAN_TURN_START/END
    alternation. A tampered log fails here, loudly."""
    records = _load_records(Path(run_dir) / "events.jsonl")
    problems: list[str] = []
    if not records:
        problems.append("empty event log")
    else:
        if records[0]["kind"] != "MATCH_START":
            problems.append("first record is not MATCH_START")
        if records[-1]["kind"] != "MATCH_END":
            problems.append("last record is not MATCH_END")
        seqs = [rec.get("seq") for rec in records]
        if seqs != list(range(len(records))):
            problems.append("seq is not contiguous from 0 (tampered or torn log)")
    for rec in records:
        if rec.get("kind") in _SPECTATE_FORBIDDEN_KINDS:
            problems.append(
                f"{rec.get('kind')} at seq {rec.get('seq')} — a spectator "
                "never acts")
            break
    boundaries = [rec for rec in records
                  if rec.get("kind") in ("HUMAN_TURN_START", "HUMAN_TURN_END")]
    expected = ["HUMAN_TURN_START", "HUMAN_TURN_END"] * (
        len(boundaries) // 2)
    if [rec["kind"] for rec in boundaries] != expected:
        problems.append(
            "HUMAN_TURN_START/HUMAN_TURN_END do not strictly alternate")
    summary = json.loads((Path(run_dir) / "summary.json").read_text()) \
        if (Path(run_dir) / "summary.json").exists() else {}
    if isinstance(summary.get("completed_rounds"), int):
        if summary["completed_rounds"] != len(boundaries) // 2:
            problems.append(
                f"summary completed_rounds={summary['completed_rounds']} "
                f"but the log carries {len(boundaries) // 2} human turns")
    return {
        "summary": summary,
        "identical": not problems,
        "problems": problems,
        "mode": "spectate",
        "live_events": len(records),
        "replayed_events": len(records),
        "live_final_hash": summary.get("final_state_hash"),
        "replayed_final_hash": None,
        "live_mode": True,
    }


def _live_turn_offset(records: list[dict[str, Any]]) -> int:
    """The live log's first driven turn minus one (a live dispatch that
    attached at engine turn 2 has offset 1). 0 when there are no calls."""
    turns = [rec.get("turn") for rec in records
             if rec["kind"] == "TOOL_CALL" and rec.get("turn") is not None]
    return min(turns) - 1 if turns else 0


def _shift_turns(row: tuple, delta: int) -> tuple:
    """Shift a strip row's turn element (index 1 in every comparable kind)."""
    return (row[0], row[1] + delta, *row[2:]) if len(row) > 1 else row


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
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--live", dest="live", action="store_true",
                      default=None,
                      help="force live-run mode (auto-detected by default "
                           "from the run's summary 'phase' key)")
    mode.add_argument("--force-sim", dest="live", action="store_false",
                      help="force simulator-mode comparison (full hashes)")
    opts = ap.parse_args(argv)

    from civ_arena.config import load_config

    spec = load_config(opts.config)
    replay_dir = opts.replay_dir or (opts.run_dir.parent / f"{opts.run_dir.name}-replay")
    result = await replay_run(opts.run_dir, spec, replay_dir, live=opts.live)
    if result.get("mode") == "spectate":
        if result["identical"]:
            print(f"SPECTATE run — no driven tool calls to re-execute; "
                  f"structural certificate over {result['live_events']} events: OK")
            return 0
        print(f"SPECTATE run — structural certificate FAILED over "
              f"{result['live_events']} events:")
        for problem in result["problems"]:
            print("  ", problem)
        return 4
    if result["identical"]:
        print(f"REPLAY OK: {result['live_events']} comparable events identical; "
              f"final hash {result['replayed_final_hash']}"
              + (" [live mode]" if result.get("live_mode") else ""))
        return 0
    print(f"REPLAY DIVERGED at comparable-event {result['first_divergence']} "
          f"(live {result['live_events']} vs replayed {result['replayed_events']})")
    if result.get("live_mode"):
        print("live mode: the sim cannot re-derive the Civ VI board — "
              "each divergence below is an itemized sim-legality boundary, "
              "not tamper evidence by itself (a tampered log diverges in "
              "args_digest/skeleton, which these rows show verbatim):")
        for line in result["divergences"]:
            print("  ", line)
    return 4


def main() -> None:
    raise SystemExit(asyncio.run(_main_async()))


if __name__ == "__main__":
    main()
