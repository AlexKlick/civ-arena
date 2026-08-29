"""Arena: owns the match loop, the adapter, the agents, checkpoints, heartbeat."""

from __future__ import annotations

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
from civ_arena.arena.telemetry import TelemetryRegistry
from civ_arena.arena.visibility import VisibilityPolicy
from civ_arena.canonical import checkpoint_hash, log_prefix_hash, rng_to_doc, state_hash
from civ_arena.config import MatchSpec
from civ_arena.game.sim.chaos import ChaosDirector, ChaosEvent, MutationSpec
from civ_arena.game.sim.simulator import SimulatorAdapter
from civ_arena.session.player_session import PlayerSession


def _write_heartbeat(run_dir: Path, phase: str, turn: int) -> None:
    path = run_dir / "heartbeat.json"
    tmp = run_dir / "heartbeat.json.tmp"
    tmp.write_text(json.dumps({"phase": phase, "turn": turn, "pid": os.getpid()}))
    os.replace(tmp, path)


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
                    profile, telemetry=self.telemetry, diary=self.diary)
            self.sessions[agent_spec.player_id] = PlayerSession(
                self.referee, agent_spec.player_id, agent_spec.agent_id)
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
                    await self.sessions[pid].take_turn(lease, self.runtimes[pid])
                self.checkpoints.maybe_save(turn, self._checkpoint_state(turn))
            final_turn = spec.max_turns
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

    # -------------------------------------------------------------- resume
    async def _resume_from(self, state: CheckpointState) -> None:
        if state.match_id != self.spec.match_id:
            raise ValueError(
                f"checkpoint is for match {state.match_id!r}, config is "
                f"{self.spec.match_id!r} — refusing to resume"
            )
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
