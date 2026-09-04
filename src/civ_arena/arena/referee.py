"""The Referee: leases, legality, idempotency gate, watchdog driver.

Authorization model: the referee's own ledger (ambient manifest requested at
the phase boundary + receipts of accepted commands) is the ONLY allowlist.
Adapter-reported mutation origins are diagnostic. Pre-commit rejections
(lease/schema/ownership/dedupe) never reach the game and are never watchdog
events; committed violations are flagged, counted, and — on adapters with
rollback capability — rolled back with a follow-up rejection record so replay
skips the rolled-back command.

Rollback semantics: begin-phase violations restore the PRE-phase snapshot and
re-run begin_phase (ambient re-applies on clean state; a consumed chaos event
does not re-fire). Command violations restore the PER-COMMAND snapshot, so an
earlier accepted command in the same lease always survives. End-phase
violations (fired inside adapter.end_phase, after the lease) are
flag-and-count only — restoring there would undo a phase the engine already
closed.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from civ_arena.arena.diary import MAX_DIARY_CHARS, DiaryStore
from civ_arena.arena.events import EventLog
from civ_arena.arena.idempotency import DedupeIndex
from civ_arena.arena.telemetry import TelemetryRegistry
from civ_arena.arena.turn_lease import TurnLease, validate_lease
from civ_arena.arena.visibility import Scope, VisibilityPolicy
from civ_arena.canonical import args_digest, canonical
from civ_arena.game.adapter import (
    ActionCommand,
    MutationRecord,
    ObserveKind,
    ObserveRequest,
    RejectionReason,
)
from civ_arena.recall import RecallCorpus
from civ_arena.session import legality
from civ_arena.session.tools import SessionCtx
from civ_arena.strategy.digest import observation_digest
from civ_arena.strategy.store import StrategyStore


class MatchAborted(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _log_safe(value: Any) -> Any:
    """Canonical JSON rejects floats; typed claim params are str/int, so
    anything else a DIRECT caller smuggles in (a float, a list) is logged
    as None — the store rejects the call regardless, and the RAW args_digest
    contract only needs to be canonical-safe, not forensic."""
    return value if isinstance(value, (str, int, bool)) or value is None \
        else None


# the recall digest's serialized-size ceiling: the config enforces
# max_result_chars >= RECALL_RESULT_FLOOR (see config.py) so a digest built
# under this budget always reaches the model untruncated
RECALL_DIGEST_BUDGET = 3900


@dataclass
class RefereeConfig:
    watchdog_mode: str = "flag_and_continue"  # "flag_and_continue" | "rollback"
    violation_limit: int = 5
    # TURN-COMPLETENESS GATE (operator request 2026-09-03): reject
    # end_turn ONCE per (player, turn) while own units still have
    # movement and no standing order — models "forget" unit handling
    # otherwise (live-observed llm-minimax2-002: founded cities, never
    # moved the warriors).
    completeness_gate: bool = False
    # LIVE-HOTSEAT ONLY (the sim/tests stay strict — the watchdog-teeth
    # pin drifts an own-unit movement row and MUST keep flagging): the
    # engine's movement bookkeeping around sanctioned acts (fortify
    # consumes moves + settles the hex; the H2 end-path zeroes movement)
    # is under-declared by the mod's act manifests, and those rows
    # crashed live matches as phantom violations (llm-minimax2-002).
    # Tolerated ONLY for the driven seat's own units' movement-class
    # rows; everything else — foreign rows, non-movement attrs — stays
    # strict. The durable fix is mod-side act manifests; this flag is
    # the live-lane bridge until then.
    declare_own_endpath_drift: bool = False


@dataclass
class _LeaseState:
    allowed: list[MutationRecord] = field(default_factory=list)
    actual: list[MutationRecord] = field(default_factory=list)
    acknowledged: list[MutationRecord] = field(default_factory=list)
    lease_start_snapshot: Any = None


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
        diary: DiaryStore | None = None,
        strategy: StrategyStore | None = None,
        recall: RecallCorpus | None = None,
    ) -> None:
        self.adapter = adapter
        self.policy = policy
        self.log = log
        self.telemetry = telemetry
        self.match_id = match_id
        self.game_instance_id = game_instance_id
        self.cfg = cfg or RefereeConfig()
        self.dedupe = dedupe or DedupeIndex()
        self.diary = diary or DiaryStore()
        self.strategy = strategy or StrategyStore()
        self.recall = recall
        self._lease: TurnLease | None = None
        self._ls = _LeaseState()
        self._completeness_bounced: set[tuple[int, int]] = set()
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

    def _lease_reason(self, ctx: SessionCtx, phase: dict[str, Any],
                      *, tool: str, args: dict[str, Any]) -> RejectionReason | None:
        """Lease validation INCLUDING identity: the ctx must carry the lease
        this referee issued, not merely a structurally similar one. Every
        failure — foreign identity OR expired/released state — is written to
        the durable audit log."""
        if self._lease is None or ctx.lease is None or ctx.lease is not self._lease:
            self._unauthorized(ctx, phase, RejectionReason.LEASE_FOREIGN,
                               tool=tool, args=args,
                               detail="lease is not the referee-issued lease")
            return RejectionReason.LEASE_FOREIGN
        reason = validate_lease(
            ctx.lease, current_turn=phase["turn"], phase_player=phase["phase_player"],
            player_id=ctx.player_id, agent_id=ctx.agent_id,
        )
        if reason is not None:
            self._unauthorized(ctx, phase, reason, tool=tool, args=args)
        return reason

    def _can_rollback(self) -> bool:
        return (self.cfg.watchdog_mode == "rollback"
                and self.adapter.capabilities().rollback)

    # ------------------------------------------------------------- turn flow
    async def begin_turn(self, player_id: int, agent_id: str, turn: int) -> None:
        phase = await self.adapter.current_phase()
        if phase["turn"] != turn or phase["phase_player"] != -1:
            raise RuntimeError(f"cannot begin turn {turn} from phase {phase}")
        # PRE-phase snapshot: a begin-phase violation restores this and
        # re-runs begin_phase (ambient re-applies; consumed chaos does not).
        # Each retry starts with FRESH ledger bookkeeping — acknowledged
        # violations from a discarded attempt must never mask new ones.
        snapshot = self.adapter.snapshot() if self._can_rollback() else None
        for attempt in range(3):
            info = await self.adapter.begin_phase(player_id, turn)
            manifest = [MutationRecord.from_doc(d) for d in info["manifest"]]
            self._ls.allowed = list(manifest)
            self._ls.actual = list(self.adapter.drain_mutations())
            self.log.write(
                "AMBIENT",
                match_id=self.match_id, game_instance_id=self.game_instance_id,
                turn=turn, phase_player_id=player_id, player_id=player_id,
                agent_id=agent_id, visibility_scope="referee",
                attempt=attempt,
                manifest=[m.to_doc() for m in manifest],
            )
            try:
                violations = await self._sweep(player_id, agent_id, turn)
            except MatchAborted:
                if snapshot is not None:
                    self.adapter.restore(snapshot)
                raise
            if not violations or snapshot is None:
                return  # clean, or flag-and-continue mode: state stands
            if attempt < 2:
                self.adapter.restore(snapshot)
                self._ls = _LeaseState(lease_start_snapshot=snapshot)
        # Retries exhausted: the LAST attempt's phase stays OPEN with its
        # violations flagged — a closed phase would strand the lease (it can
        # neither act nor end its phase). Bounded: at most 3 rollbacks.

    async def end_turn(self, ctx: SessionCtx) -> dict[str, Any]:
        t0 = time.perf_counter()
        phase = await self._phase()
        reason = self._lease_reason(ctx, phase, tool="end_turn", args={})
        if reason is not None:
            self.telemetry.note_call(ctx.agent_id, "end_turn",
                                     int((time.perf_counter() - t0) * 1000), ok=False)
            self._emit_pair(ctx, phase, "end_turn", {}, None,
                            {"status": "rejected", "rejection": reason.value})
            return {"status": "rejected", "rejection": reason.value}
        turn = ctx.lease.turn
        # TURN-COMPLETENESS GATE: one structured bounce per (player, turn)
        # when own units still have movement and no standing order — the
        # agent gets the ids and can move/fortify/sleep each, then re-end.
        # The second attempt always passes (no deadlock on a stubborn or
        # scripted agent).
        if (self.cfg.completeness_gate
                and (ctx.player_id, turn) not in self._completeness_bounced):
            unmoved = await self._unmoved_units(ctx)
            if unmoved:
                self._completeness_bounced.add((ctx.player_id, turn))
                doc = {"status": "rejected", "rejection": "unmoved_units",
                       "unmoved_units": unmoved,
                       "note": "these units still have movement and no "
                               "standing order — move each, or fortify/"
                               "sleep it, then call end_turn again"}
                self._emit_pair(ctx, phase, "end_turn", {}, None, doc)
                self.telemetry.note_call(
                    ctx.agent_id, "end_turn",
                    int((time.perf_counter() - t0) * 1000), ok=False)
                return doc
        await self._sweep(ctx.player_id, ctx.agent_id, turn, final=True)
        await self.adapter.end_phase(ctx.player_id, turn)
        # SANCTIONED END-PATH DECLARATION — live-hotseat configs only
        # (see RefereeConfig.declare_own_endpath_drift)
        if self.cfg.declare_own_endpath_drift:
            await self._declare_own_endpath_drift(ctx)
        ctx.lease.release()
        self.log.write(
            "LEASE_RELEASE",
            match_id=self.match_id, game_instance_id=self.game_instance_id,
            turn=turn, phase_player_id=ctx.player_id, player_id=ctx.player_id,
            agent_id=ctx.agent_id, visibility_scope="referee",
            lease_id=ctx.lease.lease_id,
        )
        post_hash = self.adapter.state_hash()
        self.log.write(
            "TURN_END",
            match_id=self.match_id, game_instance_id=self.game_instance_id,
            turn=turn, phase_player_id=ctx.player_id, player_id=ctx.player_id,
            agent_id=ctx.agent_id, visibility_scope="referee",
            state_hash=post_hash,
        )
        # The end_turn tool records are emitted BEFORE the post-phase sweep can
        # raise: a limit abort here must still leave a replayable end_turn.
        self._emit_pair(ctx, phase, "end_turn", {}, None,
                        {"status": "accepted", "turn": turn}, post_hash=post_hash)
        self.telemetry.note_call(ctx.agent_id, "end_turn",
                                 int((time.perf_counter() - t0) * 1000), ok=True)
        # Post-phase sweep: chaos fired INSIDE adapter.end_phase is caught HERE,
        # blamed on this agent — never silently carried into the final state or
        # dumped on the next player's ledger. Flag-and-count only (see module
        # docstring for why rollback does not apply past the phase boundary).
        await self._sweep(ctx.player_id, ctx.agent_id, turn, final=True)
        return {"status": "accepted", "turn": turn}

    # -------------------------------------------------------------- diary
    async def _declare_own_endpath_drift(self, ctx: SessionCtx) -> None:
        """Acknowledge the just-driven seat's own-entity MOVEMENT-class
        rows currently in the journal (see end_turn). NON-destructive —
        the rows stay in the journal for the post-phase sweep; the
        acknowledgment marks them declared. Ownership resolves via the
        omni pid fields; anything foreign, unknown, or non-movement
        stays strict."""
        omni_units = await self.adapter.observe(
            ObserveRequest(kind=ObserveKind.UNITS, player_id=ctx.player_id))
        units = omni_units if isinstance(omni_units, list) \
            else list(omni_units.values())
        owner_of = {u.get("unit_id"): u.get("owner") for u in units}
        journal = getattr(self.adapter, "_journal", None)
        admitted = []
        for m in list(journal or []):
            eid = getattr(m, "entity_id", None)
            if (getattr(m, "entity_type", None) == "unit"
                    and owner_of.get(eid) == ctx.player_id
                    and getattr(m, "attr", None) in ("moves", "movement",
                                                     "pos", "q", "r")):
                self._ls.acknowledged.append(m)
                admitted.append(m.to_doc())
        self.log.write(
            "HEARTBEAT", match_id=self.match_id,
            game_instance_id=self.game_instance_id, turn=ctx.turn,
            phase_player_id=ctx.player_id, player_id=ctx.player_id,
            agent_id=ctx.agent_id, visibility_scope="referee",
            audit="movement_allowance", allowance="declare_own_endpath_drift",
            mutations=admitted, owners={m["entity_id"]: owner_of[m["entity_id"]]
                                        for m in admitted},
        )

    async def _unmoved_units(self, ctx: SessionCtx) -> list[str]:
        """Own units with movement remaining and no standing order (the
        units_read exposes `fortified` via GetFortifyTurns — fortify AND
        civilian sleep both accumulate fortify turns engine-side). Owner
        comparison mirrors legality.ownership_reason (referee scope, the
        omniscient peek — own units are always visible to themselves)."""
        omni = await self.adapter.observe(
            ObserveRequest(kind=ObserveKind.UNITS, player_id=ctx.player_id))
        units = omni if isinstance(omni, list) else list(omni.values())
        return [u["unit_id"] for u in units
                if u.get("owner") == ctx.player_id
                and int(u.get("movement") or 0) > 0
                and not u.get("fortified")]

    async def write_diary(self, ctx: SessionCtx, text: str) -> dict[str, Any]:
        """Store this player's cross-turn note. Validated like every tool
        (lease, then shape) and logged as a TOOL_CALL/TOOL_RESULT pair — but
        NOT a game action: no adapter call, no state hash, no mutations, so
        nothing here can trip the watchdog."""
        t0 = time.perf_counter()
        phase = await self._phase()
        # args are logged RAW — exactly what the caller sent. Any transform
        # here would change args_digest on replay and diverge the model-free
        # replay; oversized input is bounded upstream (the LLM runtime
        # rejects it as malformed arguments before reaching this method).
        log_args: dict[str, Any] = (
            {"text": text} if isinstance(text, str)
            else {"text": None, "bad_type": type(text).__name__}
        )
        reason = self._lease_reason(ctx, phase, tool="write_diary", args=log_args)
        if reason is not None:
            self.telemetry.note_call(ctx.agent_id, "write_diary",
                                     int((time.perf_counter() - t0) * 1000), ok=False)
            self._emit_pair(ctx, phase, "write_diary", log_args, None,
                            {"status": "rejected", "rejection": reason.value})
            return {"status": "rejected", "rejection": reason.value}
        if (not isinstance(text, str)
                or not 1 <= len(text.strip()) <= MAX_DIARY_CHARS
                or len(text) > MAX_DIARY_CHARS):
            # the RAW length is bounded too: "x" + a million spaces would
            # otherwise pass the strip check and be stored/fsync'd verbatim
            doc = {"status": "rejected", "rejection": RejectionReason.ARGS_INVALID.value,
                   "tool": "write_diary"}
            self._emit_pair(ctx, phase, "write_diary", log_args, None, doc)
            self.telemetry.note_call(ctx.agent_id, "write_diary",
                                     int((time.perf_counter() - t0) * 1000), ok=False)
            return doc
        self.diary.write(ctx.player_id, text)
        doc = {"status": "accepted", "tool": "write_diary", "chars": len(text)}
        self._emit_pair(ctx, phase, "write_diary", log_args, None, doc)
        self.telemetry.note_call(ctx.agent_id, "write_diary",
                                 int((time.perf_counter() - t0) * 1000), ok=True)
        return doc

    # -------------------------------------------------------------- strategy
    async def set_goal(
        self, ctx: SessionCtx, text: str, goal_id: str = "", by_turn: int = 0,
        metric: str = "", target: int = 0, status: str = "active",
        confidence: int = 50,
    ) -> dict[str, Any]:
        """Commit or revise a goal. The write_diary shape: validated
        non-action — no adapter call, no state hash, no mutations."""
        return await self._claim(ctx, "set_goal", {
            "text": _log_safe(text), "goal_id": _log_safe(goal_id),
            "by_turn": _log_safe(by_turn), "metric": _log_safe(metric),
            "target": _log_safe(target), "status": _log_safe(status),
            "confidence": _log_safe(confidence),
        })

    async def record_prediction(
        self, ctx: SessionCtx, text: str, review_turn: int,
        prediction_id: str = "", subject_id: str = "", metric: str = "",
        target: int = 0, confidence: int = 50,
    ) -> dict[str, Any]:
        """Record a testable claim about the future."""
        return await self._claim(ctx, "record_prediction", {
            "text": _log_safe(text),
            "review_turn": _log_safe(review_turn),
            "prediction_id": _log_safe(prediction_id),
            "subject_id": _log_safe(subject_id),
            "metric": _log_safe(metric), "target": _log_safe(target),
            "confidence": _log_safe(confidence),
        })

    async def record_lesson(
        self, ctx: SessionCtx, text: str, about: str = "",
    ) -> dict[str, Any]:
        """Record a durable takeaway, optionally about an own claim."""
        return await self._claim(ctx, "record_lesson", {
            "text": _log_safe(text), "about": _log_safe(about),
        })

    async def _claim(
        self, ctx: SessionCtx, tool: str, log_args: dict[str, Any],
    ) -> dict[str, Any]:
        """The shared claim-tool path: lease, then the store's SINGLE
        validation+mutation path, args logged RAW, pair emitted, telemetry
        noted. The TOOL_CALL of this pair is the next record written, so its
        seq (len(log) now) is the claim's provenance — exactly the seq
        from_log later derives from that record."""
        t0 = time.perf_counter()
        phase = await self._phase()
        reason = self._lease_reason(ctx, phase, tool=tool, args=log_args)
        if reason is not None:
            self.telemetry.note_call(ctx.agent_id, tool,
                                     int((time.perf_counter() - t0) * 1000),
                                     ok=False)
            self._emit_pair(ctx, phase, tool, log_args, None,
                            {"status": "rejected", "rejection": reason.value})
            return {"status": "rejected", "rejection": reason.value}
        seq = len(self.log)
        doc = self.strategy.apply_claim(
            tool, ctx.player_id, log_args, phase["turn"], seq)
        self._emit_pair(ctx, phase, tool, log_args, None, doc)
        self.telemetry.note_call(
            ctx.agent_id, tool, int((time.perf_counter() - t0) * 1000),
            ok=doc.get("status") == "accepted")
        return doc

    async def get_strategy(self, ctx: SessionCtx) -> dict[str, Any]:
        """Read the current strategy view — the same docs the next turn's
        header renders (view_for), so a mid-turn read can never disagree
        with it. Requires the lease like every tool; the payload rides under
        a key _emit_pair does not lift, so the log stays lean (the view is
        derivable from the log, like every observation)."""
        t0 = time.perf_counter()
        phase = await self._phase()
        reason = self._lease_reason(ctx, phase, tool="get_strategy", args={})
        if reason is not None:
            self.telemetry.note_call(ctx.agent_id, "get_strategy",
                                     int((time.perf_counter() - t0) * 1000),
                                     ok=False)
            self._emit_pair(ctx, phase, "get_strategy", {}, None,
                            {"status": "rejected", "rejection": reason.value})
            return {"status": "rejected", "rejection": reason.value}
        doc = {"status": "accepted", "tool": "get_strategy",
               "strategy": self.strategy.view_for(ctx.player_id)}
        self._emit_pair(ctx, phase, "get_strategy", {}, None, doc)
        self.telemetry.note_call(ctx.agent_id, "get_strategy",
                                 int((time.perf_counter() - t0) * 1000),
                                 ok=True)
        return doc

    async def recall_lessons(self, ctx: SessionCtx, query: str) -> dict[str, Any]:
        """Read this agent's OWN durable lessons from prior matches, by
        topic query — the cross-match memory (M13). The get_strategy shape
        plus one additive lift: the digest of exactly what was returned
        rides the TOOL_RESULT as ``recalled`` (the observed-digest
        discipline — replay's projection ignores additive fields, so
        model-free replay cannot diverge, and the log carries what the model
        was fed). No corpus configured -> an honest tool_unavailable
        rejection (accepted-empty would lie about what exists)."""
        t0 = time.perf_counter()
        log_args = {"query": _log_safe(query)}
        phase = await self._phase()
        reason = self._lease_reason(ctx, phase, tool="recall_lessons",
                                    args=log_args)
        if reason is not None:
            self.telemetry.note_call(ctx.agent_id, "recall_lessons",
                                     int((time.perf_counter() - t0) * 1000),
                                     ok=False)
            self._emit_pair(ctx, phase, "recall_lessons", log_args, None,
                            {"status": "rejected", "rejection": reason.value})
            return {"status": "rejected", "rejection": reason.value}
        if self.recall is None:
            doc: dict[str, Any] = {
                "status": "rejected", "rejection": "tool_unavailable",
                "reason": "recall is not configured for this match "
                          "(no recall_runs in the config)",
            }
            self._emit_pair(ctx, phase, "recall_lessons", log_args, None, doc)
            self.telemetry.note_call(ctx.agent_id, "recall_lessons",
                                     int((time.perf_counter() - t0) * 1000),
                                     ok=False)
            return doc
        lessons = (self.recall.query(ctx.agent_id, query)
                   if isinstance(query, str) else [])
        digest = {"query": query, "lessons": lessons}
        # bound the SERIALIZED digest (escaping included — json.dumps
        # expands backslashes and non-ASCII, so a character-count floor
        # cannot bound it) by dropping WHOLE lessons from the tail until it
        # fits: whatever lands in the log then reaches the model untruncated
        # under the config-enforced result cap, and the log-vs-feed contract
        # holds for any lesson content
        while lessons and len(json.dumps(
                digest, sort_keys=True, default=str)) > RECALL_DIGEST_BUDGET:
            lessons.pop()
            digest = {"query": query, "lessons": lessons}
        canonical(digest)  # non-canonical fails BEFORE any record is written
        doc = {"status": "accepted", "tool": "recall_lessons",
               "recalled": digest}
        self._emit_pair(ctx, phase, "recall_lessons", log_args, None, doc)
        self.telemetry.note_call(ctx.agent_id, "recall_lessons",
                                 int((time.perf_counter() - t0) * 1000),
                                 ok=True)
        return doc

    # -------------------------------------------------------------- observe
    async def observe(
        self,
        ctx: SessionCtx,
        kind: ObserveKind,
        subject_id: str | None = None,
        scope: Scope = Scope.PRIVATE_PLAYER,
    ) -> Any:
        t0 = time.perf_counter()
        tool = f"get_{kind.value}"
        # record the tool's real positional arg name so replay can dispatch
        args = {"city_id": subject_id} if subject_id else {}
        phase = await self._phase()
        if scope is Scope.REFEREE:
            self._unauthorized(ctx, phase, RejectionReason.ARGS_INVALID,
                               tool=tool, args={"scope": "referee"},
                               detail="agent requested referee scope")
            self.telemetry.note_call(ctx.agent_id, tool,
                                     int((time.perf_counter() - t0) * 1000), ok=False)
            return {"error": "referee scope is not agent-reachable"}
        reason = self._lease_reason(ctx, phase, tool=tool, args=args)
        if reason is not None:
            self.telemetry.note_call(ctx.agent_id, tool,
                                     int((time.perf_counter() - t0) * 1000), ok=False)
            self._emit_pair(ctx, phase, tool, args, None,
                            {"status": "rejected", "rejection": reason.value})
            return {"error": reason.value}
        omniscient = await self.adapter.observe(
            ObserveRequest(kind=kind, player_id=ctx.player_id, subject_id=subject_id)
        )
        observable, remembered = self.adapter.visibility_for(ctx.player_id)
        projected = self.policy.project(
            omniscient, kind.value, ctx.player_id, observable, remembered, scope
        )
        # The strategy digest rides the TOOL_RESULT (an additive field — the
        # replay projection ignores it, so model-free replay cannot diverge
        # on it) and feeds the live store with the RESULT record's own seq
        # as sighting provenance: exactly what StrategyStore.from_log later
        # derives from the same record, so live == rebuilt by construction.
        result_doc: dict[str, Any] = {"status": "accepted"}
        digest = observation_digest(kind.value, projected, ctx.player_id)
        if digest is not None:
            result_doc["observed"] = digest
        result_seq = self._emit_pair(ctx, phase, tool, args, None, result_doc)
        if digest is not None:
            self.strategy.note_observation(
                ctx.player_id, phase["turn"], result_seq, tool, digest)
        self.telemetry.note_call(ctx.agent_id, tool,
                                 int((time.perf_counter() - t0) * 1000), ok=True)
        return projected

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

        reason = self._lease_reason(ctx, phase, tool=tool, args=args)
        if reason is not None:
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

        # PER-COMMAND snapshot: a rollback undoes only this command; earlier
        # accepted commands in the lease survive.
        cmd_snapshot = self.adapter.snapshot() if self._can_rollback() else None
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

        try:
            violations = await self._sweep(ctx.player_id, ctx.agent_id, phase["turn"])
        except MatchAborted:
            # the limit tripped: still roll the command back before aborting
            if cmd_snapshot is not None:
                self._rollback_command(ctx, phase, tool, key, cmd_snapshot)
            raise
        if violations and cmd_snapshot is not None:
            doc = self._rollback_command(ctx, phase, tool, key, cmd_snapshot, doc)
        return doc

    def _rollback_command(
        self,
        ctx: SessionCtx,
        phase: dict[str, Any],
        tool: str,
        key: str,
        cmd_snapshot: Any,
        doc: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Restore the per-command snapshot; mark the command rejected for replay."""
        self.adapter.restore(cmd_snapshot)
        if self.dedupe.seen(key) is not None:
            self.dedupe.forget(key)  # a retry may re-execute
        self.log.write(
            "TOOL_RESULT",
            match_id=self.match_id, game_instance_id=self.game_instance_id,
            turn=phase["turn"], phase_player_id=ctx.player_id,
            player_id=ctx.player_id, agent_id=ctx.agent_id,
            visibility_scope="referee", tool=tool,
            idempotency_key=key, status="rejected", rejection="rollback",
            rolled_back=True, after_state_hash=self.adapter.state_hash(),
        )
        if doc is not None:
            return {**doc, "status": "rejected", "rejection": "rollback",
                    "rolled_back": True}
        return {"status": "rejected", "rejection": "rollback", "rolled_back": True}

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
    async def abort_cleanup(self, agent_id: str) -> None:
        """After a MatchAborted: close an open phase THROUGH the adapter and
        sweep whatever it fires — cleanup must not smuggle in unobserved
        mutations (an end_phase-queued chaos event would otherwise land
        unflagged)."""
        import contextlib

        if self.adapter.state is None or self.adapter.state.phase_player == -1:
            return
        pid = self.adapter.state.phase_player
        turn = self.adapter.state.turn
        with contextlib.suppress(RuntimeError):
            await self.adapter.end_phase(pid, turn)
        with contextlib.suppress(MatchAborted):  # already aborting
            await self._sweep(pid, agent_id, turn, final=True)

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

    def restore_violation_counters(self, total: int,
                                   by_agent: dict[str, int] | None) -> None:
        """Resume support: restore counters persisted in checkpoints."""
        self.violations_total = int(total)
        self.violations_by_agent = dict(by_agent or {})

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
    ) -> int:
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
        if result_doc.get("observed") is not None:
            payload["observed"] = result_doc["observed"]
        if result_doc.get("recalled") is not None:
            payload["recalled"] = result_doc["recalled"]
        return self.log.write("TOOL_RESULT", **payload, **common)
