"""Production V2 match coordinator.

This coordinator owns lifecycle only. Policies receive ``TurnContextV2`` and
return untrusted proposals; ``TransactionalExecutorV2`` is the sole engine
mutation caller. Private watchdog snapshots/mutation journals never enter an
event, receipt, summary file, policy context, or error message.
"""

from __future__ import annotations

import asyncio
import contextlib
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
    ComputeConfigV2,
    EpisodeReceiptV2,
    EpisodeTerminationV2,
    ExecutionModeV2,
    TurnContextV2,
    TurnReceiptV2,
    TurnTerminationV2,
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
    build_policy_runtime_v2,
)
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
    ) -> None:
        if spec.schema != 2:
            raise ContractError("ArenaV2 requires a schema-2 MatchSpec")
        if spec.execution_mode != ExecutionModeV2.DAG_TX.value:
            raise ContractError("normal V2 matches require execution_mode dag_tx")
        if spec.scored and parent_episode_id is not None:
            raise ContractError("scored V2 matches cannot resume")
        if (parent_episode_id is None) != (parent_terminal_event_hash is None):
            raise ContractError("resume parent id and terminal hash must appear together")

        self.run_dir = Path(run_dir)
        self.spec = spec
        self.episode_id = episode_id or spec.match_id
        self.parent_episode_id = parent_episode_id
        self.parent_terminal_event_hash = parent_terminal_event_hash
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
        self.chaos = ChaosDirector(
            [
                ChaosEvent(MutationSpec(item.spec), hook=item.hook, offset=item.offset)
                for item in spec.chaos
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
        if len({item.descriptor_id for item in descriptors}) != len(descriptors):
            raise ContractError("V2 policy descriptors must be unique per seat")

        self.compute = ComputeConfigV2(
            ExecutionModeV2.DAG_TX,
            spec.max_graph_actions,
            spec.max_replans_per_turn,
        )
        self._receipts: list[TurnReceiptV2] = []
        self._violations_total = 0

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
        if self.spec.watchdog_mode == "rollback":
            self.private_monitor.restore(snapshot)
            return False
        return self._violations_total <= self.spec.violation_limit

    async def _run_phase(
        self,
        recorder: EpisodeRecorderV2,
        *,
        turn: int,
        player_id: int,
    ) -> tuple[bool, str | None]:
        snapshot = self.private_monitor.snapshot()
        observation = await self.environment.begin_turn(player_id, turn)
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
            proposal = await runtime.propose_turn(
                TurnContextV2(observation, legal, graph, runtime.descriptor)
            )
            receipt = await executor.execute(proposal, observation, graph)
            self._receipts.append(receipt)
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

    async def run(self) -> dict[str, Any]:
        """Run and terminally receipt every outcome, including cancellation."""

        policies = [runtime.descriptor for runtime in self.runtimes.values()]
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
        termination = EpisodeTerminationV2.FAILURE
        aborted: str | None = None
        phases_completed = 0
        final_turn = 0
        terminal_receipt: EpisodeReceiptV2 | None = None
        try:
            await self.environment.reset(
                {"seed": self.spec.seed, "chaos_director": self.chaos}
            )
            stopped = False
            for turn in range(1, self.spec.max_turns + 1):
                final_turn = turn
                for agent in self.spec.agents:
                    completed, reason = await self._run_phase(
                        recorder,
                        turn=turn,
                        player_id=agent.player_id,
                    )
                    if not completed:
                        aborted = reason
                        stopped = True
                        break
                    phases_completed += 1
                if stopped:
                    break
            if not stopped:
                termination = EpisodeTerminationV2.SUCCESS
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
            "environment_id": self.environment.descriptor.descriptor_id,
            "turn_receipts": [item.receipt_id for item in self._receipts],
            "telemetry": self.telemetry.snapshot(),
            "model_posts": {
                str(player_id): service.model_posts
                for player_id, service in sorted(self.services.items())
            },
        }
