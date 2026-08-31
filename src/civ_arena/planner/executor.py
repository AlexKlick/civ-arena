"""M15c — the guarded plan executor: canonical order, prevalidate, cascade.

``execute_plan`` runs one turn's action plan through the tool facade in
the DAG's canonical order. Before each referee call it prevalidates the
action against the ROLLING belief state (the M15b reconstruction advanced
by ``apply_action`` for every accepted call): an action the belief already
knows is stale costs no referee call. The referee stays the sole
authority — a rejection there cascades: every MUST_PRECEDE dependent is
skipped transitively, commuting actions continue.

The belief roll-forward is a declared approximation: combat resolves with
the BELIEF's rng, so predicted damage can differ from the real engine's.
Prevalidation only needs legality-shaped state; divergence is caught by
the referee (a wrongly-issued action is rejected, a wrongly-skipped one
costs a turn of tempo, never a violation).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from civ_arena.game.sim.rules import apply_action, check_action
from civ_arena.game.sim.state import SimState
from civ_arena.planner.action_dag import build_dag, canonical_order


@dataclass
class ExecutionReport:
    executed: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    rejected: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)
    skipped: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)


async def execute_plan(
    facade: Any,
    belief_state: SimState,
    player_id: int,
    actions: list[tuple[str, dict[str, Any]]],
) -> ExecutionReport:
    dag = build_dag(actions)
    order = canonical_order(dag)
    report = ExecutionReport()
    dead: set[int] = set()  # rejected/skipped indices; dependents cascade

    def _cascade(idx: int) -> None:
        for dep in sorted(dag.dependents.get(idx, set())):
            if dep in dead:
                continue
            dead.add(dep)
            tool, args = dag.actions[dep]
            report.skipped.append((tool, args, f"upstream action {idx} failed"))
            _cascade(dep)

    for idx in order:
        if idx in dead:
            continue
        tool, args = dag.actions[idx]
        reason = check_action(belief_state, player_id, tool, args)
        if reason is not None:
            dead.add(idx)
            report.skipped.append((tool, args, f"belief prevalidation: {reason.value}"))
            _cascade(idx)
            continue
        result = await getattr(facade, tool)(**args)
        if result.get("status") == "accepted":
            apply_action(belief_state, player_id, tool, args)
            report.executed.append((tool, args))
        else:
            dead.add(idx)
            report.rejected.append((tool, args, str(result.get("rejection"))))
            _cascade(idx)
    return report
