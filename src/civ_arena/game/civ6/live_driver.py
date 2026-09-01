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
import json
import os
import time
from pathlib import Path
from typing import Any

from civ_arena.agents.runtime import AgentProfile, build_runtime
from civ_arena.arena.diary import DiaryStore
from civ_arena.arena.events import EventLog
from civ_arena.arena.referee import Referee, RefereeConfig
from civ_arena.arena.telemetry import TelemetryRegistry
from civ_arena.arena.visibility import VisibilityPolicy
from civ_arena.config import MatchSpec, load_config
from civ_arena.game.adapter import ActionCommand, ObserveKind, ObserveRequest
from civ_arena.game.civ6 import lua_translator, response_parser
from civ_arena.game.civ6.fake_tuner_server import FakeMod, FakeTunerServer
from civ_arena.game.civ6.firetuner import FireTunerAdapter
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
        # Arena.run envelope parity (Codex P2-6): replay reads
        # final_state_hash and _strip walks these fields — a live log must
        # carry the same keys (scores stay empty until the sim-shaped
        # observe surface lands in M14c; live matches are not corpus
        # members, so projection tolerates the empty civ table).
        summary = {
            "match_id": self.spec.match_id,
            "game_instance_id": self.game_instance_id,
            "final_turn": final_turn,
            "aborted": None,
            "violations_total": self.referee.violation_count(),
            "final_state_hash": self.adapter.state_hash(),
            "telemetry": self.telemetry.snapshot(),
            "scores": {},
            **extra,
        }
        self._write("MATCH_END", turn=final_turn, summary=summary)
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
    profile = AgentProfile(
        agent_id=agent.agent_id, player_id=agent.player_id,
        policy=agent.policy, seed=agent.seed, model=agent.model,
        llm=agent.llm, proposer=agent.proposer)
    runtime = build_runtime(profile)
    driver = LiveDriver(spec, adapter, run_dir,
                        f"{spec.match_id}-i{os.getpid()}")
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
            await driver.referee.begin_turn(
                agent.player_id, agent.agent_id, turn)
            digest_open = await adapter.refresh_digest()
            await _resolve_blockers(adapter, agent.player_id, turn)
            # Housekeeping mutations are DRIVER-commanded, not
            # agent-commanded — they must not read as uncommanded drift in
            # the session's watchdog window. The civic/policy resolutions
            # dodge this only because their attrs sit outside the
            # recorder's coverage; research IS covered (player snapshot
            # parity), so acknowledge explicitly. The wire transcript
            # remains the record — same trust class as the resolutions.
            driver.referee._ls.acknowledged.extend(  # noqa: SLF001
                adapter.drain_mutations())
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
            # move there-and-back) — so it must not flag.
            row["unexpected"] = row["digest_changed"] and not row["mutated"]
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
        await adapter.teardown()


# the turtler doctrine's own build preference (agents/scripted.py)
_BUILD_PREFERENCE = ["MONUMENT", "WALLS", "WARRIOR", "GRANARY", "SETTLER",
                     "SCOUT", "SLINGER", "BARRACKS"]

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
                             turn: int) -> None:
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
            await _fill_empty_queues(adapter, player_id, turn)
        elif "RESEARCH" in b:
            # completed research with no follow-up parks 'Choose a
            # Technology' on the local player — game four froze the whole
            # engine cycle here at the turn-17 transition
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
    await _fill_empty_queues(adapter, player_id, turn)
    # Research is deliberately NOT pre-filled: the blocker notification
    # DOES list at lease start (game four, turn 11 — unlike production),
    # so the reactive branch above resolves it in time, and pre-filling
    # would starve the driven policy of its own research choice.


async def _fill_empty_queues(adapter: FireTunerAdapter, player_id: int,
                             turn: int) -> None:
    """Set production for every own city whose queue reads empty (the
    turtler doctrine's preference order). A city finishing its build
    mid-turn with no follow-up parks ENDTURN_BLOCKING_PRODUCTION on the
    cycle at turn end — a state the wire cannot release (glm-g1 t12)."""
    cities = await adapter.observe(ObserveRequest(
        kind=ObserveKind.CITIES, player_id=player_id))
    for city in cities:
        if city["owner"] != player_id or city.get("production_queue"):
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
                    choices=["probe", "exclusive-control", "dispatch"])
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
    opts = ap.parse_args()
    spec = load_config(opts.config)
    mod_lua = opts.mod_path.read_text(encoding="utf-8")

    async def run() -> int:
        if opts.fake:
            server = FakeTunerServer(mod=FakeMod())
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
    return await phase_dispatch(
        spec, adapter, run_dir, opts.turns, opts.strategy, mod_lua,
        bootstrap=opts.bootstrap_end_turn)


if __name__ == "__main__":
    main()
