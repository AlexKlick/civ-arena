"""M15c — the guarded plan executor: canonical order, prevalidate,
reconcile, cascade.

``execute_plan`` runs one turn's action plan through the tool facade in
the DAG's canonical order, keeping a ROLLING belief state:

- every action is prevalidated with ``check_action`` against the rolling
  state — a stale action costs no referee call;
- deterministic own-state actions (move/fortify/found/research/
  production) roll the state forward with ``apply_action`` — their
  predicted effect is exact;
- truth-divergent actions (``attack``: engine rng resolves damage;
  ``purchase``: the true spawned unit id depends on hidden id
  consumption) trigger a RECONCILIATION instead: re-observe through the
  facade, fold the observation into the belief, and rebuild the rolling
  state. During the holder's lease the opponent is frozen, so an attacked
  target absent from the fresh observation DIED — its last-seen belief
  entry is retired (the one exception to no-expiry, and it is derived
  from an in-lease observation, not from ground truth);
- a referee rejection (or prevalidation skip) cascades to ``depends``
  dependents only; ``order`` edges constrain sequence, never survival.

The referee stays the sole authority; the executor's job is that
staleness is absorbed before it becomes referee spend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from civ_arena.game.sim.rules import apply_action, check_action
from civ_arena.game.sim.state import SimState
from civ_arena.planner.action_dag import build_dag, canonical_order
from civ_arena.planner.belief import PlannerBelief, build_state_doc

DETERMINISTIC_TOOLS = frozenset(
    {"move_unit", "fortify", "found_city", "set_research", "set_city_production"})


@dataclass
class ExecutionReport:
    executed: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    rejected: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)
    skipped: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)


async def execute_plan(
    facade: Any,
    belief: PlannerBelief,
    player_id: int,
    actions: list[tuple[str, dict[str, Any]]],
    *,
    seed: int,
) -> ExecutionReport:
    dag = build_dag(actions)
    order = canonical_order(dag)
    report = ExecutionReport()
    dead: set[int] = set()  # rejected/skipped indices; depends-dependents cascade
    state = SimState.from_doc(build_state_doc(belief, seed))

    def _cascade(idx: int) -> None:
        for dep in sorted(dag.hard_dependents.get(idx, set())):
            if dep in dead:
                continue
            dead.add(dep)
            tool, args = dag.actions[dep]
            report.skipped.append((tool, args, f"upstream action {idx} failed"))
            _cascade(dep)

    async def _reconcile(tool: str, args: dict[str, Any]) -> SimState:
        docs = await facade.get_units()
        belief.observe_units(docs, belief.turn)
        if tool == "purchase":
            belief.observe_cities(await facade.get_cities(), belief.turn)
            belief.observe_overview(await facade.get_overview())  # gold moved
        if tool == "attack":
            target = args.get("target_id")
            # frozen-opponent lease: the attacked target missing from a
            # fresh in-lease observation means it DIED, not that it moved —
            # retire its last-seen entry (derived from an in-lease
            # observation, never from ground truth)
            if target and all(d["unit_id"] != target for d in docs):
                belief.foreign_units.pop(target, None)
        return SimState.from_doc(build_state_doc(belief, seed))

    for idx in order:
        if idx in dead:
            continue
        tool, args = dag.actions[idx]
        reason = check_action(state, player_id, tool, args)
        if reason is not None:
            dead.add(idx)
            report.skipped.append((tool, args, f"belief prevalidation: {reason.value}"))
            _cascade(idx)
            continue
        result = await getattr(facade, tool)(**args)
        if result.get("status") == "accepted":
            report.executed.append((tool, args))
            if tool in DETERMINISTIC_TOOLS:
                apply_action(state, player_id, tool, args)
            else:
                state = await _reconcile(tool, args)
        else:
            dead.add(idx)
            report.rejected.append((tool, args, str(result.get("rejection"))))
            _cascade(idx)
    return report
