"""The Referee: leases, legality, idempotency gate, watchdog driver.

Authorization model: the referee's own ledger (ambient manifest requested at
the phase boundary + receipts of accepted commands) is the ONLY allowlist.
Adapter-reported mutation origins are diagnostic. Pre-commit rejections
(lease/schema/ownership/dedupe) never reach the game and are never watchdog
events; committed violations are flagged, counted, and — on adapters with
rollback capability — rolled back with a follow-up rejection record so replay
skips the rolled-back command.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from civ_arena.arena.events import EventLog
from civ_arena.arena.idempotency import DedupeIndex
from civ_arena.arena.telemetry import TelemetryRegistry
from civ_arena.arena.turn_lease import TurnLease, validate_lease
from civ_arena.arena.visibility import Scope, VisibilityPolicy
from civ_arena.canonical import args_digest
from civ_arena.game.adapter import (
    ActionCommand,
    MutationRecord,
    ObserveKind,
    ObserveRequest,
    RejectionReason,
)
from civ_arena.session import legality
from civ_arena.session.tools import SessionCtx


class MatchAborted(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class RefereeConfig:
    watchdog_mode: str = "flag_and_continue"  # "flag_and_continue" | "rollback"
    violation_limit: int = 5


@dataclass
class _LeaseState:
    allowed: list[MutationRecord] = field(default_factory=list)
    actual: list[MutationRecord] = field(default_factory=list)
    acknowledged: list[MutationRecord] = field(default_factory=list)
    snapshot: Any = None


class Referee:
    def __init__(
        self,
        adapter: Any,
        policy: VisibilityPolicy,
        log: EventLog,
        telemetry: TelemetryRegistry,
        match_id: str,
        game_instance_id: str,
        cfg: RefereeConfig | None = None,
        dedupe: DedupeIndex | None = None,
    ) -> None:
        self.adapter = adapter
        self.policy = policy
        self.log = log
        self.telemetry = telemetry
        self.match_id = match_id
        self.game_instance_id = game_instance_id
        self.cfg = cfg or RefereeConfig()
        self.dedupe = dedupe or DedupeIndex()
        self._lease: TurnLease | None = None
        self._ls = _LeaseState()
        self.violations_total = 0
        self.violations_by_agent: dict[str, int] = {}

    # ------------------------------------------------------------------ lease
    def grant_lease(self, player_id: int, agent_id: str, turn: int) -> TurnLease:
        lease = TurnLease(
            lease_id=f"{self.match_id}-t{turn}-p{player_id}",
            match_id=self.match_id,
            game_instance_id=self.game_instance_id,
            player_id=player_id,
            agent_id=agent_id,
            turn=turn,
            granted_seq=len(self.log),
        )
        self._lease = lease
        self._ls = _LeaseState()
        self.log.write(
            "LEASE_GRANT",
            match_id=self.match_id, game_instance_id=self.game_instance_id,
            turn=turn, phase_player_id=player_id, player_id=player_id,
            agent_id=agent_id, visibility_scope="referee",
            lease_id=lease.lease_id,
        )
        return lease

    # ------------------------------------------------------------- turn flow
    async def begin_turn(self, player_id: int, agent_id: str, turn: int) -> None:
        phase = await self.adapter.current_phase()
        if phase["turn"] != turn or phase["phase_player"] != -1:
            raise RuntimeError(f"cannot begin turn {turn} from phase {phase}")
        info = await self.adapter.begin_phase(player_id, turn)
        manifest = [MutationRecord.from_doc(d) for d in info["manifest"]]
        self._ls.allowed = list(manifest)
        self._ls.actual = list(self.adapter.drain_mutations())
        if self.cfg.watchdog_mode == "rollback" and self.adapter.capabilities().rollback:
            self._ls.snapshot = self.adapter.snapshot()
        self.log.write(
            "AMBIENT",
            match_id=self.match_id, game_instance_id=self.game_instance_id,
            turn=turn, phase_player_id=player_id, player_id=player_id,
            agent_id=agent_id, visibility_scope="referee",
            manifest=[m.to_doc() for m in manifest],
        )
        await self._sweep(player_id, agent_id, turn)

    async def end_turn(self, ctx: SessionCtx) -> dict[str, Any]:
        phase = await self._phase()
        reason = validate_lease(
            ctx.lease, current_turn=phase["turn"], phase_player=phase["phase_player"],
            player_id=ctx.player_id, agent_id=ctx.agent_id,
        )
        if reason is not None:
            self._unauthorized(ctx, phase, reason, tool="end_turn", args={})
            return {"status": "rejected", "rejection": reason.value}
        turn = ctx.lease.turn
        await self._sweep(ctx.player_id, ctx.agent_id, turn, final=True)
        await self.adapter.end_phase(ctx.player_id, turn)
        ctx.lease.release()
        self.log.write(
            "LEASE_RELEASE",
            match_id=self.match_id, game_instance_id=self.game_instance_id,
            turn=turn, phase_player_id=ctx.player_id, player_id=ctx.player_id,
            agent_id=ctx.agent_id, visibility_scope="referee",
            lease_id=ctx.lease.lease_id,
        )
        self.log.write(
            "TURN_END",
            match_id=self.match_id, game_instance_id=self.game_instance_id,
            turn=turn, phase_player_id=ctx.player_id, player_id=ctx.player_id,
            agent_id=ctx.agent_id, visibility_scope="referee",
            state_hash=self.adapter.state_hash(),
        )
        return {"status": "accepted", "turn": turn}

    # -------------------------------------------------------------- observe
    async def observe(
        self,
        ctx: SessionCtx,
        kind: ObserveKind,
        subject_id: str | None = None,
        scope: Scope = Scope.PRIVATE_PLAYER,
    ) -> Any:
        phase = await self._phase()
        if scope is Scope.REFEREE:
            self._unauthorized(ctx, phase, RejectionReason.ARGS_INVALID,
                               tool=f"observe:{kind.value}", args={"scope": "referee"},
                               detail="agent requested referee scope")
            return {"error": "referee scope is not agent-reachable"}
        reason = validate_lease(
            ctx.lease, current_turn=phase["turn"], phase_player=phase["phase_player"],
            player_id=ctx.player_id, agent_id=ctx.agent_id,
        )
        if reason is not None:
            self._unauthorized(ctx, phase, reason, tool=f"observe:{kind.value}", args={})
            return {"error": reason.value}
        omniscient = await self.adapter.observe(
            ObserveRequest(kind=kind, player_id=ctx.player_id, subject_id=subject_id)
        )
        observable, remembered = self.adapter.visibility_for(ctx.player_id)
        return self.policy.project(
            omniscient, kind.value, ctx.player_id, observable, remembered, scope
        )

    # -------------------------------------------------------------- execute
    async def execute(
        self,
        ctx: SessionCtx,
        tool: str,
        args: dict[str, Any],
        client_key: str | None = None,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        phase = await self._phase()

        if tool == "end_turn":
            return await self.end_turn(ctx)

        reason = validate_lease(
            ctx.lease, current_turn=phase["turn"], phase_player=phase["phase_player"],
            player_id=ctx.player_id, agent_id=ctx.agent_id,
        )
        if reason is not None:
            self._unauthorized(ctx, phase, reason, tool=tool, args=args)
            self.telemetry.note_call(ctx.agent_id, tool,
                                     int((time.perf_counter() - t0) * 1000), ok=False)
            return {"status": "rejected", "rejection": reason.value}

        reason = legality.validate_args(tool, args)
        if reason is None and tool in legality.KNOWN_ACTION_TOOLS:
            omni_units = await self.adapter.observe(
                ObserveRequest(kind=ObserveKind.UNITS, player_id=ctx.player_id))
            omni_cities = await self.adapter.observe(
                ObserveRequest(kind=ObserveKind.CITIES, player_id=ctx.player_id))
            reason = legality.ownership_reason(
                omni_units, omni_cities, ctx.player_id, tool, args)
        if reason is not None:
            doc = {"status": "rejected", "rejection": reason.value, "tool": tool}
            self._emit_pair(ctx, phase, tool, args, None, doc)
            self.telemetry.note_call(ctx.agent_id, tool,
                                     int((time.perf_counter() - t0) * 1000), ok=False)
            return doc

        key = DedupeIndex.key_for(ctx.player_id, tool, args, client_key,
                                  turn=phase["turn"])
        cached = self.dedupe.seen(key)
        if cached is not None:
            doc = {**cached, "duplicate": True}
            self._emit_pair(ctx, phase, tool, args, key, doc)
            self.telemetry.note_call(ctx.agent_id, tool,
                                     int((time.perf_counter() - t0) * 1000), ok=True)
            return doc

        pre_hash = self.adapter.state_hash()
        res = await self.adapter.act(ActionCommand(
            tool=tool, args=args, player_id=ctx.player_id,
            idempotency_key=key, lease_id=ctx.lease.lease_id,
        ))
        step_mutations = self.adapter.drain_mutations()
        post_hash = self.adapter.state_hash()
        if res.status == "accepted":
            self._ls.allowed.extend(res.mutations)
        self._ls.actual.extend(step_mutations)

        doc = {
            "status": res.status,
            "tool": tool,
            "result": res.result,
            "rejection": res.rejection,
            "error": res.error,
            "duplicate": False,
            "mutations": [m.to_doc() for m in step_mutations],
        }
        self._emit_pair(ctx, phase, tool, args, key, doc,
                        pre_hash=pre_hash, post_hash=post_hash,
                        receipts=[m.to_doc() for m in res.mutations])
        ok = res.status == "accepted"
        self.telemetry.note_call(ctx.agent_id, tool,
                                 int((time.perf_counter() - t0) * 1000), ok=ok)
        if ok:
            self.dedupe.record(key, {
                "status": "accepted", "tool": tool, "result": res.result,
            })

        violations = await self._sweep(ctx.player_id, ctx.agent_id, phase["turn"])
        if (violations and self.cfg.watchdog_mode == "rollback"
                and self.adapter.capabilities().rollback
                and self._ls.snapshot is not None):
            self.adapter.restore(self._ls.snapshot)
            # the command is rolled back: state is restored, the key is NOT
            # recorded (a retry may re-execute), and a follow-up rejection
            # record tells replay to skip this command.
            rolled = self.dedupe.seen(key)
            if rolled is not None:
                self.dedupe.forget(key)
            self.log.write(
                "TOOL_RESULT",
                match_id=self.match_id, game_instance_id=self.game_instance_id,
                turn=phase["turn"], phase_player_id=ctx.player_id,
                player_id=ctx.player_id, agent_id=ctx.agent_id,
                visibility_scope="referee", tool=tool,
                idempotency_key=key, status="rejected", rejection="rollback",
                rolled_back=True, after_state_hash=self.adapter.state_hash(),
            )
            doc = {**doc, "status": "rejected", "rejection": "rollback",
                   "rolled_back": True}
        return doc

    # -------------------------------------------------------------- watchdog
    async def _sweep(self, player_id: int, agent_id: str, turn: int,
                     final: bool = False) -> list[Any]:
        from civ_arena.arena.watchdog import diff

        self._ls.actual.extend(self.adapter.drain_mutations())
        violations = diff(self._ls.actual,
                          self._ls.allowed + self._ls.acknowledged)
        if not violations:
            return []
        for violation in violations:
            self.log.write(
                "VIOLATION",
                match_id=self.match_id, game_instance_id=self.game_instance_id,
                turn=turn, phase_player_id=player_id, player_id=player_id,
                agent_id=agent_id, visibility_scope="referee",
                watchdog=violation.to_doc(), final=final,
            )
            self.violations_total += 1
            self.violations_by_agent[agent_id] = (
                self.violations_by_agent.get(agent_id, 0) + 1
            )
            # acknowledge the flagged mutations so sweeps don't re-report them
            self._ls.acknowledged.extend(
                MutationRecord.from_doc(m) for m in violation.mutations
            )
        if self.violations_total > self.cfg.violation_limit:
            raise MatchAborted(
                f"violation limit exceeded ({self.violations_total} > "
                f"{self.cfg.violation_limit})"
            )
        return violations

    # -------------------------------------------------------------- misc
    async def _phase(self) -> dict[str, Any]:
        return await self.adapter.current_phase()

    async def referee_snapshot(self) -> Any:
        """Omniscient overview. Debug/eval only — NEVER handed to an agent."""
        return await self.adapter.observe(ObserveRequest(
            kind=ObserveKind.OVERVIEW, player_id=0))

    def violation_count(self, agent_id: str | None = None) -> int:
        if agent_id is None:
            return self.violations_total
        return self.violations_by_agent.get(agent_id, 0)

    def _unauthorized(self, ctx: SessionCtx, phase: dict[str, Any],
                      reason: RejectionReason, *, tool: str, args: dict[str, Any],
                      detail: str | None = None) -> None:
        self.log.write(
            "UNAUTHORIZED_TOOL_CALL",
            match_id=self.match_id, game_instance_id=self.game_instance_id,
            turn=phase.get("turn", -1), phase_player_id=phase.get("phase_player", -1),
            player_id=ctx.player_id, agent_id=ctx.agent_id,
            visibility_scope="referee", tool=tool, args=args,
            rejection=reason.value, detail=detail,
        )

    def _emit_pair(
        self,
        ctx: SessionCtx,
        phase: dict[str, Any],
        tool: str,
        args: dict[str, Any],
        key: str | None,
        result_doc: dict[str, Any],
        pre_hash: str | None = None,
        post_hash: str | None = None,
        receipts: list[dict[str, Any]] | None = None,
    ) -> None:
        common = dict(
            match_id=self.match_id, game_instance_id=self.game_instance_id,
            turn=phase.get("turn", -1), phase_player_id=phase.get("phase_player", -1),
            player_id=ctx.player_id, agent_id=ctx.agent_id,
        )
        self.log.write(
            "TOOL_CALL", visibility_scope="private_player", tool=tool,
            args=args, args_digest=args_digest(args), idempotency_key=key,
            **common,
        )
        payload: dict[str, Any] = dict(
            visibility_scope="private_player", tool=tool,
            status=result_doc.get("status"), rejection=result_doc.get("rejection"),
            duplicate=bool(result_doc.get("duplicate")),
            result_doc=result_doc.get("result"),
            idempotency_key=key,
        )
        if pre_hash is not None:
            payload["before_state_hash"] = pre_hash
        if post_hash is not None:
            payload["after_state_hash"] = post_hash
        if receipts is not None:
            payload["receipts"] = receipts
        if result_doc.get("mutations"):
            payload["mutations"] = result_doc["mutations"]
        self.log.write("TOOL_RESULT", **payload, **common)
