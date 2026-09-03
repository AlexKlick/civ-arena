"""Production V2 match coordinator.

This coordinator owns lifecycle only. Policies receive ``TurnContextV2`` and
return untrusted proposals; ``TransactionalExecutorV2`` is the sole engine
mutation caller. Private watchdog snapshots/mutation journals never enter an
event, receipt, summary file, policy context, or error message.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Any

from civ_arena.agents.runtime import AgentProfile
from civ_arena.arena.diary import DiaryStore
from civ_arena.arena.referee import MatchAborted
from civ_arena.arena.telemetry import TelemetryRegistry
from civ_arena.arena.watchdog import diff as watchdog_diff
from civ_arena.config import MatchSpec
from civ_arena.game.sim.chaos import ChaosDirector, ChaosEvent, MutationSpec
from civ_arena.recall import RecallCorpus
from civ_arena.strategy.store import StrategyStore
from civ_arena.v2.contracts import (
    AdapterKindV2,
    ChaosEventConfigV2,
    ChaosHookV2,
    ChaosSpecV2,
    ComputeConfigV2,
    EpisodeConfigV2,
    EpisodeReceiptV2,
    EpisodeTerminationV2,
    EventTypeV2,
    ExecutionModeV2,
    TurnContextV2,
    TurnReceiptV2,
    TurnTerminationV2,
    WatchdogModeV2,
)
from civ_arena.v2.enumeration import ActionEnumeratorV2
from civ_arena.v2.environment import (
    ObservableExecutionFacetV2,
    PrivateRefereeMonitorV2,
    simulator_facets_v2,
)
from civ_arena.v2.executor import TransactionalExecutorV2
from civ_arena.v2.graph import ActionGraphCompilerV2
from civ_arena.v2.ledger import EpisodeRecorderV2
from civ_arena.v2.policy import (
    AgentRuntimeV2,
    PolicyServicesV2,
    SystemHousekeepingRuntimeV2,
    build_policy_runtime_v2,
    restore_policy_history_v2,
    snapshot_policy_state_v2,
)
from civ_arena.v2.replay import replay_fake_episode_v2
from civ_arena.v2.resume import ResumePlanV2, inspect_resume_parent_v2
from civ_arena.v2.schemas import ContractError


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, MatchAborted):
        message = "policy runtime aborted"
    elif isinstance(exc, TimeoutError):
        message = "match timed out"
    else:
        message = f"{type(exc).__name__} during V2 match"
    return " ".join(message.split())[:160]


class ArenaV2:
    """Run a schema-2 match through the authoritative DAG+TX path."""

    def __init__(
        self,
        run_dir: Path | str,
        spec: MatchSpec,
        *,
        runtimes: dict[int, Any] | None = None,
        environment: ObservableExecutionFacetV2 | None = None,
        private_monitor: PrivateRefereeMonitorV2 | None = None,
        recall_root: Path | None = None,
        episode_id: str | None = None,
        parent_episode_id: str | None = None,
        parent_terminal_event_hash: str | None = None,
        parent_episode_dir: Path | str | None = None,
        system_runtime: SystemHousekeepingRuntimeV2 | None = None,
    ) -> None:
        if spec.schema != 2:
            raise ContractError("ArenaV2 requires a schema-2 MatchSpec")
        if spec.execution_mode != ExecutionModeV2.DAG_TX.value:
            raise ContractError("normal V2 matches require execution_mode dag_tx")
        if spec.scored and (parent_episode_id is not None or parent_episode_dir is not None):
            raise ContractError("scored V2 matches cannot resume")
        if (parent_episode_id is None) != (parent_terminal_event_hash is None):
            raise ContractError("resume parent id and terminal hash must appear together")
        if parent_episode_dir is not None and parent_episode_id is not None:
            raise ContractError("resume parent directory replaces explicit parent metadata")

        self.run_dir = Path(run_dir)
        self.spec = spec
        self.episode_id = episode_id or spec.match_id
        self.parent_episode_id = parent_episode_id
        self.parent_terminal_event_hash = parent_terminal_event_hash
        self.system_runtime = system_runtime
        self.recall_root = Path(recall_root) if recall_root else self.run_dir.parent
        self.telemetry = TelemetryRegistry()
        self.diary = DiaryStore()
        self.strategy = StrategyStore()
        self.recall = (
            RecallCorpus.from_runs(self.recall_root, spec.match_id, spec.recall_runs)
            if spec.recall_runs
            else None
        )
        if environment is None or private_monitor is None:
            if spec.adapter != "simulator":
                raise ContractError(
                    "live V2 matches require handshake-bound environment facets"
                )
            environment, private_monitor = simulator_facets_v2()
        self.environment = environment
        self.private_monitor = private_monitor
        self.episode_config = EpisodeConfigV2.create(
            watchdog_mode=WatchdogModeV2(spec.watchdog_mode),
            violation_limit=spec.violation_limit,
            chaos=[
                ChaosEventConfigV2(
                    ChaosSpecV2(item.spec),
                    ChaosHookV2(item.hook),
                    item.offset,
                )
                for item in spec.chaos
            ],
        )
        self.chaos = ChaosDirector(
            [
                ChaosEvent(
                    MutationSpec(item.spec.value),
                    hook=item.hook.value,
                    offset=item.offset,
                )
                for item in self.episode_config.chaos
            ]
        )

        injected = runtimes or {}
        self.services: dict[int, PolicyServicesV2] = {}
        self.runtimes: dict[int, AgentRuntimeV2] = {}
        for agent in spec.agents:
            services = PolicyServicesV2(
                agent_id=agent.agent_id,
                player_id=agent.player_id,
                diary=self.diary,
                strategy=self.strategy,
                recall=self.recall,
                telemetry=self.telemetry,
            )
            self.services[agent.player_id] = services
            candidate = injected.get(agent.player_id)
            if candidate is not None and isinstance(candidate, AgentRuntimeV2):
                runtime = candidate
            else:
                profile = AgentProfile(
                    agent_id=agent.agent_id,
                    player_id=agent.player_id,
                    policy=agent.policy,
                    seed=agent.seed,
                    model=agent.model,
                    llm=agent.llm,
                    proposer=agent.proposer,
                    case_base=agent.case_base,
                )
                runtime = build_policy_runtime_v2(
                    profile,
                    services=services,
                    runtime=candidate,
                )
            self.runtimes[agent.player_id] = runtime
        descriptors = [runtime.descriptor for runtime in self.runtimes.values()]
        if self.system_runtime is not None:
            descriptors.append(self.system_runtime.descriptor)
        if len({item.descriptor_id for item in descriptors}) != len(descriptors):
            raise ContractError("V2 policy descriptors must be unique per seat")

        self.compute = ComputeConfigV2(
            ExecutionModeV2.DAG_TX,
            spec.max_graph_actions,
            spec.max_replans_per_turn,
        )
        self._receipts: list[TurnReceiptV2] = []
        self._violations_total = 0
        self.resume_plan: ResumePlanV2 | None = None
        if parent_episode_dir is not None:
            if spec.adapter != "simulator":
                raise ContractError(
                    "live V2 child resume requires a handshake-bound save-load implementation"
                )
            self.resume_plan = inspect_resume_parent_v2(
                parent_episode_dir,
                environment_id=self.environment.descriptor.descriptor_id,
                policies_by_player={
                    player_id: runtime.descriptor
                    for player_id, runtime in self.runtimes.items()
                },
                seed=spec.seed,
                compute=self.compute,
                episode_config=self.episode_config,
            )
            self.parent_episode_id = self.resume_plan.parent_episode_id
            self.parent_terminal_event_hash = (
                self.resume_plan.parent_terminal_event_hash
            )
            if episode_id is None:
                self.episode_id = (
                    f"{spec.match_id}-child-"
                    f"{self.resume_plan.parent_terminal_event_hash[:12]}"
                )

    async def _close_runtimes(self) -> None:
        for runtime in self.runtimes.values():
            close = getattr(runtime, "aclose", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    await close()

    def _watchdog_sweep(self, snapshot: Any) -> bool:
        actual = self.private_monitor.drain_mutations()
        allowed = self.private_monitor.drain_authorized_mutations()
        violations = watchdog_diff(actual, allowed)
        if not violations:
            return True
        self._violations_total += len(violations)
        if self.episode_config.watchdog_mode is WatchdogModeV2.ROLLBACK:
            self.private_monitor.restore(snapshot)
            return False
        return self._violations_total <= self.episode_config.violation_limit

    async def _run_phase(
        self,
        recorder: EpisodeRecorderV2,
        *,
        turn: int,
        player_id: int,
    ) -> tuple[bool, str | None]:
        snapshot = (
            self.private_monitor.snapshot()
            if self.episode_config.watchdog_mode is WatchdogModeV2.ROLLBACK
            else None
        )
        observation = await self.environment.begin_turn(player_id, turn)
        if self.system_runtime is not None:
            for _ in range(32):
                legal = ActionEnumeratorV2(self.spec.max_graph_actions).enumerate(
                    observation
                )
                graph = ActionGraphCompilerV2(self.spec.max_graph_actions).compile(
                    observation, legal
                )
                system_proposal = await self.system_runtime.propose_turn(
                    TurnContextV2(
                        observation,
                        legal,
                        graph,
                        self.system_runtime.descriptor,
                    )
                )
                if not system_proposal.intents:
                    break
                system_receipt = await TransactionalExecutorV2(
                    self.environment,
                    self.system_runtime.descriptor,
                    episode_id=self.episode_id,
                    max_graph_actions=self.spec.max_graph_actions,
                    max_replans_per_turn=self.spec.max_replans_per_turn,
                    recorder=recorder,
                ).execute(system_proposal, observation, graph)
                self._receipts.append(system_receipt)
                if system_receipt.termination is not TurnTerminationV2.SYSTEM_HANDOFF:
                    self._watchdog_sweep(snapshot)
                    return False, system_receipt.safe_error or "system housekeeping failed"
                completed_kind = system_proposal.intents[0].action_kind
                observation = await self.environment.observe(player_id)
                if completed_kind in observation.mandatory_action_kinds:
                    self._watchdog_sweep(snapshot)
                    return False, "system housekeeping made no observable progress"
            else:
                self._watchdog_sweep(snapshot)
                return False, "system housekeeping exceeded its action bound"
        runtime = self.runtimes[player_id]
        executor = TransactionalExecutorV2(
            self.environment,
            runtime.descriptor,
            episode_id=self.episode_id,
            max_graph_actions=self.spec.max_graph_actions,
            max_replans_per_turn=self.spec.max_replans_per_turn,
            recorder=recorder,
        )
        policy_replans = 0
        while True:
            legal = ActionEnumeratorV2(self.spec.max_graph_actions).enumerate(observation)
            graph = ActionGraphCompilerV2(self.spec.max_graph_actions).compile(
                observation, legal
            )
            try:
                proposal = await runtime.propose_turn(
                    TurnContextV2(observation, legal, graph, runtime.descriptor)
                )
            except BaseException:
                state = snapshot_policy_state_v2(
                    runtime,
                    self.services[player_id],
                    turn=turn,
                    proposal_id=None,
                )
                recorder.record_reference(
                    EventTypeV2.POLICY_STATE_RECORDED,
                    state,
                    turn_id=turn,
                    correlation_id=f"turn-{turn}-p{player_id}",
                )
                self.services[player_id].mark_operations_durable()
                raise
            receipt = await executor.execute(proposal, observation, graph)
            self._receipts.append(receipt)
            state = snapshot_policy_state_v2(
                runtime,
                self.services[player_id],
                turn=turn,
                proposal_id=proposal.proposal_id,
            )
            recorder.record_reference(
                EventTypeV2.POLICY_STATE_RECORDED,
                state,
                turn_id=turn,
                correlation_id=f"turn-{turn}-p{player_id}",
            )
            self.services[player_id].mark_operations_durable()
            if receipt.termination is TurnTerminationV2.COMPLETED:
                clean = self._watchdog_sweep(snapshot)
                if not clean:
                    return False, "private watchdog rejected the completed phase"
                return True, None
            if (
                receipt.termination
                in {
                    TurnTerminationV2.MANDATORY_UNRESOLVED,
                    TurnTerminationV2.REPLANS_EXHAUSTED,
                }
                and policy_replans < self.spec.max_replans_per_turn
            ):
                policy_replans += 1
                observation = await self.environment.observe(player_id)
                continue
            self._watchdog_sweep(snapshot)
            return False, receipt.safe_error or "turn execution failed"

    async def _run_schedule(
        self,
        phases: AsyncIterable[tuple[int, int]],
        *,
        expected_phases: int,
        reset_environment: bool,
        recovered_crash_after_phases: int | None = None,
    ) -> dict[str, Any]:
        """Run one already-authorized phase schedule under a single V2 ledger."""

        if self.parent_episode_id is not None and self.resume_plan is None:
            raise ContractError(
                "running a V2 child requires the read-only parent episode directory"
            )

        if self.resume_plan is not None:
            await replay_fake_episode_v2(
                self.resume_plan.parent_episode_dir,
                expected_environment_id=self.environment.descriptor.descriptor_id,
                environment=self.environment,
                private_monitor=self.private_monitor,
                through_sequence=self.resume_plan.checkpoint_sequence,
            )
            for player_id, runtime in self.runtimes.items():
                restore_policy_history_v2(
                    runtime,
                    self.services[player_id],
                    self.resume_plan.states_for(player_id),
                    spend_attempts=self.resume_plan.spends_for(player_id),
                )
            # Parent replay is an internal reconstruction step. Its mutation
            # journals must not contaminate the first child watchdog sweep.
            self.private_monitor.drain_mutations()
            self.private_monitor.drain_authorized_mutations()

        policies = [runtime.descriptor for runtime in self.runtimes.values()]
        if self.system_runtime is not None:
            policies.append(self.system_runtime.descriptor)
        recorder = EpisodeRecorderV2(
            self.run_dir,
            episode_id=self.episode_id,
            environment=self.environment.descriptor,
            policies=policies,
            seed=self.spec.seed if self.spec.adapter == "simulator" else None,
            compute=self.compute,
            scored=self.spec.scored,
            parent_episode_id=self.parent_episode_id,
            parent_terminal_event_hash=self.parent_terminal_event_hash,
        )
        recorder.record_reference(
            EventTypeV2.EPISODE_CONFIG_RECORDED,
            self.episode_config,
            turn_id=None,
            correlation_id=self.episode_id,
        )
        termination = EpisodeTerminationV2.FAILURE
        aborted: str | None = None
        phases_completed = 0
        final_turn = 0
        terminal_receipt: EpisodeReceiptV2 | None = None
        try:
            for player_id, service in self.services.items():
                service.bind_custody(
                    self.runtimes[player_id].descriptor.descriptor_id,
                    recorder,
                )
            if reset_environment and self.resume_plan is None:
                await self.environment.reset(
                    {"seed": self.spec.seed, "chaos_director": self.chaos}
                )
            stopped = False
            async for turn, player_id in phases:
                if player_id not in self.runtimes:
                    raise ContractError("phase schedule names an unregistered player")
                final_turn = turn
                completed, reason = await self._run_phase(
                    recorder,
                    turn=turn,
                    player_id=player_id,
                )
                if not completed:
                    aborted = reason
                    stopped = True
                    break
                phases_completed += 1
                if (
                    recovered_crash_after_phases is not None
                    and phases_completed >= recovered_crash_after_phases
                ):
                    termination = EpisodeTerminationV2.RECOVERED_CRASH
                    aborted = "crash recovered at a completed phase boundary"
                    stopped = True
                    break
            if not stopped and phases_completed == expected_phases:
                termination = EpisodeTerminationV2.SUCCESS
            elif not stopped:
                aborted = "phase schedule ended before its declared horizon"
        except asyncio.CancelledError:
            termination = EpisodeTerminationV2.CANCELLED
            aborted = "match cancelled"
            raise
        except (KeyboardInterrupt, SystemExit):
            termination = EpisodeTerminationV2.CANCELLED
            aborted = "match cancelled"
            raise
        except TimeoutError as exc:
            termination = EpisodeTerminationV2.TIMEOUT
            aborted = _safe_error(exc)
        except Exception as exc:
            termination = EpisodeTerminationV2.FAILURE
            aborted = _safe_error(exc)
        finally:
            for service in self.services.values():
                service.unbind_custody()
            await self._close_runtimes()
            if terminal_receipt is None:
                terminal_receipt = recorder.terminate(
                    termination,
                    turns_completed=phases_completed,
                )
            recorder.close()

        assert terminal_receipt is not None
        return {
            "schema": 2,
            "match_id": self.spec.match_id,
            "episode_id": self.episode_id,
            "final_turn": final_turn,
            "phases_completed": phases_completed,
            "termination_reason": terminal_receipt.termination_reason.value,
            "aborted": aborted,
            "violations_total": self._violations_total,
            "terminal_event_hash": terminal_receipt.terminal_event_hash,
            "parent_episode_id": terminal_receipt.parent_episode_id,
            "parent_terminal_event_hash": terminal_receipt.parent_terminal_event_hash,
            "parent_checkpoint_event_hash": (
                self.resume_plan.checkpoint_event_hash
                if self.resume_plan is not None
                else None
            ),
            "environment_id": self.environment.descriptor.descriptor_id,
            "turn_receipts": [item.receipt_id for item in self._receipts],
            "telemetry": self.telemetry.snapshot(),
            "model_posts": {
                str(player_id): service.model_posts
                for player_id, service in sorted(self.services.items())
            },
        }

    async def run(self, *, recover_after_turn: int | None = None) -> dict[str, Any]:
        """Run and terminally receipt a normal deterministic match."""

        agents = self.spec.agents
        if recover_after_turn is not None:
            if type(recover_after_turn) is not int or not (
                1 <= recover_after_turn <= self.spec.max_turns
            ):
                raise ContractError(
                    "recover_after_turn must be within the configured horizon"
                )
            if self.spec.scored:
                raise ContractError("scored V2 matches cannot inject a recovered crash")
            if self.resume_plan is not None or self.parent_episode_id is not None:
                raise ContractError("a V2 child cannot inject another recovered crash")
        phase_start = (
            self.resume_plan.completed_phases if self.resume_plan is not None else 0
        )
        phase_stop = self.spec.max_turns * len(agents)

        async def phases() -> AsyncIterable[tuple[int, int]]:
            for phase_index in range(phase_start, phase_stop):
                turn = phase_index // len(agents) + 1
                yield turn, agents[phase_index % len(agents)].player_id

        return await self._run_schedule(
            phases(),
            expected_phases=phase_stop - phase_start,
            reset_environment=True,
            recovered_crash_after_phases=(
                recover_after_turn * len(agents)
                if recover_after_turn is not None
                else None
            ),
        )

    async def run_prepared(
        self,
        phases: AsyncIterable[tuple[int, int]],
        *,
        expected_phases: int,
    ) -> dict[str, Any]:
        """Run a handshake-prepared live phase schedule without resetting it."""

        if self.environment.descriptor.adapter_kind is not AdapterKindV2.FIRETUNER:
            raise ContractError("prepared schedules are reserved for FireTuner")
        if self.resume_plan is not None or self.parent_episode_id is not None:
            raise ContractError("live prepared schedules cannot resume")
        if expected_phases < 1:
            raise ContractError("prepared schedule horizon must be positive")
        return await self._run_schedule(
            phases,
            expected_phases=expected_phases,
            reset_environment=False,
        )
