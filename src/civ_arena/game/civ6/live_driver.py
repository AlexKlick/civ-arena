"""Live-leg driver for the breaking V2 turn-control contract.

Dispatch phases prepare FireTuner, then hand every policy and housekeeping
mutation to ``ArenaV2`` and ``TransactionalExecutorV2``.  Historical schema-1
live logs remain readable through the frozen V1 compatibility reader; this
module no longer exposes a schema-1 writer.

    python -m civ_arena.game.civ6.live_driver configs/live-duel.yaml --phase probe
    python -m civ_arena.game.civ6.live_driver configs/live-duel.yaml \
        --phase dispatch --turns 10   # the live 1v1: seat 0 driven, AI opp

``--fake`` rehearses every phase against an in-process FakeTunerServer +
FakeMod (the same entrypoint the live run uses; ``Simulate.*`` engine events
fire only through the rehearsal hook). ``--live`` (the default) attaches to
the real tuner on :4318 as its ONLY client.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
from collections.abc import AsyncIterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from civ_arena.config import MatchSpec, load_config
from civ_arena.game.civ6 import lua_translator
from civ_arena.game.civ6.fake_tuner_server import FakeMod, FakeTunerServer
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from civ_arena.game.civ6.vendor.connection import GameConnection
from civ_arena.v2.arena import ArenaV2
from civ_arena.v2.environment import firetuner_facets_v2
from civ_arena.v2.policy import SystemHousekeepingRuntimeV2


class TapConnection(GameConnection):
    """Every command + response appended to wire.jsonl — diagnostic
    transcript, explicitly NOT a trust root (the event log is). A transcript
    failure must never break the authority path (Codex P2-7): the tap
    degrades to closed on the first I/O error, the game response still
    reaches the adapter."""

    def __init__(self, host: str, port: int, tap: Path) -> None:
        super().__init__(host, port)
        self._tap = tap
        self._fh = None
        self._tap_broken = False

    async def _locked_execute(
        self, state_index: int, lua_code: str, timeout: float
    ) -> list[str]:
        t0 = time.monotonic()
        lines = await super()._locked_execute(state_index, lua_code, timeout)
        if not self._tap_broken:
            try:
                if self._fh is None:
                    self._fh = self._tap.open("a", encoding="utf-8")
                self._fh.write(json.dumps({
                    "state": state_index, "lua": lua_code,
                    "ms": round((time.monotonic() - t0) * 1000, 1),
                    "lines": lines,
                }) + "\n")
                self._fh.flush()
            except OSError:
                self._tap_broken = True
        return lines

    async def disconnect(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        await super().disconnect()


def _fake_hook(event: str, player_id: int, turn: int = 0) -> str:
    """Rehearsal-only engine events (FakeMod Simulate.*)."""
    return {
        "turn_start": f"Simulate.TurnStartAt({player_id}, {turn})",
        "turn_deactivated": f"Simulate.TurnDeactivated({player_id})",
        "advance_turn": "Simulate.AdvanceTurn()",
    }[event]


MOD_DEFAULT = (Path(__file__).resolve().parents[4] / "mods" / "PuppeteerMod"
               / "PuppeteerMod.lua")


async def phase_probe(
    spec: MatchSpec, adapter: FireTunerAdapter, mod_lua: str,
) -> int:
    """Phase 1 gate: connect, inject the mod, verify the capability
    handshake, poll status."""
    await adapter.setup({})
    caps = await adapter.inject_mod(mod_lua)
    status = await adapter.poll_status()
    print(f"PROBE ok: injected mod={caps['mod_version']} "
          f"freeze+ledger+digest ok, engine turn {status.get('TURN')}, "
          f"puppet_active={status.get('PUPPET_ACTIVE')}")
    await adapter.teardown()
    return 0


def _refuse_rerun(events: Path) -> None:
    if events.exists() and events.stat().st_size > 0:
        # ANY prior content refuses the rerun (Codex P1-5): a partial log
        # from a timed-out attempt must never gain a second MATCH_START —
        # replay and projection would consume a mixed pair of attempts
        raise RuntimeError(
            f"{events} already exists — appending would corrupt the trust "
            "root (partial or finished); use a fresh --run-id")


def _last_deact_turn(trace: list[str]) -> int | None:
    """The game turn of the LAST PlayerTurnDeactivated in the hook trace
    (mod v0.3.1 ring; lines are '<turn>|HOOK_DEACT|<pid>'). None when the
    mod is older or the ring is empty."""


    last: int | None = None
    for line in trace:
        parts = line.split("|")
        if len(parts) >= 3 and parts[1] == "HOOK_DEACT":
            try:
                last = int(parts[0])
            except ValueError:
                continue
    return last


def _target_turn(status: dict[str, Any], player_id: int,
                 last_driven: int = -1, last_deact_turn: int | None = None,
                 ) -> int:
    """Which engine turn the dispatch loop should drive.

    Live-learned 2026-08-30 (runs 002/003/005/006/009), in precedence order:
    1. a lease ENGAGED for us IS the turn (after our own end-turn the
       engine parks on our next turn with the lease already engaged —
       TURN+1 would strand that frozen turn forever);
    2. no lease + our turn ACTIVE => the parked turn's hook already fired
       before SetPuppet (the attach case) — drive TURN+1. AMBIGUOUS with
       "activation in progress, hook milliseconds away" (run 006) — the
       caller settles it via _settle_engagement first;
    3. no lease + our turn NOT active + TURN == the turn we just drove =>
       we ended TURN ourselves and the AI is still playing it; our next
       hook fires at TURN+1 (run 009: targeting TURN waits for a hook that
       already fired);
    4. no lease + our turn NOT active + a NEW TURN => mid-transition INTO
       our next turn; the hook is imminent at TURN — drive TURN.
    Rule 4's fresh-attach ambiguity (last_driven=-1, run 010): the AI
    finishing OUR turn N and the engine ENTERING our turn N both read
    (TURN=N, inactive, no lease). The hook ring discriminates: our last
    DEACT at turn == TURN => the AI is still on OUR turn (target TURN+1);
    our last DEACT at turn < TURN => the engine has advanced INTO our
    next turn (target TURN)."""
    if (status.get("PUPPET_ACTIVE") is True
            and int(status.get("LEASE_PLAYER", -1)) == player_id):
        return int(status.get("LEASE_TURN", -1))
    if status.get("TURN_ACTIVE") is True:
        return int(status["TURN"]) + 1
    if int(status.get("TURN", -1)) == last_driven:
        return int(status["TURN"]) + 1
    if last_deact_turn is not None and last_deact_turn == int(
            status.get("TURN", -1)):
        return int(status["TURN"]) + 1
    return int(status["TURN"])


async def _settle_engagement(adapter: FireTunerAdapter,
                             settle_s: float = 20.0) -> dict[str, Any]:
    """Resolve the ambiguous "no lease + our turn active" state (run 006):
    the hook may be IMMINENT (turn activation in progress — the lease
    appears within seconds) or already PAST (the attach case — no lease
    ever appears for this turn). Poll briefly; whatever the state settles
    into is the truth the targeting rules then apply."""
    deadline = time.monotonic() + settle_s
    status = await adapter.poll_status()
    while (time.monotonic() < deadline
           and status.get("PUPPET_ACTIVE") is not True):
        await asyncio.sleep(1.0)
        status = await adapter.poll_status()
    return status


def _live_environment_identity(
    *,
    game_version: str,
    ruleset_digest: str,
    mod_lua: str,
    strategy: str,
) -> dict[str, str]:
    """Bind the live receipt to explicit engine/ruleset and local code bytes."""

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/+,-]{0,159}", game_version):
        raise ValueError("--game-version must be a V2 identifier")
    if not re.fullmatch(r"[0-9a-f]{64}", ruleset_digest):
        raise ValueError("--ruleset-digest must be 64 lowercase hex characters")
    source_hash = hashlib.sha256()
    for source in (
        Path(__file__),
        Path(__file__).with_name("firetuner.py"),
        Path(__file__).with_name("lua_translator.py"),
    ):
        source_hash.update(source.read_bytes())
        source_hash.update(b"\0")
    return {
        "adapter_version": f"firetuner-v2.{strategy}.{source_hash.hexdigest()[:16]}",
        "game_version": game_version,
        "ruleset_digest": ruleset_digest,
        "mod_digest": hashlib.sha256(mod_lua.encode("utf-8")).hexdigest(),
    }


async def _run_live_v2(
    spec: MatchSpec,
    adapter: FireTunerAdapter,
    run_dir: Path,
    phases: AsyncIterable[tuple[int, int]],
    *,
    expected_phases: int,
    strategy: str,
    mod_lua: str,
    game_version: str,
    ruleset_digest: str,
    driven_players: set[int],
) -> dict[str, Any]:
    """Prepare FireTuner and run only the hash-chained V2 coordinator."""

    if spec.schema != 2 or spec.execution_mode != "dag_tx":
        raise ValueError("live dispatch requires schema 2 with execution_mode dag_tx")
    if spec.watchdog_mode == "rollback":
        raise ValueError("live FireTuner cannot satisfy rollback watchdog mode")
    _refuse_rerun(run_dir / "events.jsonl")
    run_dir.mkdir(parents=True, exist_ok=True)
    await adapter.setup({})
    try:
        await adapter.inject_mod(mod_lua)
        for player_id in sorted(driven_players):
            await adapter.read_raw(lua_translator.set_puppet(player_id, True))
        identity = _live_environment_identity(
            game_version=game_version,
            ruleset_digest=ruleset_digest,
            mod_lua=mod_lua,
            strategy=strategy,
        )
        environment, private_monitor = firetuner_facets_v2(adapter, **identity)
        driven_spec = replace(
            spec,
            agents=[agent for agent in spec.agents if agent.player_id in driven_players],
        )
        arena = ArenaV2(
            run_dir,
            driven_spec,
            environment=environment,
            private_monitor=private_monitor,
            system_runtime=SystemHousekeepingRuntimeV2(),
        )
        return await arena.run_prepared(phases, expected_phases=expected_phases)
    finally:
        await adapter.teardown()


async def phase_dispatch_v2(
    spec: MatchSpec,
    adapter: FireTunerAdapter,
    run_dir: Path,
    turns: int,
    strategy: str,
    mod_lua: str,
    *,
    game_version: str,
    ruleset_digest: str,
) -> int:
    """Drive one live seat through proposal -> DAG -> transaction only."""

    agent = spec.agents[0]

    async def phases() -> AsyncIterable[tuple[int, int]]:
        last_driven = -1
        for _ in range(turns):
            status = await adapter.poll_status()
            if (
                status.get("PUPPET_ACTIVE") is not True
                and status.get("TURN_ACTIVE") is True
            ):
                status = await _settle_engagement(adapter)
            turn = _target_turn(
                status,
                agent.player_id,
                last_driven,
                _last_deact_turn(await adapter.read_trace()),
            )
            adapter.expect_turn(turn)
            yield turn, agent.player_id
            last_driven = turn

    summary = await _run_live_v2(
        spec,
        adapter,
        run_dir,
        phases(),
        expected_phases=turns,
        strategy=strategy,
        mod_lua=mod_lua,
        game_version=game_version,
        ruleset_digest=ruleset_digest,
        driven_players={agent.player_id},
    )
    print(
        f"V2 DISPATCH {summary['termination_reason'].upper()} "
        f"({summary['phases_completed']}/{turns} phases, "
        f"violations={summary['violations_total']})"
    )
    return 0 if summary["termination_reason"] == "success" else 1


async def phase_dispatch_hotseat_v2(
    spec: MatchSpec,
    adapter: FireTunerAdapter,
    run_dir: Path,
    rounds: int,
    strategy: str,
    mod_lua: str,
    *,
    game_version: str,
    ruleset_digest: str,
    schedule_timeout_s: float,
) -> int:
    """Drive every configured hotseat seat through the V2 authority path."""

    seats = {agent.player_id for agent in spec.agents}
    expected = rounds * len(seats)

    async def phases() -> AsyncIterable[tuple[int, int]]:
        completed = 0
        deadline = time.monotonic() + schedule_timeout_s
        while completed < expected:
            status = await adapter.poll_status()
            player_id = int(status.get("LEASE_PLAYER", -1))
            if player_id not in seats:
                if completed:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("timed out waiting for the next driven hotseat lease")
                    await asyncio.sleep(1.0)
                    continue
                player_id = min(seats)
                turn = _target_turn(
                    status,
                    player_id,
                    -1,
                    _last_deact_turn(await adapter.read_trace()),
                )
            else:
                turn = int(status.get("LEASE_TURN", status.get("TURN", 0)))
            adapter.expect_turn(turn)
            yield turn, player_id
            completed += 1
            deadline = time.monotonic() + schedule_timeout_s

    summary = await _run_live_v2(
        spec,
        adapter,
        run_dir,
        phases(),
        expected_phases=expected,
        strategy=strategy,
        mod_lua=mod_lua,
        game_version=game_version,
        ruleset_digest=ruleset_digest,
        driven_players=seats,
    )
    print(
        f"V2 HOTSEAT {summary['termination_reason'].upper()} "
        f"({summary['phases_completed']}/{expected} phases, "
        f"violations={summary['violations_total']})"
    )
    return 0 if summary["termination_reason"] == "success" else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("config", type=Path)
    ap.add_argument("--phase", required=True,
                    choices=["probe", "exclusive-control", "dispatch",
                             "dispatch-hotseat"])
    ap.add_argument("--turns", type=int, default=1)
    ap.add_argument("--run-id", default=None,
                    help="run dir name under runs/ (default: match_id)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4318)
    ap.add_argument("--strategy", default="h1", choices=["h1", "h2", "h3"],
                    help="D7 turn-end experiment")
    ap.add_argument("--engage-timeout", type=int, default=120,
                    help="seconds to wait for the lease to engage (a parked "
                         "human turn ends on human time — raise this)")
    ap.add_argument("--runs-root", default="runs",
                    help="run dirs root (tests point this at a tmp dir)")
    ap.add_argument("--mod-path", type=Path, default=MOD_DEFAULT,
                    help="PuppeteerMod.lua to inject at attach (D9)")
    ap.add_argument("--fake", action="store_true",
                    help="rehearse against an in-process FakeTunerServer")
    ap.add_argument(
        "--game-version",
        default=None,
        help="verified Civ VI build identifier (required for live V2 dispatch)",
    )
    ap.add_argument(
        "--ruleset-digest",
        default=None,
        help="verified live ruleset SHA-256 (required for live V2 dispatch)",
    )
    ap.add_argument("--bootstrap-end-turn", action="store_true",
                    help="dispatch: end a parked lease-free local turn "
                         "ourselves to reach the first driven turn (the "
                         "tuner is single-client, so the standalone "
                         "bootstrap script cannot run beside the driver)")
    opts = ap.parse_args()
    spec = load_config(opts.config)
    mod_lua = opts.mod_path.read_text(encoding="utf-8")

    async def run() -> int:
        if opts.fake:
            server = FakeTunerServer(mod=FakeMod(
                hotseat=[a.player_id for a in spec.agents]
                if opts.phase == "dispatch-hotseat" else None))
            port = await server.start()
            adapter = FireTunerAdapter(
                "127.0.0.1", port,
                simulate_hook=_fake_hook, poll_timeout_s=2.0)
            try:
                return await _dispatch(spec, adapter, opts, mod_lua)
            finally:
                await server.stop()
        run_dir = Path(opts.runs_root) / (opts.run_id or spec.match_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        adapter = FireTunerAdapter(
            opts.host, opts.port,
            conn=TapConnection(opts.host, opts.port, run_dir / "wire.jsonl"),
            end_phase_strategy=opts.strategy,
            turn_wait_s=float(opts.engage_timeout))
        return await _dispatch(spec, adapter, opts, mod_lua)

    raise SystemExit(asyncio.run(run()))


async def _dispatch(spec: MatchSpec, adapter: FireTunerAdapter,
                    opts: argparse.Namespace, mod_lua: str) -> int:
    if opts.phase == "probe":
        return await phase_probe(spec, adapter, mod_lua)
    run_dir = Path(opts.runs_root) / (opts.run_id or spec.match_id)
    if opts.phase == "exclusive-control":
        raise RuntimeError(
            "exclusive-control is frozen V1 diagnostic evidence; the V2 live "
            "runtime only writes dispatch or dispatch-hotseat episodes"
        )
    if opts.bootstrap_end_turn:
        raise RuntimeError(
            "V2 dispatch refuses bootstrap mutation outside the transactional executor"
        )
    if opts.fake:
        game_version = "civ6-fake-tuner-v2"
        ruleset_digest = hashlib.sha256(b"civ6-fake-tuner-rules-v2").hexdigest()
    else:
        if opts.game_version is None or opts.ruleset_digest is None:
            raise RuntimeError(
                "live V2 dispatch requires --game-version and --ruleset-digest"
            )
        game_version = opts.game_version
        ruleset_digest = opts.ruleset_digest
    if opts.phase == "dispatch-hotseat":
        return await phase_dispatch_hotseat_v2(
            spec,
            adapter,
            run_dir,
            opts.turns,
            opts.strategy,
            mod_lua,
            game_version=game_version,
            ruleset_digest=ruleset_digest,
            schedule_timeout_s=float(opts.engage_timeout),
        )
    return await phase_dispatch_v2(
        spec,
        adapter,
        run_dir,
        opts.turns,
        opts.strategy,
        mod_lua,
        game_version=game_version,
        ruleset_digest=ruleset_digest,
    )


if __name__ == "__main__":
    main()
