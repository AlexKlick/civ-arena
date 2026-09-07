"""Live-leg driver — orchestrates Referee + EventLog over FireTunerAdapter.

``Arena`` is deliberately sim-coupled (D1: hardcoded adapter, sim rng,
checkpoints), so the live leg drives the same referee machinery directly
through this driver — with zero changes in ``arena/``, ``session/``, or
``agents/``. Phases follow docs/live-validation.md §3, stop-at-first-anomaly.

    python -m civ_arena.game.civ6.live_driver configs/live-duel.yaml --phase probe
    python -m civ_arena.game.civ6.live_driver configs/live-duel.yaml \
        --phase exclusive-control --turns 1
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
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from civ_arena.agents.runtime import AgentProfile, build_runtime, strategy_audit_event
from civ_arena.arena.diary import DiaryStore
from civ_arena.arena.events import EventLog
from civ_arena.arena.referee import MatchAborted, Referee, RefereeConfig
from civ_arena.arena.telemetry import TelemetryRegistry
from civ_arena.arena.visibility import VisibilityPolicy
from civ_arena.config import AgentSpec, MatchSpec, load_config
from civ_arena.game.adapter import ActionCommand, ObserveKind, ObserveRequest
from civ_arena.game.civ6 import lua_translator, response_parser, ui_control
from civ_arena.game.civ6.fake_tuner_server import FakeMod, FakeTunerServer
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from civ_arena.game.civ6.spectate_capture import (
    SpectateLimits,
    SpectatorCensus,
    TurnWatch,
)
from civ_arena.game.civ6.spectator import PopupMonitor
from civ_arena.game.civ6.vendor.connection import GameConnection
from civ_arena.session.player_session import PlayerSession
from civ_arena.session.tools import SessionCtx
from civ_arena.strategy.store import StrategyStore


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
        self._ended = False
        self.telemetry = TelemetryRegistry()
        self.referee = Referee(
            adapter, VisibilityPolicy(), self.log, self.telemetry,
            spec.match_id, game_instance_id,
            RefereeConfig(watchdog_mode=spec.watchdog_mode,
                          violation_limit=spec.violation_limit,
                          completeness_gate=spec.completeness_gate,
                          declare_own_endpath_drift=(
                              spec.declare_own_endpath_drift)),
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

    def cached_hash(self):
        try:
            return self.adapter.state_hash()
        except RuntimeError:
            return None

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
            initial_state_hash=self.cached_hash(),
        )

    async def match_end(self, final_turn: int, extra: dict[str, Any]) -> None:
        # Arena.run envelope parity (Codex P2-6): replay reads
        # final_state_hash and _strip walks these fields — a live log must
        # carry the same keys (scores stay empty until the sim-shaped
        # observe surface lands in M14c; live matches are not corpus
        # members, so projection tolerates the empty civ table).
        if self._ended:
            raise RuntimeError("MATCH_END already recorded")
        summary = {
            "match_id": self.spec.match_id,
            "game_instance_id": self.game_instance_id,
            "final_turn": final_turn,
            "aborted": None,
            "violations_total": self.referee.violation_count(),
            "final_state_hash": self.cached_hash(),
            "telemetry": self.telemetry.snapshot(),
            "scores": {},
            **extra,
        }
        self._write("MATCH_END", turn=final_turn, summary=summary)
        self._ended = True
        (self.run_dir / "summary.json").write_text(
            json.dumps(summary, sort_keys=True))
        self.log.close()


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


async def phase_exclusive_control(
    spec: MatchSpec, adapter: FireTunerAdapter, run_dir: Path,
    turns: int, strategy: str, mod_lua: str,
) -> int:
    """Phase 2 (the make-or-break): puppet ONE player, hold an idle lease,
    issue ZERO commands. Success = 0 violations and identical digests
    bracketing the lease."""
    events = run_dir / "events.jsonl"
    _refuse_rerun(events)
    run_dir.mkdir(parents=True, exist_ok=True)
    agent = spec.agents[0]
    driver = LiveDriver(spec, adapter, run_dir,
                        f"{spec.match_id}-i{os.getpid()}")
    await adapter.setup({})
    await adapter.inject_mod(mod_lua)
    per_turn: list[dict[str, Any]] = []
    await driver.match_start()
    try:
        for _ in range(turns):
            status = await adapter.poll_status()  # engine turn is authority
            # attach-while-parked: this turn's hook already fired, so the
            # lease engages at the player's NEXT natural turn start
            turn = int(status["TURN"]) + 1
            adapter.expect_turn(turn)  # mirror leads; Status polls re-sync
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


async def phase_dispatch(
    spec: MatchSpec, adapter: FireTunerAdapter, run_dir: Path,
    turns: int, strategy: str, mod_lua: str, bootstrap: bool = False,
) -> int:
    """Phase 4 (M14d, the first live 1v1): drive ONE seat — the LOCAL
    player — through the real PlayerSession/Referee/tool surface while the
    ENGINE'S OWN AI plays the other seat naturally between our turns. The
    policy's every tool call is legality-checked, deduped, hashed, and
    watchdog-diffed exactly as in the simulator; commanded effects
    reconcile through the mod's DiffSinceLast seam (v0.3)."""
    events = run_dir / "events.jsonl"
    _refuse_rerun(events)
    run_dir.mkdir(parents=True, exist_ok=True)
    agent = spec.agents[0]
    profile = _agent_profile(agent)
    driver = LiveDriver(spec, adapter, run_dir,
                        f"{spec.match_id}-i{os.getpid()}")
    runtime = build_runtime(profile, match_id=spec.match_id,
                            audit=lambda payload: _strategic_audit(driver, payload),
                            opening_units_frozen=adapter._simulate is None)
    session = PlayerSession(driver.referee, agent.player_id, agent.agent_id)
    # Arena-owned services reach the runtime exactly as the coordinator
    # wires them (LLM runtimes read diary/strategy at turn start; without
    # this an llm-policy seat would run with empty cross-turn memory).
    # A planner seat also gets its belief journal (M17a) — the same side
    # artifact the Arena wires, so live fog memory survives a driver
    # relaunch the same way it survives a resume.
    bind = getattr(runtime, "bind_services", None)
    if bind is not None:
        from civ_arena.planner.journal import PlannerJournal

        journal = (PlannerJournal(run_dir / "planner"
                                  / f"p{agent.player_id}-journal.jsonl")
                   if agent.policy == "planner" else None)
        if journal is not None:
            bind(diary=driver.referee.diary, strategy=driver.referee.strategy,
                 journal=journal)
        else:
            bind(diary=driver.referee.diary, strategy=driver.referee.strategy)
    await adapter.setup({})
    await adapter.inject_mod(mod_lua)
    # ARM THE PUPPET AT ATTACH (live-learned glm-g1): a turn whose
    # PlayerTurnStartComplete fires while the puppet is off is SKIPPED
    # entirely (HOOK_SKIP|not-puppet) — the engine plays that turn as an
    # idle human seat and the driver can never engage it. Arming here
    # means EVERY hook from now on engages a lease; targeting then only
    # decides which engaged turn to drive.
    await adapter.read_raw(lua_translator.set_puppet(agent.player_id, True))
    per_turn: list[dict[str, Any]] = []
    await driver.match_start()
    last_driven = -1
    try:
        for _ in range(turns):
            status = await adapter.poll_status()  # engine turn is authority
            if (status.get("PUPPET_ACTIVE") is not True
                    and status.get("TURN_ACTIVE") is True):
                status = await _settle_engagement(adapter)
            attach_case = (status.get("PUPPET_ACTIVE") is not True
                           and status.get("TURN_ACTIVE") is True)
            turn = _target_turn(
                status, agent.player_id, last_driven,
                _last_deact_turn(await adapter.read_trace()))
            # the bootstrap fires ONLY on the attach case (our turn ACTIVE,
            # hook past, settle found no lease) — NOT on the rule-5 state
            # (AI still on our just-ended turn): issuing an end-turn there
            # is a no-op on an already-ended turn (run 011's misfire)
            if (bootstrap and attach_case
                    and turn == int(status["TURN"]) + 1
                    and status.get("PUPPET_ACTIVE") is not True):
                # --bootstrap-end-turn: the attach case (parked local turn,
                # its hook already fired, no lease) — end it OURSELVES, the
                # same single command the operator clicked in M14b (the
                # tuner is single-client: an out-of-process bootstrap
                # cannot run beside the driver). Guarded: only when NO
                # lease is engaged, and only to reach the turn we target.
                print(f"bootstrap: ending parked turn {status['TURN']} "
                      "(no lease, hook past)")
                await adapter.write_raw(
                    lua_translator.request_end_turn(agent.player_id))
                # break ONLY on engagement: the turn NUMBER advances before
                # the hook fires, and re-targeting from that instant
                # overshoots to N+1 while the lease then engages at N
                # (live-learned glm-g1's second race). The end-turn existed
                # to GET the next lease — wait for exactly that.
                deadline = time.monotonic() + 60.0
                while time.monotonic() < deadline:
                    status = await adapter.poll_status()
                    if status.get("PUPPET_ACTIVE") is True:
                        break
                    await asyncio.sleep(2.0)
                turn = _target_turn(status, agent.player_id, last_driven)
            adapter.expect_turn(turn)
            lease = driver.referee.grant_lease(
                agent.player_id, agent.agent_id, turn)
            try:
                await driver.referee.begin_turn(
                    agent.player_id, agent.agent_id, turn)
            except RuntimeError as e:
                if "lease to engage" not in str(e):
                    raise
                # A stalled lease engage has two live shapes: the attach
                # case (our parked human turn was never ended — recover by
                # ending it) and a front-end modal (advisor tips freeze the
                # between-turn processing — recover by dismissal). Retry
                # ONCE on the SAME lease (the phase never opened); the
                # failed wait's polls reset the turn mirror, so the
                # expectation is re-armed first.
                await _recover_stall(adapter, agent.player_id, turn)
                adapter.expect_turn(turn)
                await driver.referee.begin_turn(
                    agent.player_id, agent.agent_id, turn)
            digest_open = await adapter.refresh_digest()
            await _resolve_blockers(
                adapter, agent.player_id, turn,
                defer_economy=agent.decision_mode == "strategic_autopilot")
            # Housekeeping mutations are DRIVER-commanded, not
            # agent-commanded — they must not read as uncommanded drift in
            # the session's watchdog window. The civic/policy resolutions
            # dodge this only because their attrs sit outside the
            # recorder's coverage; research IS covered (player snapshot
            # parity), so acknowledge explicitly. The wire transcript
            # remains the record — same trust class as the resolutions.
            housekept = adapter.drain_mutations()
            driver.referee._ls.acknowledged.extend(housekept)  # noqa: SLF001
            allowed_open = len(driver.referee._ls.allowed)  # noqa: SLF001
            # the coordinator's turn-start hook (LLM runtimes REQUIRE it —
            # the authoritative turn number for their prompt; the turtler's
            # ScriptedRuntime simply lacks it)
            begin_hook = getattr(runtime, "begin_turn", None)
            if begin_hook is not None:
                begin_hook(turn)
            # THE TURN: the policy acts through the bound ToolFacade —
            # every call flows observe/execute/end_turn through the referee
            await session.take_turn(lease, runtime)
            # CACHED close hash: end_phase sealed it before the end-turn
            # command, so the engine's post-processing cannot race it
            digest_close = adapter.state_hash()
            allowed = driver.referee._ls.allowed[allowed_open:]  # noqa: SLF001
            row = {
                "turn": turn,
                "allowed_mutations": len(allowed),
                "digest_changed": digest_open != digest_close,
                "mutated": bool(allowed),
                "violations": driver.referee.violation_count(),
            }
            # integrity (Codex P2-1, one-directional): the digest moving
            # with ZERO authorized mutations is undeclared drift. The
            # inverse is LEGAL — receipts with an unchanged net hash (a
            # move there-and-back) — so it must not flag. Driver-commanded
            # housekeeping mutations (acknowledged, not allowed) explain
            # their own digest movement — research set at lease start
            # moved the digest with zero AGENT mutations (game seven,
            # turn 11 — a legal stop turned into a false anomaly).
            row["unexpected"] = (row["digest_changed"]
                                 and not (row["mutated"] or housekept))
            per_turn.append(row)
            last_driven = turn
            print(f"dispatch turn {turn}: allowed={row['allowed_mutations']} "
                  f"digest_changed={row['digest_changed']} "
                  f"violations={row['violations']}")
            if row["unexpected"] or row["violations"]:
                print("ANOMALY — stopping (record dated in §6)")
                break
        final_turn = per_turn[-1]["turn"] if per_turn else 0
        ok = bool(per_turn) and all(
            not r["unexpected"] and r["violations"] == 0 for r in per_turn) \
            and len(per_turn) == turns
        await driver.match_end(final_turn, {
            "phase": "dispatch", "strategy": strategy,
            "per_turn": per_turn, "clean": ok,
        })
        print(f"DISPATCH {'CLEAN' if ok else 'ANOMALOUS'} "
              f"({len(per_turn)}/{turns} turns, "
              f"violations={driver.referee.violation_count()})")
        return 0 if ok else 1
    except MatchAborted as exc:
        # A2: an LLM auth-death mid-match must still leave a clean,
        # replay-consumable record (no summary at all was the old shape);
        # mirrors Arena.run's abort path through the referee.
        await driver.referee.abort_cleanup(agent.agent_id)
        await driver.match_end(final_turn := (per_turn[-1]["turn"]
                                              if per_turn else 0), {
            "phase": "dispatch", "strategy": strategy,
            "per_turn": per_turn, "clean": False, "aborted": str(exc),
        })
        print(f"DISPATCH ABORTED: {exc}")
        return 2
    finally:
        # Codex P1-7: a crashed run must not leave the engine parked under
        # a frozen lease — release this turn's lease (turn-bound, so a
        # freshly-engaged next lease survives) and drop the puppet
        try:
            if adapter._phase_open != -1:  # noqa: SLF001
                await adapter.read_raw(lua_translator.release(
                    adapter._phase_open, adapter._turn_mirror))  # noqa: SLF001
        except Exception:
            pass
        close = getattr(runtime, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()
        await adapter.teardown()


# the turtler doctrine's own build preference (agents/scripted.py)
_BUILD_PREFERENCE = ["MONUMENT", "WALLS", "WARRIOR", "GRANARY", "SETTLER",
                     "SCOUT", "SLINGER", "BARRACKS"]


def _strategic_audit(driver: LiveDriver, payload: dict) -> None:
    doc = strategy_audit_event(payload)
    doc.pop("match_id")  # LiveDriver binds its own match identity.
    driver._write("HEARTBEAT", **doc)


def _agent_profile(agent: AgentSpec) -> AgentProfile:
    """The ONE AgentSpec -> AgentProfile mapping for every live dispatch
    path (M14d single-seat and M18 hotseat). Both paths MUST go through
    this — a second hand-rolled constructor is exactly how a config field
    (the M19b case_base) silently stops reaching the live runtime. The
    case base itself loads inside build_runtime: LOUD at construction
    (missing/corrupt artifact refuses pre-spend), never mid-match."""
    return AgentProfile(
        agent_id=agent.agent_id, player_id=agent.player_id,
        policy=agent.policy, seed=agent.seed, model=agent.model,
        llm=agent.llm, proposer=agent.proposer,
        case_base=agent.case_base, decision_mode=agent.decision_mode,
        growth_autopilot=agent.growth_autopilot)


@dataclass(frozen=True)
class HotseatLimits:
    startup: float = 2700
    match: float = 7200
    agent_turn: float = 600
    recovery: float = 180
    sweeps: int = 8
    cleanup: float = 20

    def __post_init__(self):
        if any(v <= 0 for v in asdict(self).values()):
            raise ValueError("hotseat limits must be positive")


class RecoveryEpisode:
    """One budget spans release and engagement; only engagement resets it."""
    def __init__(self, limits: HotseatLimits, controller, audit, run_dir: Path,
                 popup_check=None):
        self.limits, self.controller, self.audit = limits, controller, audit
        self.run_dir = run_dir
        self.started = None
        self.attempts = 0
        self.total_attempts = 0
        self.next_sweep = 0.0
        self.popup_check = popup_check

    def start(self):
        if self.started is None:
            self.started = time.monotonic()
            self.next_sweep = self.started + 15

    def remaining(self):
        self.start()
        remaining = self.limits.recovery - (time.monotonic() - self.started)
        if remaining <= 0:
            raise TimeoutError("recovery episode deadline expired")
        return remaining

    def engaged(self):
        self.audit("engaged", attempts=self.attempts,
                   elapsed_s=0 if self.started is None else time.monotonic() - self.started)
        self.started = None
        self.attempts = 0

    async def sweep(self, probe, *, keys=("Return", "Escape", "Escape")):
        self.remaining()
        if self.attempts >= self.limits.sweeps:
            raise RuntimeError("recovery exhausted: sweep limit")
        self.attempts += 1
        self.total_attempts += 1
        self.audit("recovery_sweep", attempt=self.attempts)
        async with asyncio.timeout(self.remaining()):
            if await probe():
                return True
            if self.popup_check is not None and await self.popup_check():
                # The semantic handler belongs to this sweep's same budget.
                # Do not also send blind keys into a newly advanced UI queue.
                progress = await probe()
                self.next_sweep = time.monotonic() + 5
                return progress
            for index, key in enumerate((*keys, None)):
                if await probe():
                    return True
                result = await self.controller.action(
                    key=key, banner=key is None, timeout=min(15, self.remaining()),
                    evidence=self.run_dir / "recovery" /
                    f"sweep-{self.total_attempts}-action-{index}.png")
                self.audit("recovery_input", attempt=self.attempts, key=key,
                           outcome=asdict(result))
                # A sent helper is not a successful recovery. Re-poll after
                # EVERY action and stop inputs at the first observed progress.
                if await probe():
                    return True
                if result.status == "failed":
                    raise RuntimeError(f"recovery helper failed: {result.diagnostic}")
        self.next_sweep = time.monotonic() + 5
        return False


class CompletedTurns:
    def __init__(self, order):
        self.order = sorted(order)
        if len(self.order) != 2 or len(set(self.order)) != 2:
            raise ValueError("hotseat requires two distinct seats")
        self.rows = []
        self.first_turn = None

    def expected(self):
        index = len(self.rows)
        return (None if self.first_turn is None else self.first_turn + index // 2,
                self.order[index % 2])

    def check(self, turn, player):
        expected_turn, expected_player = self.expected()
        if player != expected_player or (expected_turn is not None and turn != expected_turn):
            raise RuntimeError(f"out-of-order seat turn {(turn, player)}; "
                               f"expected {(expected_turn, expected_player)}")

    def append(self, row, lease):
        self.check(row["turn"], row["player"])
        if not lease.released:
            raise RuntimeError("agent returned with an open lease")
        if self.first_turn is None:
            self.first_turn = row["turn"]
        self.rows.append(row)

    @property
    def rounds(self):
        return len(self.rows) // 2


def implementation_identity(spec, mod_lua):
    repo = Path(__file__).resolve().parents[4]
    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True,
                              capture_output=True, text=True, timeout=5).stdout.strip()
    return {"commit": git("rev-parse", "HEAD"),
            "tree": git("rev-parse", "HEAD^{tree}"),
            "dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
            "config": asdict(spec),
            "mod_sha256": hashlib.sha256(mod_lua.encode()).hexdigest()}


