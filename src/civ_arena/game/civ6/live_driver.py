"""Live-leg driver — orchestrates Referee + EventLog over FireTunerAdapter.

``Arena`` is deliberately sim-coupled (D1: hardcoded adapter, sim rng,
checkpoints), so the live leg drives the same referee machinery directly
through this driver — with zero changes in ``arena/``, ``session/``, or
``agents/``. Phases follow docs/live-validation.md §3, stop-at-first-anomaly.

    python -m civ_arena.game.civ6.live_driver configs/live-duel.yaml --phase probe
    python -m civ_arena.game.civ6.live_driver configs/live-duel.yaml \
        --phase exclusive-control --turns 1

``--fake`` rehearses every phase against an in-process FakeTunerServer +
FakeMod (the same entrypoint the live run uses; ``Simulate.*`` engine events
fire only through the rehearsal hook). ``--live`` (the default) attaches to
the real tuner on :4318 as its ONLY client.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from civ_arena.arena.diary import DiaryStore
from civ_arena.arena.events import EventLog
from civ_arena.arena.referee import Referee, RefereeConfig
from civ_arena.arena.telemetry import TelemetryRegistry
from civ_arena.arena.visibility import VisibilityPolicy
from civ_arena.config import MatchSpec, load_config
from civ_arena.game.civ6.fake_tuner_server import FakeMod, FakeTunerServer
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from civ_arena.game.civ6.vendor.connection import GameConnection
from civ_arena.session.tools import SessionCtx
from civ_arena.strategy.store import StrategyStore


class TapConnection(GameConnection):
    """Every command + response appended to wire.jsonl — diagnostic
    transcript, explicitly NOT a trust root (the event log is)."""

    def __init__(self, host: str, port: int, tap: Path) -> None:
        super().__init__(host, port)
        self._tap = tap
        self._fh = None

    async def _locked_execute(
        self, state_index: int, lua_code: str, timeout: float
    ) -> list[str]:
        t0 = time.monotonic()
        lines = await super()._locked_execute(state_index, lua_code, timeout)
        if self._fh is None:
            self._fh = self._tap.open("a", encoding="utf-8")
        self._fh.write(json.dumps({
            "state": state_index, "lua": lua_code,
            "ms": round((time.monotonic() - t0) * 1000, 1), "lines": lines,
        }) + "\n")
        self._fh.flush()
        return lines

    async def disconnect(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        await super().disconnect()


def _fake_hook(event: str, player_id: int) -> str:
    """Rehearsal-only engine events (FakeMod Simulate.*)."""
    return {
        "turn_start": f"Simulate.TurnStart({player_id})",
        "turn_deactivated": f"Simulate.TurnDeactivated({player_id})",
        "advance_turn": "Simulate.AdvanceTurn()",
    }[event]


class LiveDriver:
    def __init__(
        self,
        spec: MatchSpec,
        adapter: FireTunerAdapter,
        run_dir: Path,
        game_instance_id: str,
    ) -> None:
        self.spec = spec
        self.adapter = adapter
        self.run_dir = run_dir
        self.game_instance_id = game_instance_id
        if spec.watchdog_mode == "rollback":
            raise ValueError(
                "the live adapter has no rollback capability — run "
                "flag_and_continue (docs/design-notes.md: live Civ cannot "
                "roll back)")
        self.log = EventLog(run_dir / "events.jsonl")
        self.telemetry = TelemetryRegistry()
        self.referee = Referee(
            adapter, VisibilityPolicy(), self.log, self.telemetry,
            spec.match_id, game_instance_id,
            RefereeConfig(watchdog_mode=spec.watchdog_mode,
                          violation_limit=spec.violation_limit),
            diary=DiaryStore(), strategy=StrategyStore(),
        )

    def _write(self, kind: str, *, turn: int, phase_player_id: int = -1,
               player_id: int | None = None, agent_id: str | None = None,
               visibility_scope: str = "referee", **payload: Any) -> None:
        self.log.write(
            kind,
            match_id=self.spec.match_id,
            game_instance_id=self.game_instance_id,
            turn=turn, phase_player_id=phase_player_id, player_id=player_id,
            agent_id=agent_id, visibility_scope=visibility_scope, **payload,
        )

    async def match_start(self) -> None:
        self._write(
            "MATCH_START", turn=0,
            config={
                "seed": self.spec.seed, "max_turns": self.spec.max_turns,
                "agents": [(a.agent_id, a.player_id, a.policy)
                           for a in self.spec.agents],
                "watchdog_mode": self.spec.watchdog_mode,
                "adapter": self.spec.adapter,
            },
            initial_state_hash=self.adapter.state_hash(),
        )

    async def match_end(self, final_turn: int, extra: dict[str, Any]) -> None:
        summary = {
            "match_id": self.spec.match_id,
            "game_instance_id": self.game_instance_id,
            "final_turn": final_turn,
            "violations_total": self.referee.violation_count(),
            "telemetry": self.telemetry.snapshot(),
            **extra,
        }
        self._write("MATCH_END", turn=final_turn, summary=summary)
        (self.run_dir / "summary.json").write_text(
            json.dumps(summary, sort_keys=True))
        self.log.close()


async def phase_probe(spec: MatchSpec, adapter: FireTunerAdapter) -> int:
    """Phase 1 gate: connect, mod handshake, status poll. Read-only."""
    await adapter.setup({})
    caps = await adapter.require_mod()
    status = await adapter.poll_status()
    print(f"PROBE ok: mod={caps['mod_version']} freeze+ledger+digest ok, "
          f"engine turn {status.get('TURN')}, "
          f"puppet_active={status.get('PUPPET_ACTIVE')}")
    await adapter.teardown()
    return 0


async def phase_exclusive_control(
    spec: MatchSpec, adapter: FireTunerAdapter, run_dir: Path,
    turns: int, strategy: str,
) -> int:
    """Phase 2 (the make-or-break): puppet ONE player, hold an idle lease,
    issue ZERO commands. Success = 0 violations and identical digests
    bracketing the lease."""
    events = run_dir / "events.jsonl"
    if events.exists() and any(
            json.loads(line).get("kind") == "MATCH_END"
            for line in events.read_text().splitlines() if line.strip()):
        raise RuntimeError(
            f"{events} already holds a finished match — re-running would "
            "append a second MATCH_START and corrupt the record; use a "
            "fresh --run-id")
    run_dir.mkdir(parents=True, exist_ok=True)
    agent = spec.agents[0]
    driver = LiveDriver(spec, adapter, run_dir,
                        f"{spec.match_id}-i{os.getpid()}")
    await adapter.setup({})
    await adapter.require_mod()
    per_turn: list[dict[str, Any]] = []
    await driver.match_start()
    try:
        for _ in range(turns):
            status = await adapter.poll_status()  # engine turn is authority
            turn = int(status["TURN"])
            lease = driver.referee.grant_lease(
                agent.player_id, agent.agent_id, turn)
            await driver.referee.begin_turn(
                agent.player_id, agent.agent_id, turn)
            digest_open = await adapter.refresh_digest()
            # idle hold: zero commands by design — anything that moves is
            # undeclared engine drift (the open-question-1/3 check)
            ctx = SessionCtx(referee=driver.referee, player_id=agent.player_id,
                             agent_id=agent.agent_id, lease=lease, turn=turn)
            await driver.referee.end_turn(ctx)
            # CACHED close hash (refreshed inside end_phase before the turn
            # can advance): a fresh poll here would race the other player
            digest_close = adapter.state_hash()
            row = {
                "turn": turn,
                "manifest": len(driver.referee._ls.allowed),  # noqa: SLF001
                "drift": digest_open != digest_close,
                "violations": driver.referee.violation_count(),
            }
            per_turn.append(row)
            print(f"exclusive-control turn {turn}: "
                  f"manifest={row['manifest']} drift={row['drift']} "
                  f"violations={row['violations']}")
            if row["drift"] or row["violations"]:
                print("ANOMALY — stopping (record dated in §6)")
                break
        final_turn = per_turn[-1]["turn"] if per_turn else 0
        ok = bool(per_turn) and all(
            not r["drift"] and r["violations"] == 0 for r in per_turn) \
            and len(per_turn) == turns
        await driver.match_end(final_turn, {
            "phase": "exclusive-control", "strategy": strategy,
            "per_turn": per_turn, "clean": ok,
        })
        print(f"EXCLUSIVE-CONTROL {'CLEAN' if ok else 'ANOMALOUS'} "
              f"({len(per_turn)}/{turns} turns, "
              f"violations={driver.referee.violation_count()})")
        return 0 if ok else 1
    finally:
        await adapter.teardown()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("config", type=Path)
    ap.add_argument("--phase", required=True,
                    choices=["probe", "exclusive-control"])
    ap.add_argument("--turns", type=int, default=1)
    ap.add_argument("--run-id", default=None,
                    help="run dir name under runs/ (default: match_id)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4318)
    ap.add_argument("--strategy", default="h1", choices=["h1", "h2", "h3"],
                    help="D7 turn-end experiment")
    ap.add_argument("--runs-root", default="runs",
                    help="run dirs root (tests point this at a tmp dir)")
    ap.add_argument("--fake", action="store_true",
                    help="rehearse against an in-process FakeTunerServer")
    opts = ap.parse_args()
    spec = load_config(opts.config)

    async def run() -> int:
        if opts.fake:
            server = FakeTunerServer(mod=FakeMod())
            port = await server.start()
            adapter = FireTunerAdapter(
                "127.0.0.1", port,
                simulate_hook=_fake_hook, poll_timeout_s=2.0)
            try:
                return await _dispatch(spec, adapter, opts)
            finally:
                await server.stop()
        run_dir = Path(opts.runs_root) / (opts.run_id or spec.match_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        adapter = FireTunerAdapter(
            opts.host, opts.port,
            conn=TapConnection(opts.host, opts.port, run_dir / "wire.jsonl"),
            end_phase_strategy=opts.strategy)
        return await _dispatch(spec, adapter, opts)

    raise SystemExit(asyncio.run(run()))


async def _dispatch(spec: MatchSpec, adapter: FireTunerAdapter,
                    opts: argparse.Namespace) -> int:
    if opts.phase == "probe":
        return await phase_probe(spec, adapter)
    run_dir = Path(opts.runs_root) / (opts.run_id or spec.match_id)
    return await phase_exclusive_control(
        spec, adapter, run_dir, opts.turns, opts.strategy)


if __name__ == "__main__":
    main()
