"""Arena: owns the match loop, the adapter, the agents, checkpoints, heartbeat."""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import signal
from pathlib import Path
from typing import Any

from civ_arena.agents.runtime import AgentProfile, build_runtime
from civ_arena.arena.checkpoints import CheckpointManager, CheckpointState
from civ_arena.arena.diary import DiaryStore
from civ_arena.arena.events import EventLog
from civ_arena.arena.referee import MatchAborted, Referee, RefereeConfig
from civ_arena.arena.spend import SpendLedger
from civ_arena.arena.telemetry import TelemetryRegistry
from civ_arena.arena.visibility import VisibilityPolicy
from civ_arena.canonical import checkpoint_hash, log_prefix_hash, rng_to_doc, state_hash
from civ_arena.config import MatchSpec
from civ_arena.game.sim.chaos import ChaosDirector, ChaosEvent, MutationSpec
from civ_arena.game.sim.simulator import SimulatorAdapter
from civ_arena.recall import RecallCorpus
from civ_arena.session.player_session import PlayerSession
from civ_arena.strategy.store import StrategyStore


def _write_heartbeat(run_dir: Path, phase: str, turn: int) -> None:
    path = run_dir / "heartbeat.json"
    tmp = run_dir / "heartbeat.json.tmp"
    tmp.write_text(json.dumps({"phase": phase, "turn": turn, "pid": os.getpid()}))
    os.replace(tmp, path)


def _service_binder(rt: Any) -> Any:
    """Returns the runtime's bind_services callable, or None — proven a
    DELIBERATE opt-in by VALIDATING the exact call this coordinator makes:
    signature.bind(diary=..., strategy=...) must succeed. Parameter-name
    sniffing is not enough — an extra required kwarg or positional-only
    params would pass a name check and then raise at the call; a
    ``**services`` wrapper is a valid opt-in a name check would skip,
    leaving stale stores across resume. Read-only store properties are
    never assigned behind their back — the hook is the only binding path,
    at construction AND resume."""
    bind = getattr(rt, "bind_services", None)
    if not callable(bind):
        return None
    try:
        inspect.signature(bind).bind(diary=None, strategy=None)
    except (TypeError, ValueError):
        return None
    return bind


def _wire_services(runtimes: dict[int, Any], diary: Any, strategy: Any) -> None:
    for rt in runtimes.values():
        bind = _service_binder(rt)
        if bind is not None:
            bind(diary=diary, strategy=strategy)