async def _attach_initial_hotseat_turn(adapter, player: int, audit) -> None:
    """Acquire an already-active fresh turn once; never skip the first seat.

    Fake games acquire leases through their simulated engine hooks. Live
    attachment requires the mod's guarded operation and an observed lease.
    """
    if adapter._simulate is not None:
        return
    async with asyncio.timeout(12):
        before = await adapter.poll_status()
        if before.get("PUPPET_ACTIVE") is True or before.get("TURN_ACTIVE") is not True:
            return
        turn = before.get("TURN")
        if type(turn) is not int or turn != 1:
            raise RuntimeError("initial hotseat attachment requires fresh engine turn 1")
        conn = adapter._conn
        async with conn._lock:
            state = conn.gamecore_index
            if (not conn.is_connected or state is None
                    or conn.lua_states.get(state) != "GameCore_Tuner"):
                raise RuntimeError("initial hotseat attachment has no GameCore connection")
            # Bypass reconnect/retry: a lost response must not repeat mutation.
            lines = await conn._locked_execute(
                state, lua_translator.attach_current_turn(player, turn), 8.0)
        receipts = [line.strip() for block in lines for line in block.splitlines()
                    if line.strip().startswith("ATTACH_CURRENT|")]
        audit("initial_turn_attach", player=player, expected_turn=turn,
              receipts=[ui_control.redact(row) for row in receipts])
        if len(receipts) != 1:
            raise RuntimeError("initial hotseat attachment receipt missing or duplicated")
        parts = receipts[0].split("|")
        if (len(parts) != 5 or parts[1] not in ("accepted", "duplicate")
                or parts[2:4] != [str(player), str(turn)]):
            raise RuntimeError(
                f"initial hotseat attachment refused: {ui_control.redact(receipts[0])}")
        after = await adapter.poll_status()
        audit("initial_turn_attach_observed", status=after)
        if (after.get("PUPPET_ACTIVE") is not True
                or after.get("LEASE_PLAYER") != player
                or after.get("LEASE_TURN") != turn
                or after.get("TURN") != turn):
            raise RuntimeError("initial hotseat attachment did not establish the expected lease")


async def phase_dispatch_hotseat(
    spec: MatchSpec, adapter: FireTunerAdapter, run_dir: Path,
    rounds: int, strategy: str, mod_lua: str, *,
    limits: HotseatLimits | None = None, controller=None, pace_llm_turns: bool = True,
) -> int:
    """Drive and account for two ordered, completed seats per engine turn."""
    _refuse_rerun(run_dir / "events.jsonl")
    run_dir.mkdir(parents=True, exist_ok=True)
    driver = LiveDriver(spec, adapter, run_dir, f"{spec.match_id}-i{os.getpid()}")
    limits = limits or HotseatLimits()
    controller = controller or (ui_control.FakeController() if adapter._simulate is not None
                                else ui_control.Controller())
    seats = {}
    ledger = CompletedTurns([a.player_id for a in spec.agents])
    lease = None
    status = None
    failure = None
    stage = "startup"
    began = time.monotonic()
    identity = None
    cleanup = {"status": "unavailable"}

    def audit(event, **payload):
        driver._write("HEARTBEAT", turn=int((status or {}).get("TURN", 0)),
                      audit=event, **payload)

    popups = PopupMonitor(adapter, controller, audit, timeout=limits.recovery,
                         attempts=limits.sweeps)
    recovery = RecoveryEpisode(limits, popups, audit, run_dir, popup_check=popups.check)

    async def poll():
        nonlocal status
        status = await adapter.poll_status()
        audit("engine_status", status=status)
        return status

    def start_handoff():
        nonlocal stage
        stage = "recovery"
        recovery.start()
        return recovery.remaining()

    async def wait_release(player, turn):
        nonlocal stage
        stage = "recovery"
        recovery.start()
        async def released():
            p = await poll()
            return (p.get("PUPPET_ACTIVE") is False
                    or (p.get("PUPPET_ACTIVE") is True and
                        (p.get("LEASE_PLAYER"), p.get("LEASE_TURN")) != (player, turn)))
        async with asyncio.timeout(recovery.remaining()):
            await popups.quiesce()
            while not await released():
                if (time.monotonic() >= recovery.next_sweep
                        and await recovery.sweep(released, keys=("Return", "Escape", "Escape"))):
                    return
                await asyncio.sleep(min(1, recovery.remaining()))

    async def engage():
        nonlocal status, stage
        stage = "recovery"
        recovery.start()
        async def probe():
            await poll()
            pid = status.get("LEASE_PLAYER", -1)
            if status.get("PUPPET_ACTIVE") is True and pid in seats:
                turn = int(status["LEASE_TURN"])
                # The last seat's pending release is a stall, never another
                # completed seat. Any other wrong identity is an anomaly.
                if ledger.rows and (turn, pid) == (ledger.rows[-1]["turn"],
                                                   ledger.rows[-1]["player"]):
                    return None
                ledger.check(turn, pid)
            elif not ledger.rows:
                pid = ledger.order[0]
                turn = _target_turn(status, pid, -1,
                                    _last_deact_turn(await adapter.read_trace()))
            else:
                return None
            agent = seats[pid]["agent"]
            adapter.expect_turn(turn)
            previous_wait = adapter._turn_wait_s
            adapter._turn_wait_s = min(5, recovery.remaining())
            try:
                await driver.referee.begin_turn(agent.player_id, agent.agent_id, turn)
            except RuntimeError as exc:
                if not any(x in str(exc) for x in ("cannot begin turn", "lease to engage")):
                    raise
                audit("engagement_wait", reason=ui_control.redact(str(exc)))
                return None
            finally:
                adapter._turn_wait_s = previous_wait
            return turn, pid

        async with asyncio.timeout(recovery.remaining()):
            await popups.quiesce()
            while True:
                result = await probe()
                if result:
                    recovery.engaged()
                    return result
                if time.monotonic() >= recovery.next_sweep:
                    found = None
                    async def engaged():
                        nonlocal found
                        found = await probe()
                        return found is not None
                    if await recovery.sweep(engaged):
                        recovery.engaged()
                        return found
                await asyncio.sleep(min(1, recovery.remaining()))

    async def cleanup_all():
        errors = []
        # Preserve the game and its lease for diagnosis; cleanup sends no
        # new game actions. Disconnect first, then close provider resources.
        try:
            await adapter.teardown()
        except Exception as exc:
            errors.append(ui_control.redact(f"disconnect: {exc}"))
        for seat in seats.values():
            close = getattr(seat["runtime"], "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception as exc:
                    errors.append(ui_control.redact(f"provider close: {exc}"))
        return {"status": "failed" if errors else "completed", "errors": errors}

    try:
        # MATCH_START exists even when construction, provider auth, or setup fails.
        await driver.match_start()
        identity = implementation_identity(spec, mod_lua)
        audit("run_identity", identity=identity, limits=asdict(limits),
              fake=adapter._simulate is not None,
              llm_turn_pacing="visible_briefing_v1" if pace_llm_turns else "standard",
              movement_allowance=spec.declare_own_endpath_drift)
        if rounds <= 0:
            raise ValueError("rounds must be positive")
        async with asyncio.timeout(limits.startup):
            for agent in spec.agents:
                runtime = build_runtime(
                    _agent_profile(agent), match_id=spec.match_id,
                    audit=lambda payload: _strategic_audit(driver, payload),
                    opening_units_frozen=adapter._simulate is None)
                audit("decision_mode", agent=agent.agent_id, mode=agent.decision_mode,
                      opening_units_frozen=adapter._simulate is None)
                seats[agent.player_id] = {
                    "agent": agent, "runtime": runtime,
                    "session": PlayerSession(driver.referee, agent.player_id, agent.agent_id),
                }
                bind = getattr(runtime, "bind_services", None)
                if bind is not None:
                    kwargs = dict(diary=driver.referee.diary, strategy=driver.referee.strategy)
                    if agent.policy == "planner":
                        from civ_arena.planner.journal import PlannerJournal
                        kwargs["journal"] = PlannerJournal(
                            run_dir / "planner" / f"p{agent.player_id}-journal.jsonl")
                    bind(**kwargs)
                if hasattr(runtime, "telemetry"):
                    runtime.telemetry = driver.telemetry
                configure_pacing = getattr(runtime, "configure_turn_pacing", None)
                if pace_llm_turns and agent.policy == "llm" and configure_pacing is not None:
                    recall_available = driver.referee.recall is not None
                    configure_pacing(recall_available=recall_available)
                    audit("turn_pacing", agent=agent.agent_id, mode="visible_briefing_v1",
                          recall_available=recall_available)
                client = getattr(runtime, "client", None)
                if client is not None and hasattr(client, "on_post"):
                    client.on_post = lambda client=client, aid=agent.agent_id: audit(
                        "provider_request", agent=aid, posts_sent=client.posts_sent)
            await adapter.setup({})
            caps = await adapter.inject_mod(mod_lua)
            audit("mod_capabilities", capabilities=caps)
            for pid in seats:
                await adapter.read_raw(lua_translator.set_puppet(pid, True))
            await _attach_initial_hotseat_turn(adapter, ledger.order[0], audit)
            await adapter.refresh_digest()
        adapter.handoff_wait = wait_release
        adapter.handoff_start = start_handoff
        adapter.human_seats = tuple(ledger.order)
        adapter.human_handoff_audit = lambda receipt: audit("human_handoff", receipt=receipt)
        play_started = time.monotonic()
        async with asyncio.timeout(limits.match), asyncio.TaskGroup() as tasks:
            watcher = tasks.create_task(popups.watch(lambda: stage == "active_turn"))
            try:
                while ledger.rounds < rounds:
                    turn, seat_pid = await engage()
                    stage = "active_turn"
                    turn_started = time.monotonic()
                    seat = seats[seat_pid]
                    agent = seat["agent"]
                    lease = driver.referee.grant_lease(agent.player_id, agent.agent_id, turn)
                    async with asyncio.timeout(limits.agent_turn):
                        await adapter.activate_human_seat(agent.player_id, turn)
                        await adapter.read_raw(lua_translator.unpause_local())
                        digest_open = await adapter.refresh_digest()
                        await _resolve_blockers(
                            adapter, agent.player_id, turn,
                            defer_economy=agent.decision_mode == "strategic_autopilot")
                        housekept = adapter.drain_mutations()
                        driver.referee._ls.acknowledged.extend(housekept)
                        allowed_open = len(driver.referee._ls.allowed)
                        begin_hook = getattr(seat["runtime"], "begin_turn", None)
                        if begin_hook is not None:
                            begin_hook(turn)
                        nxt = ledger.order[(ledger.order.index(seat_pid) + 1) % 2]
                        adapter.set_pre_end_switch(nxt)
                        try:
                            await seat["session"].take_turn(lease, seat["runtime"])
                        finally:
                            adapter.set_pre_end_switch(None)
                        if not lease.released or adapter._phase_open != -1:
                            raise RuntimeError("agent returned with an open lease")
                        # Adapter.end_phase observed engine release before the
                        # referee released the logical lease. Both are required.
                        digest_close = adapter.state_hash()
                        allowed = driver.referee._ls.allowed[allowed_open:]
                        row = {"turn": turn, "player": agent.player_id,
                               "agent": agent.agent_id, "allowed_mutations": len(allowed),
                               "digest_changed": digest_open != digest_close,
                               "mutated": bool(allowed), "lease_released": lease.released,
                               "violations": driver.referee.violation_count(),
                               "elapsed_s": time.monotonic() - turn_started}
                        row["unexpected"] = bool(
                            row["digest_changed"] and not (allowed or housekept))
                        ledger.append(row, lease)
                        audit("completed_seat_turn", row=row)
                        print(f"hotseat turn {turn} p{seat_pid}: "
                              f"{len(ledger.rows)} completed", flush=True)
                        if row["unexpected"] or row["violations"]:
                            raise RuntimeError("watchdog or unexplained digest anomaly")
                stage = "finishing"
                await popups.quiesce()
                audit("play_complete", elapsed_s=time.monotonic() - play_started)
            finally:
                watcher.cancel()
    except (Exception, asyncio.CancelledError, KeyboardInterrupt) as exc:
        detail = str(exc)
        if isinstance(exc, BaseExceptionGroup):
            detail = "; ".join(f"{type(child).__name__}: {child}" for child in exc.exceptions)
        failure = ui_control.redact(f"{type(exc).__name__} during {stage}: {detail}")
    finally:
        adapter.handoff_wait = None
        try:
            async with asyncio.timeout(limits.cleanup):
                cleanup = await cleanup_all()
        except (Exception, asyncio.CancelledError) as exc:
            cleanup = {"status": "failed", "error": ui_control.redact(type(exc).__name__)}
        if cleanup["status"] != "completed" and failure is None:
            failure = "cleanup failed"
        ok = failure is None and ledger.rounds == rounds and len(ledger.rows) == 2 * rounds
        # Use cached observations: cleanup never queries a changing game.
        summary = {
            "phase": "dispatch-hotseat", "strategy": strategy,
            "per_turn": ledger.rows, "completed_rounds": ledger.rounds,
            "requested_rounds": rounds, "clean": ok, "aborted": failure,
            "failure_reason": failure, "failure_stage": stage if failure else None,
            "last_completed_turn": ledger.rows[-1] if ledger.rows else None,
            "active_lease": asdict(lease) if lease and not lease.released else None,
            "last_engine_status": status, "final_observation": "cached; not re-polled at shutdown",
            "recovery_attempts": recovery.total_attempts,
            "pending_recovery_attempts": recovery.attempts, "cleanup": cleanup,
            "informational_popups": popups.summary(),
            "limits": asdict(limits), "elapsed_s": time.monotonic() - began,
            "identity": identity, "movement_allowance": spec.declare_own_endpath_drift,
            "turn_pacing": {s["agent"].agent_id: {
                "enabled": bool(getattr(s["runtime"], "paced_turns", False)),
                "recall_available": getattr(s["runtime"], "recall_available", None),
            } for s in seats.values()},
            "request_usage": {s["agent"].agent_id: getattr(
                getattr(s["runtime"], "client", None), "posts_sent", None)
                for s in seats.values()},
        }
        await driver.match_end(ledger.rows[-1]["turn"] if ledger.rows else 0, summary)
    print(f"HOTSEAT {'CLEAN' if ok else 'ABORTED'}: {len(ledger.rows)}/{rounds * 2} "
          f"seat turns; {failure or 'completed'}", flush=True)
    return 0 if ok else 2


async def phase_spectate(
    spec: MatchSpec, adapter: FireTunerAdapter, run_dir: Path,
    turns: int, mod_lua: str, limits: SpectateLimits,
) -> int:
    """Watch-and-record a live game the OPERATOR plays against the engine's
    own AI. The harness NEVER acts: no puppet arming (a lease would freeze
    the human's units), no local-player switch, no blocker housekeeping, no
    popup dismissal, no desktop input of any kind — polls, observes, and
    the mod's lease-free ambient-window recorder commands only. Turn
    boundaries come from the unconditional hook ring (HOOK_ENTER/HOOK_DEACT
    for the human seat), corroborated by TURN_ACTIVE. Attaching mid-human-
    turn records that (partial) turn as round 1 with window="attach". The
    turn budget is AUDIT-ONLY: an overrun is recorded, never acted on."""
    events = run_dir / "events.jsonl"
    _refuse_rerun(events)
    run_dir.mkdir(parents=True, exist_ok=True)
    if spec.spectate is None:
        raise RuntimeError(
            "--phase spectate requires a spectate: block in the config")
    if spec.agents:
        raise RuntimeError(
            "a spectate run rosters no driven agents (config refused it "
            "already; this is the driver-side belt-and-braces)")
    sc = spec.spectate
    human = sc.human_seat
    ai_players = [p for p in sc.observed_players if p != human]
    driver = LiveDriver(spec, adapter, run_dir,
                        f"{spec.match_id}-i{os.getpid()}")
    census = SpectatorCensus(adapter, sc)
    watch = TurnWatch()
    started = time.monotonic()

    def audit(tag: str, **fields: Any) -> None:
        turn = fields.pop("turn", 0)
        driver._write("HEARTBEAT", turn=turn, phase_player_id=human,  # noqa: SLF001
                      player_id=None, agent_id=None,
                      visibility_scope="spectator", audit=tag, **fields)

    per_round: list[dict[str, Any]] = []
    windows_open: set[int] = set()
    failure: str | None = None
    clean = False
    attached_mid_turn = False
    engine_turn_at_attach = -1
    ai_hook_events = 0
    overrun_flagged = False
    human_turn = -1
    snapshot: dict[str, Any] = {}
    ai_manifests: dict[str, list[dict[str, Any]]] = {}
    status_polls = trace_polls = digest_reads = observe_reads = \
        recorder_commands = 0
    # the phase has NO write path at all — pinned structurally by
    # test_live_spectate's source pin and by the wire-level command
    # allowlist over the fake server's received_commands
    game_writes = 0

    async def counted_status() -> dict[str, Any]:
        nonlocal status_polls
        status_polls += 1
        return await adapter.poll_status()

    async def counted_trace() -> list[str]:
        nonlocal trace_polls
        trace_polls += 1
        return await adapter.read_trace()

    async def open_window(pid: int) -> None:
        nonlocal recorder_commands
        recorder_commands += 1
        await census.open_window(pid)
        windows_open.add(pid)

    async def close_window(pid: int) -> list[dict[str, Any]]:
        nonlocal recorder_commands
        recorder_commands += 1
        rows = await census.close_window(pid)
        windows_open.discard(pid)
        return rows

    await adapter.setup({})
    await adapter.inject_mod(mod_lua)
    await driver.match_start()
    audit("run_identity", identity=implementation_identity(spec, mod_lua),
          fake=adapter._simulate is not None)  # noqa: SLF001
    audit("spectate_config", **asdict(sc))
    try:
        async with asyncio.timeout(limits.match_s):
            status = await counted_status()
            trace = await counted_trace()
            engine_turn_at_attach = int(status.get("TURN", -1))
            attached_mid_turn = status.get("TURN_ACTIVE") is True
            audit("engine_status", turn=engine_turn_at_attach,
                  turn_active=status.get("TURN_ACTIVE"),
                  attached_mid_turn=attached_mid_turn)
            # baseline windows from attach: every observed player (the
            # attach round's human window opens here — window="attach")
            for pid in sc.observed_players:
                await open_window(pid)

            round_no = 0
            active = False
            history_drained = False
            last_heartbeat = time.monotonic()
            turn_started = time.monotonic()
            while not clean:
                new, gap = watch.new_entries(trace)
                if gap:
                    audit("trace_gap", ring_size=len(trace))
                if not history_drained:
                    # the FIRST read sees ring HISTORY. Attached mid-human-
                    # turn: keep only the current turn's entries (the
                    # human's ENTER is real and unprocessed). Attached
                    # between/AI turns: every history entry is stale (the
                    # human's next turn has not started) — drop it all and
                    # wait for the next fresh HOOK_ENTER.
                    history_drained = True
                    if attached_mid_turn:
                        new = [e for e in new
                               if (TurnWatch.parse(e) or (-1, "", -1))[0]
                               == engine_turn_at_attach]
                    else:
                        if new:
                            audit("attach_history_discarded",
                                  entries=len(new))
                        new = []
                for entry in new:
                    parsed = TurnWatch.parse(entry)
                    if parsed is None:
                        continue
                    entry_turn, event, pid = parsed
                    if pid == human and event == "HOOK_ENTER" and not active:
                        # -- round start: close the AI windows (their
                        # deltas since they last opened), census, snapshot,
                        # ensure the HUMAN window is open, START
                        round_no += 1
                        human_turn = entry_turn
                        ai_manifests = {}
                        for ai in ai_players:
                            if ai in windows_open:
                                ai_manifests[str(ai)] = await close_window(ai)
                            await open_window(ai)
                        snapshot = await census.snapshot()
                        digest_reads += 2
                        observe_reads += 1 + (
                            2 if sc.snapshot_scope == "full" else 0)
                        driver._write(  # noqa: SLF001
                            "HUMAN_TURN_START", turn=entry_turn,
                            phase_player_id=human, player_id=None,
                            agent_id=None, visibility_scope="spectator",
                            operator=sc.operator,
                            window="attach" if attached_mid_turn
                            and round_no == 1 else "turn_start",
                            turn_active_corroborated=(
                                status.get("TURN_ACTIVE") is True))
                        driver._write(  # noqa: SLF001
                            "SPECTATOR_SNAPSHOT", turn=entry_turn,
                            phase_player_id=human, player_id=None,
                            agent_id=None, visibility_scope="spectator",
                            round=round_no, phase="turn_start",
                            ambient=ai_manifests, **snapshot)
                        if human not in windows_open:
                            await open_window(human)
                        active = True
                        overrun_flagged = False
                        turn_started = time.monotonic()
                    elif pid == human and event == "HOOK_DEACT" and active:
                        # -- round end: close the human window (their
                        # in-turn delta), digest, END, reopen AI windows
                        human_rows = await close_window(human)
                        duration = time.monotonic() - turn_started
                        digest_reads += 1
                        digest_after = await adapter.refresh_digest()
                        overrun = duration > sc.turn_budget_s
                        driver._write(  # noqa: SLF001
                            "HUMAN_TURN_END", turn=entry_turn,
                            phase_player_id=human, player_id=None,
                            agent_id=None, visibility_scope="spectator",
                            operator=sc.operator,
                            duration_s=round(duration, 3),
                            human_ambient=human_rows,
                            digest_after=digest_after, overrun=overrun)
                        for ai in ai_players:
                            if ai not in windows_open:
                                await open_window(ai)
                        per_round.append({
                            "round": round_no, "turn": entry_turn,
                            "human_duration_s": round(duration, 3),
                            "human_ambient_rows": len(human_rows),
                            "ai_ambient_rows": {k: len(v) for k, v in
                                                ai_manifests.items()},
                            "census_consistent":
                                snapshot.get("digest", {}).get("consistent"),
                            "digest": digest_after, "overrun": overrun,
                        })
                        active = False
                        if round_no >= turns:
                            clean = True
                            break
                    elif pid != human:
                        ai_hook_events += 1
                if clean:
                    break
                await asyncio.sleep(limits.poll_s)
                status = await counted_status()
                trace = await counted_trace()
                now = time.monotonic()
                if active and not overrun_flagged \
                        and now - turn_started > sc.turn_budget_s:
                    overrun_flagged = True
                    audit("human_turn_overrun", turn=human_turn,
                          elapsed_s=round(now - turn_started, 1),
                          note="audit only — the phase never acts")
                if now - last_heartbeat >= limits.heartbeat_s:
                    last_heartbeat = now
                    audit("engine_status", turn=status.get("TURN"),
                          turn_active=status.get("TURN_ACTIVE"),
                          round=round_no, active=active)
    except TimeoutError:
        failure = "match timeout"
    except asyncio.CancelledError:
        failure = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 — recorded, then re-raised
        failure = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        summary = {
            "phase": "spectate", "operator": sc.operator,
            "observed_players": list(sc.observed_players),
            "snapshot_scope": sc.snapshot_scope,
            "human_turn_budget_s": sc.turn_budget_s,
            "per_round": per_round,
            "completed_rounds": len(per_round), "requested_rounds": turns,
            "clean": clean and failure is None,
            "aborted": failure, "failure_reason": failure,
            "failure_stage": None if failure is None else "spectate",
            "attach": {"attached_mid_turn": attached_mid_turn,
                       "engine_turn_at_attach": engine_turn_at_attach},
            "command_census": {
                "status_polls": status_polls, "trace_polls": trace_polls,
                "digest_reads": digest_reads, "observe_reads": observe_reads,
                "recorder_commands": recorder_commands,
                "game_writes": game_writes,
            },
            "trace_gaps": watch.gaps, "census_retries": census.retries,
            "ai_hook_events": ai_hook_events,
            "limits": {"match_s": limits.match_s, "poll_s": limits.poll_s,
                       "heartbeat_s": limits.heartbeat_s,
                       "turn_budget_s": sc.turn_budget_s},
            "identity": implementation_identity(spec, mod_lua),
            "cleanup": {"status":
                        "disconnect_only_no_game_actions_no_leases"},
            "elapsed_s": round(time.monotonic() - started, 3),
        }
        await driver.match_end(
            per_round[-1]["turn"] if per_round else engine_turn_at_attach,
            summary)
        await adapter.teardown()
    print(f"SPECTATE {'CLEAN' if clean and failure is None else 'ENDED'}: "
          f"{len(per_round)}/{turns} rounds; {failure or 'completed'}",
          flush=True)
    return 0 if clean and failure is None else 2


# research housekeeping preference: era-1 techs the wire actually offers,
# then the sorted fallback — a deterministic pick that NEVER leaves
# research empty while techs remain (an empty slot is the freeze)
_TECH_PREFERENCE = ["MINING", "POTTERY", "ANIMAL_HUSBANDRY", "MASONRY",
                    "BRONZE_WORKING", "ARCHERY", "WRITING", "IRRIGATION",
                    "SAILING", "ASTROLOGY", "CATTLE"]


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


async def _resolve_blockers(adapter: FireTunerAdapter, player_id: int,
                             turn: int, *, defer_economy: bool = False) -> None:
    """Turn-blocker housekeeping at LEASE START (inside our own turn, where
    civic/policy changes are legal): a completed civic parks 'Choose a
    Civic' + 'Fill Policy Slot' on the local player, and a forced end-turn
    past them freezes the whole engine cycle (live-learned run 011).
    Resolved with the engine's own safe primitives (SetProgressingCivic,
    NEVER SetCivic; UNLOCK_POLICIES + RequestPolicyChanges). Both attrs
    are outside the recorder's coverage, so nothing here enters the
    watchdog ledgers — the wire transcript is the record."""
    _ = player_id
    rows = response_parser._split_lines(  # noqa: SLF001
        await adapter.write_raw(lua_translator.blocker_query()))
    blockers = [r for r in rows if r.startswith("BLOCKING|")]
    for b in blockers:
        if b.endswith("ENDTURN_BLOCKING_CIVIC"):
            out = await adapter.read_raw(lua_translator.resolve_civic())
            print(f"blocker[{turn}]: civic -> "
                  f"{[r for r in out if not r.endswith('---END---')]}")
        elif "FILL_CIVIC_SLOT" in b:
            out = await adapter.write_raw(lua_translator.fill_policy_slots())
            print(f"blocker[{turn}]: policies -> "
                  f"{[r for r in out if not r.endswith('---END---')]}")
        elif "PRODUCTION" in b:
            # the empty-queue city: set production for EVERY own city with
            # an empty queue (the agent may have missed one; the blocker
            # fires at turn END, when resolution freezes the cycle)
            if not defer_economy:
                await _fill_empty_queues(adapter, player_id, turn)
        elif "RESEARCH" in b:
            # completed research with no follow-up parks 'Choose a
            # Technology' on the local player — game four froze the whole
            # engine cycle here at the turn-17 transition
            if not defer_economy:
                await _ensure_research(adapter, player_id, turn)
        else:
            # unknown blocker: report it loudly — the run must not freeze
            # silently on something this housekeeping does not cover
            print(f"blocker[{turn}]: UNHANDLED {b} (manual resolution "
                  "may be needed)")
    # PROACTIVE: fill empty queues every lease start — the production
    # blocker only ever lists at turn end, when the wire can no longer
    # resolve it (glm-g1 turn 12's freeze); pre-filling makes the class
    # unreachable
    if not defer_economy:
        await _fill_empty_queues(adapter, player_id, turn)
    # Strategic control owns empty production and research through the audited
    # facade. Civic/policy housekeeping and turn completeness still apply.
    # Research is deliberately NOT pre-filled: the blocker notification
    # DOES list at lease start (game four, turn 11 — unlike production),
    # so the reactive branch above resolves it in time, and pre-filling
    # would starve the driven policy of its own research choice.


async def _fill_empty_queues(adapter: FireTunerAdapter, player_id: int,
                             turn: int) -> None:
    """Set production for every own city whose queue reads empty (the
    turtler doctrine's preference order). A city finishing its build
    mid-turn with no follow-up parks ENDTURN_BLOCKING_PRODUCTION on the
    cycle at turn end — a state the wire cannot release (glm-g1 t12).

    B2: the CITIES read is advisory only — its queue field came back '-'
    on every live row for the accessor's whole life, which made this
    re-fill EVERY turn and overwrite the agent's own choice. The
    authority is now current_production_read (the hash accessor the
    shipped UI uses): a non-zero hash means a build is in progress and
    the city is skipped regardless of what the CITIES row said."""
    cities = await adapter.observe(ObserveRequest(
        kind=ObserveKind.CITIES, player_id=player_id))
    for city in cities:
        if city["owner"] != player_id:
            continue
        # Codex r1 P2-11: the read FAILS CLOSED — a missing/unparsable
        # CURPROD row or -1 (unknown city) means we cannot prove the city
        # is idle, so we skip it. Filling on uncertainty is the exact
        # regression (overwriting the agent's own build) this gate exists
        # to prevent.
        lines = await adapter.write_raw(lua_translator.current_production_read(
            city["city_id"]))
        row = next((ln for ln in lines if ln.startswith("CURPROD|")), None)
        if row is None:
            continue
        try:
            cur = int(row.split("|", 1)[1])
        except ValueError:
            continue
        if cur != 0 or city.get("production_queue"):
            continue
        items = await adapter.observe(ObserveRequest(
            kind=ObserveKind.AVAILABLE_PRODUCTION, player_id=player_id,
            subject_id=city["city_id"]))
        by_id = {i["item_id"]: i for i in items}
        pick = next((p for p in _BUILD_PREFERENCE if p in by_id), None)
        if pick is None:
            continue
        res = await adapter.act(ActionCommand(
            tool="set_city_production",
            args={"city_id": city["city_id"], "item_id": pick},
            player_id=player_id,
            idempotency_key=f"housekeep-{turn}-{city['city_id']}",
            lease_id="housekeeping"))
        queue = city.get("production_queue")
        print(f"housekeep[{turn}]: {city['name']} queue={queue!r} "
              f"-> BUILD {pick}: {res.status}")


async def _ensure_research(adapter: FireTunerAdapter, player_id: int,
                           turn: int) -> None:
    """Set research when the player's slot reads empty. A completed tech
    mid-turn with no follow-up parks ENDTURN_BLOCKING_RESEARCH on the
    cycle — game four's turn-17 freeze (the one blocker class this
    housekeeping did not cover). Deterministic pick: the preference
    order, else the alphabetically-first available; NEVER leaves the
    slot empty while any tech is offerable."""
    overview = await adapter.observe(ObserveRequest(
        kind=ObserveKind.OVERVIEW, player_id=player_id))
    me = overview.get("players", {}).get(str(player_id), {})
    if me.get("researching"):
        return
    options = await adapter.observe(ObserveRequest(
        kind=ObserveKind.AVAILABLE_RESEARCH, player_id=player_id))
    by_id = {i["tech_id"] for i in options}
    if not by_id:
        return  # nothing offerable: the engine cannot be blocking on this
    pick = next((t for t in _TECH_PREFERENCE if t in by_id), sorted(by_id)[0])
    res = await adapter.act(ActionCommand(
        tool="set_research",
        args={"tech_id": pick},
        player_id=player_id,
        idempotency_key=f"housekeep-research-{turn}",
        lease_id="housekeeping"))
    print(f"housekeep[{turn}]: research empty -> STUDY {pick}: {res.status}")


async def _dismiss_popups(turn: int, keys: tuple[str, ...] = ("Escape", "Escape"),
                          controller=None) -> list[dict[str, Any]]:
    controller = controller or ui_control.Controller()
    results = []
    for key in keys:
        results.append(asdict(await controller.action(key=key)))
    results.append(asdict(await controller.action(banner=True)))
    return results


async def _recover_stall(adapter: FireTunerAdapter, player_id: int,
                         turn: int, keys: tuple[str, ...] = ("Escape",
                                                             "Escape"),
                         ) -> None:
    """Stall recovery on a lease-engage timeout, by SHAPE (2026-09-01):
    - ATTACH case (our turn ACTIVE, hook past, no puppet — the settle
      window can misread an imminent hook and skip the bootstrap): end
      the parked human turn ourselves, exactly what --bootstrap-end-turn
      does; the engine then processes and the next hook engages.
    - Otherwise assume a front-end modal (advisor tips freeze the
      between-turn processing): dismiss with the given keys (hotseat
      passes Return-first — Escape opens the options menu on the
      PlayerChange panel)."""
    status = await adapter.poll_status()
    if (status.get("PUPPET_ACTIVE") is not True
            and status.get("TURN_ACTIVE") is True
            and int(status.get("TURN", -1)) == turn - 1):
        print(f"recover[{turn}]: attach case — ending parked turn "
              f"{status['TURN']}")
        await adapter.write_raw(lua_translator.request_end_turn(player_id))
        return
    await _dismiss_popups(
        turn, keys, ui_control.FakeController()
        if adapter._simulate is not None else ui_control.Controller())


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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("config", type=Path)
    ap.add_argument("--phase", required=True,
                    choices=["probe", "exclusive-control", "dispatch",
                             "dispatch-hotseat", "spectate"])
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
    ap.add_argument("--bootstrap-end-turn", action="store_true",
                    help="dispatch: end a parked lease-free local turn "
                         "ourselves to reach the first driven turn (the "
                         "tuner is single-client, so the standalone "
                         "bootstrap script cannot run beside the driver)")
    ap.add_argument("--startup-timeout", type=float, default=2700)
    ap.add_argument("--match-timeout", type=float, default=7200)
    ap.add_argument("--agent-turn-timeout", type=float, default=600)
    ap.add_argument("--recovery-timeout", type=float, default=180)
    ap.add_argument("--recovery-sweeps", type=int, default=8)
    ap.add_argument("--no-turn-pacing", action="store_true",
                    help="use the standard LLM loop without the initial visible briefing")
    opts = ap.parse_args()
    spec = load_config(opts.config)
    mod_lua = opts.mod_path.read_text(encoding="utf-8")

    async def run() -> int:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        if opts.fake:
            fake_cfg: dict | None = None
            if opts.phase == "spectate":
                if spec.spectate is None:
                    raise SystemExit(
                        "--phase spectate requires a spectate: config block")
                sc = spec.spectate
                fake_cfg = {
                    "human_seat": sc.human_seat,
                    "ai_seats": [p for p in sc.observed_players
                                 if p != sc.human_seat],
                    "polls_per_human_turn": 3,
                }
            server = FakeTunerServer(mod=FakeMod(
                hotseat=[a.player_id for a in spec.agents]
                if opts.phase == "dispatch-hotseat" else None,
                spectate=fake_cfg, ambient_diffs=fake_cfg is not None))
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
        return await phase_exclusive_control(
            spec, adapter, run_dir, opts.turns, opts.strategy, mod_lua)
    if opts.phase == "dispatch-hotseat":
        return await phase_dispatch_hotseat(
            spec, adapter, run_dir, opts.turns, opts.strategy, mod_lua,
            pace_llm_turns=not getattr(opts, "no_turn_pacing", False),
            limits=HotseatLimits(startup=opts.startup_timeout, match=opts.match_timeout,
                                 agent_turn=opts.agent_turn_timeout,
                                 recovery=opts.recovery_timeout, sweeps=opts.recovery_sweeps),
            controller=ui_control.FakeController() if opts.fake else ui_control.Controller())
    if opts.phase == "spectate":
        sc = spec.spectate or None
        if sc is None:
            raise RuntimeError(
                "--phase spectate requires a spectate: block in the config")
        return await phase_spectate(
            spec, adapter, run_dir, opts.turns, mod_lua,
            limits=SpectateLimits(
                poll_s=sc.poll_s, heartbeat_s=sc.heartbeat_s,
                match_s=opts.match_timeout))
    return await phase_dispatch(
        spec, adapter, run_dir, opts.turns, opts.strategy, mod_lua,
        bootstrap=opts.bootstrap_end_turn)


if __name__ == "__main__":
    main()