class Arena:
    def __init__(self, run_dir: Path, spec: MatchSpec,
                 runtimes: dict[int, Any] | None = None) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "checkpoints").mkdir(exist_ok=True)
        self.spec = spec
        self.game_instance_id = f"{spec.match_id}-i{os.getpid()}"
        self.log = EventLog(self.run_dir / "events.jsonl")
        self.telemetry = TelemetryRegistry()
        self.diary = DiaryStore()
        self.strategy = StrategyStore()
        # cross-match recall corpus (M13): prior runs' lessons, read from
        # their logs — built BEFORE any spend so a bad corpus config fails
        # at startup, not mid-match. Absent recall_runs => tool unavailable.
        self.recall: RecallCorpus | None = None
        if spec.recall_runs:
            self.recall = RecallCorpus.from_runs(
                self.run_dir.parent, spec.match_id, spec.recall_runs)
        self.spend = SpendLedger(self.run_dir / "spend.jsonl")
        self.adapter = SimulatorAdapter()
        self.chaos = ChaosDirector([
            ChaosEvent(MutationSpec(e.spec), hook=e.hook, offset=e.offset)
            for e in spec.chaos
        ])
        self.referee = Referee(
            self.adapter, VisibilityPolicy(), self.log, self.telemetry,
            spec.match_id, self.game_instance_id,
            RefereeConfig(watchdog_mode=spec.watchdog_mode,
                          violation_limit=spec.violation_limit),
            diary=self.diary,
            strategy=self.strategy,
            recall=self.recall,
        )
        self.runtimes: dict[int, Any] = {}
        self.sessions: dict[int, PlayerSession] = {}
        for agent_spec in spec.agents:
            if runtimes is not None and agent_spec.player_id in runtimes:
                self.runtimes[agent_spec.player_id] = runtimes[agent_spec.player_id]
            else:
                profile = AgentProfile(
                    agent_id=agent_spec.agent_id, player_id=agent_spec.player_id,
                    policy=agent_spec.policy, seed=agent_spec.seed,
                    model=agent_spec.model, llm=agent_spec.llm,
                )
                self.runtimes[agent_spec.player_id] = build_runtime(
                    profile, telemetry=self.telemetry, diary=self.diary,
                    strategy=self.strategy,
                    on_post=(self._spend_sink(agent_spec)
                             if agent_spec.policy == "llm" else None))
            self.sessions[agent_spec.player_id] = PlayerSession(
                self.referee, agent_spec.player_id, agent_spec.agent_id)
        # Arena-owned services reach EVERY opted-in runtime, injected ones
        # included: a runtime holding its own store reference would read
        # empty memory after resume.
        _wire_services(self.runtimes, self.diary, self.strategy)
        self.checkpoints = CheckpointManager(
            self.run_dir / "checkpoints", every_n_turns=spec.checkpoint_every)

    # ------------------------------------------------------------------ run
    async def run(
        self,
        resume_state: CheckpointState | None = None,
        crash_after_turn: int | None = None,
    ) -> dict[str, Any]:
        spec = self.spec
        if resume_state is None:
            await self.adapter.setup({"seed": spec.seed, "chaos_director": self.chaos})
            (self.run_dir / "init.json").write_text(json.dumps(
                self.adapter.export_state(), sort_keys=True))
            self.log.write(
                "MATCH_START",
                match_id=spec.match_id, game_instance_id=self.game_instance_id,
                turn=0, phase_player_id=-1, player_id=None, agent_id=None,
                visibility_scope="referee",
                config={
                    "seed": spec.seed, "max_turns": spec.max_turns,
                    "agents": [(a.agent_id, a.player_id, a.policy) for a in spec.agents],
                    "watchdog_mode": spec.watchdog_mode,
                },
                initial_state_hash=self.adapter.state_hash(),
            )
            start_turn = 1
        else:
            await self._resume_from(resume_state)
            start_turn = resume_state.turn + 1

        aborted: str | None = None
        try:
            try:
                for turn in range(start_turn, spec.max_turns + 1):
                    _write_heartbeat(self.run_dir, "turn", turn)
                    for agent_spec in spec.agents:
                        pid = agent_spec.player_id
                        lease = self.referee.grant_lease(pid, agent_spec.agent_id, turn)
                        if (crash_after_turn is not None
                                and turn == crash_after_turn + 1 and pid == 0):
                            # deterministic worst-case crash point: after the
                            # turn-(K+1) lease is granted, before any act
                            self.log.close()
                            os.kill(os.getpid(), signal.SIGKILL)
                        await self.referee.begin_turn(pid, agent_spec.agent_id, turn)
                        # authoritative turn number for runtime-authored prompts
                        # (resume-safe; runtimes without the hook are unaffected)
                        begin = getattr(self.runtimes[pid], "begin_turn", None)
                        if callable(begin):
                            begin(turn)
                        await self.sessions[pid].take_turn(lease, self.runtimes[pid])
                    self.checkpoints.maybe_save(turn, self._checkpoint_state(turn))
                final_turn = spec.max_turns
            finally:
                # release runtime-held resources (the LLM client's connection
                # pool) whether the match completed or aborted
                await self._close_runtimes()
        except MatchAborted as exc:
            aborted = str(exc)
            # Close an open phase THROUGH the referee so cleanup sweeps
            # whatever the phase boundary fires (never bypass the watchdog).
            abort_agent = self.spec.agents[0].agent_id
            if self.adapter.state is not None:
                pid = self.adapter.state.phase_player
                if pid != -1:
                    abort_agent = self.spec.agent_for_player(pid).agent_id
            await self.referee.abort_cleanup(abort_agent)
            final_turn = self.adapter.state.turn if self.adapter.state else 0

        _write_heartbeat(self.run_dir, "match_end", final_turn)
        summary = {
            "match_id": spec.match_id,
            "game_instance_id": self.game_instance_id,
            "final_turn": final_turn,
            "aborted": aborted,
            "violations_total": self.referee.violation_count(),
            "final_state_hash": self.adapter.state_hash() if self.adapter.state else None,
            "telemetry": self.telemetry.snapshot(),
            "scores": self._scores(),
        }
        self.log.write(
            "MATCH_END",
            match_id=spec.match_id, game_instance_id=self.game_instance_id,
            turn=final_turn, phase_player_id=-1, player_id=None, agent_id=None,
            visibility_scope="referee", summary=summary,
        )
        (self.run_dir / "summary.json").write_text(json.dumps(summary, sort_keys=True))
        self.log.close()
        return summary

    def _spend_sink(self, agent_spec: Any) -> Any:
        """Durable per-attempt spend record for one LLM agent."""
        def sink() -> None:
            self.spend.note(agent_spec.agent_id, agent_spec.player_id)
        return sink

    async def _close_runtimes(self) -> None:
        for rt in self.runtimes.values():
            aclose = getattr(rt, "aclose", None)
            if callable(aclose):
                with contextlib.suppress(Exception):
                    await aclose()

    # -------------------------------------------------------------- resume
    def _validate_resume_accounting(self, state: CheckpointState) -> None:
        """Fail-closed accounting validation — runs BEFORE any mutation of
        the run dir (a refusal must never truncate the event log)."""
        if not any(a.policy == "llm" for a in self.spec.agents):
            return
        posts = state.coordinator_state.get("llm_posts")
        if "telemetry" not in state.coordinator_state \
                or not isinstance(posts, dict) or not posts:
            raise ValueError(
                f"checkpoint from turn {state.turn} predates cross-leg "
                "accounting (telemetry/llm_posts) and cannot resume an "
                "llm-policy match — restart the match instead"
            )
        expected = {str(a.player_id) for a in self.spec.agents if a.policy == "llm"}
        if not expected <= set(posts):
            # e.g. an intermediate-version checkpoint keyed by agent_id
            raise ValueError(
                f"checkpoint llm_posts keys {sorted(posts)} do not cover "
                f"llm players {sorted(expected)} — mismatched checkpoint "
                "version; refusing to resume"
            )

    async def _resume_from(self, state: CheckpointState) -> None:
        if state.match_id != self.spec.match_id:
            raise ValueError(
                f"checkpoint is for match {state.match_id!r}, config is "
                f"{self.spec.match_id!r} — refusing to resume"
            )
        # validation before ANY mutation: a refusal must leave the run dir
        # exactly as it was (including a completed run's MATCH_END)
        self._validate_resume_accounting(state)
        # CheckpointState.from_doc already verified the content hash; verify
        # it against THIS config too (the hash covers sim+rng+coordinator,
        # and match identity above covers the config's match).
        await self.adapter.setup({
            "seed": self.spec.seed, "chaos_director": self.chaos,
        })
        self.adapter.import_state(state.sim_doc)
        self.log.truncate_to(state.seq)
        from civ_arena.arena.idempotency import DedupeIndex

        self.referee.dedupe = DedupeIndex.from_log(self.log.records())
        # the diary is derived state: rebuild it from the truncated prefix
        self.diary = DiaryStore.from_log(self.log.records())
        self.referee.diary = self.diary
        # the strategy store is derived the same way (claims by adjacency-
        # paired TOOL_CALLs, beliefs/facts from the observation digests)
        self.strategy = StrategyStore.from_log(self.log.records())
        self.referee.strategy = self.strategy
        # the recall corpus is derived state too: prior logs are immutable
        # so this rebuilds to the identical corpus — uniform with the other
        # derived stores rather than special-cased as "kept"
        self.recall = (RecallCorpus.from_runs(
            self.run_dir.parent, self.spec.match_id, self.spec.recall_runs)
            if self.spec.recall_runs else None)
        self.referee.recall = self.recall
        # the runtimes hold their OWN store references from construction —
        # point the opted-in ones at the rebuilt stores or every resumed
        # prompt reads empty and resumed writes land in a store nobody
        # feeds back. One binding path (the signature-checked hook) for
        # construction and resume alike: no legacy attribute assignment that
        # could hit a read-only property after the log is already truncated.
        _wire_services(self.runtimes, self.diary, self.strategy)
        # cumulative accounting across legs (spend budget, tokens)
        telemetry_doc = state.coordinator_state.get("telemetry")
        if telemetry_doc:
            self.telemetry.merge_snapshot(telemetry_doc)
        posts = state.coordinator_state.get("llm_posts") or {}
        spend_counts = self.spend.counts()
        for pid, rt in self.runtimes.items():
            client = getattr(rt, "client", None)
            if getattr(client, "posts_sent", None) is not None:
                # monotonic over three sources: the live counter, the
                # checkpoint, and the durable spend ledger (which survives
                # the post-checkpoint crash window the checkpoint cannot)
                client.posts_sent = max(
                    client.posts_sent,
                    int(posts.get(str(pid), 0)),
                    spend_counts.get(str(pid), 0),
                )
        # control-plane counters and chaos schedule must resume, not reset
        self.referee.restore_violation_counters(
            state.coordinator_state.get("violations", 0),
            state.coordinator_state.get("violations_by_agent"),
        )
        chaos_doc = state.coordinator_state.get("chaos")
        if chaos_doc is not None:
            self.chaos.restore(chaos_doc)
        # restore agent rngs
        for agent_spec in self.spec.agents:
            doc = state.rng_states.get(f"agent-{agent_spec.player_id}")
            runtime = self.runtimes[agent_spec.player_id]
            if doc is not None:
                from civ_arena.agents.runtime import load_rng

                load_rng(runtime, doc)
        if state.rng_states.get("sim") is not None:
            from civ_arena.canonical import rng_from_doc

            self.adapter.state.rng = rng_from_doc(state.rng_states["sim"])

    def _llm_posts(self) -> dict[str, int]:
        """Spend counters keyed by PLAYER id — agent ids need not be unique
        across players, but player ids are (config-enforced)."""
        posts: dict[str, int] = {}
        for pid, rt in self.runtimes.items():
            client = getattr(rt, "client", None)
            count = getattr(client, "posts_sent", None)
            if isinstance(count, int):
                posts[str(pid)] = count
        return posts

    def _checkpoint_state(self, turn: int) -> CheckpointState:
        rng_states = {
            "sim": rng_to_doc(self.adapter.state.rng),
            **{
                f"agent-{pid}": rng_to_doc(rt.rng)
                for pid, rt in self.runtimes.items()
            },
        }
        return CheckpointState(
            match_id=self.spec.match_id,
            game_instance_id_of_origin=self.game_instance_id,
            turn=turn,
            seq=len(self.log),
            sim_doc=self.adapter.export_state(),
            rng_states=rng_states,
            coordinator_state={
                "violations": self.referee.violation_count(),
                "violations_by_agent": dict(self.referee.violations_by_agent),
                "chaos": self.chaos.state_doc(),
                # cumulative across legs so a resumed match keeps the whole
                # picture: spend budget and token accounting never reset.
                # total_ms is STRIPPED: durations are wall-clock envelope
                # data (same class as duration_ms) and would break the
                # same-seed-same-checkpoint determinism contract
                "telemetry": {
                    aid: {k: v for k, v in doc.items() if k != "total_ms"}
                    for aid, doc in self.telemetry.snapshot().items()
                },
                "llm_posts": self._llm_posts(),
            },
            log_prefix_sha256=log_prefix_hash(self.log.records()),
        )

    def _scores(self) -> dict[str, Any]:
        if self.adapter.state is None:
            return {}
        state = self.adapter.state
        scores = {}
        for pid_str, player in state.players.items():
            cities = [c for c in state.cities.values() if c["owner"] == int(pid_str)]
            scores[player["civ_name"]] = {
                "player_id": int(pid_str),
                "cities": len(cities),
                "population": sum(c["population"] for c in cities),
                "gold": player["gold"],
                "techs": len(player["researched"]),
                "units": len([u for u in state.units.values()
                              if u["owner"] == int(pid_str)]),
            }
        return scores

    def checkpoint_hash_now(self) -> str:
        ckpt = self._checkpoint_state(self.adapter.state.turn)
        return checkpoint_hash(ckpt.sim_doc, ckpt.rng_states, ckpt.coordinator_state)

    def state_hash_now(self) -> str:
        return state_hash(self.adapter.export_state())
