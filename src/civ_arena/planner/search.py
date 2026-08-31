"""M15d — option-level search: MCTS baseline vs transposition-aware MCGS.

Both searchers share everything except the DEPTH-2 NODE KEY:

- ``mcts``: children are keyed by the PATH (the depth-1 option id) — the
  classic tree; convergent worlds are re-estimated separately per path.
- ``mcgs``: children are keyed by the EXACT integer abstraction of the
  reached state (``abstract_key``) — option sequences that converge to
  the same strategic situation SHARE visit counts and value estimates
  (the transposition reuse H3 tests at equal budget).

A rollout: determinize the belief (fresh seed per rollout), simulate
epoch 1 under the depth-1 option, select a depth-2 option by UCT at the
reached node, simulate epoch 2, score with ``value_of``. Simulation
executes compiled option steps directly on the sim rules (check, then
apply; a step that fails checks is dropped — the M15c executor's
prevalidation, without a facade) and runs the built-in ``stock_ai_turn``
for the opponent. Everything is deterministic in (belief, seed, budget);
floats exist only inside UCT arithmetic, never in any artifact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from civ_arena.canonical import canonical
from civ_arena.game.sim.engine import run_ambient, stock_ai_turn
from civ_arena.game.sim.rules import apply_action, check_action
from civ_arena.game.sim.state import SimState
from civ_arena.game.sim.value import value_of
from civ_arena.planner.belief import PlannerBelief, build_state_doc
from civ_arena.planner.options import OPTIONS, Option

UCT_C = 140.0  # exploration constant, on the value_of scale


@dataclass
class SearchResult:
    method: str
    chosen: str
    root_visits: dict[str, int]
    root_values: dict[str, int]
    rollouts: int
    nodes_expanded: int
    unique_states: int
    transposition_hits: int

    def to_doc(self) -> dict[str, Any]:
        return {
            "method": self.method, "chosen": self.chosen,
            "root_visits": dict(sorted(self.root_visits.items())),
            "root_values": dict(sorted(self.root_values.items())),
            "rollouts": self.rollouts, "nodes_expanded": self.nodes_expanded,
            "unique_states": self.unique_states,
            "transposition_hits": self.transposition_hits,
        }


def abstract_key(state: SimState, pid: int) -> str:
    """Exact integer strategic abstraction — the MCGS equivalence key.

    Coarse on purpose (gold in buckets of 25, unit counts by type, tech
    SETS): convergent option paths merge; anything finer would make every
    node unique and the graph degenerate to the tree.
    """
    doc: dict[str, Any] = {"turn": state.turn}
    for spid in sorted(state.players):
        p = state.players[spid]
        units: dict[str, int] = {}
        for u in state.units.values():
            if u["owner"] == int(spid):
                units[u["type"]] = units.get(u["type"], 0) + 1
        cities = [c for c in state.cities.values() if c["owner"] == int(spid)]
        doc[spid] = {
            "cities": len(cities),
            "population": sum(c["population"] for c in cities),
            "gold_bucket": p["gold"] // 25,
            "researched": sorted(p["researched"]),
            "units": dict(sorted(units.items())),
        }
    _ = pid
    return canonical(doc)


def _apply_step(state: SimState, pid: int, option: Option) -> None:
    for tool, args in option.compile_step(state, pid):
        if check_action(state, pid, tool, args) is None:
            apply_action(state, pid, tool, args)


def _simulate_epoch(state: SimState, pid: int, option: Option, turns: int) -> None:
    opp_ids = sorted(int(p) for p in state.players if int(p) != pid)
    for _ in range(turns):
        run_ambient(state, pid)
        if not option.termination(state, pid):
            _apply_step(state, pid, option)
        for opp in opp_ids:
            run_ambient(state, opp)
            stock_ai_turn(state, opp)
        state.turn += 1


@dataclass
class _Node:
    visits: dict[str, int] = field(default_factory=dict)
    values: dict[str, int] = field(default_factory=dict)

    def total(self) -> int:
        return sum(self.visits.values())

    def uct_pick(self, candidates: list[str]) -> str:
        n_total = self.total() + 1
        best: tuple[float, str] | None = None
        for oid in candidates:
            n = self.visits.get(oid, 0)
            if n == 0:
                return oid  # expand untried options first, candidate order
            q = self.values[oid] / n
            score = q + UCT_C * math.sqrt(math.log(n_total) / n)
            if best is None or score > best[0]:
                best = (score, oid)
        assert best is not None
        return best[1]

    def record(self, oid: str, value: int) -> None:
        self.visits[oid] = self.visits.get(oid, 0) + 1
        self.values[oid] = self.values.get(oid, 0) + value


def _candidates(state: SimState, pid: int) -> list[str]:
    return [oid for oid in sorted(OPTIONS)
            if OPTIONS[oid].initiation(state, pid)]


def search_option(
    belief: PlannerBelief,
    pid: int,
    *,
    method: str,
    budget: int = 16,
    epoch_turns: int = 3,
    seed: int = 0,
) -> SearchResult:
    """Pick the next option for ``pid`` under the given rollout budget."""
    assert method in ("mcts", "mcgs")
    root_state = SimState.from_doc(build_state_doc(belief, seed))
    root_candidates = _candidates(root_state, pid) or ["tech_race"]

    root = _Node()
    children: dict[str, _Node] = {}
    seen_keys: set[str] = set()
    transposition_hits = 0

    for i in range(budget):
        world = SimState.from_doc(build_state_doc(belief, seed * 10_007 + i + 1))
        oid1 = root.uct_pick(root_candidates)
        _simulate_epoch(world, pid, OPTIONS[oid1], epoch_turns)

        if method == "mcts":
            key = f"path:{oid1}"
        else:
            key = abstract_key(world, pid)
            if key in seen_keys:
                transposition_hits += 1
        seen_keys.add(key)
        node = children.setdefault(key, _Node())

        mid_candidates = _candidates(world, pid) or ["tech_race"]
        oid2 = node.uct_pick(mid_candidates)
        _simulate_epoch(world, pid, OPTIONS[oid2], epoch_turns)

        value = value_of(world, pid)
        root.record(oid1, value)
        node.record(oid2, value)

    chosen = max(root_candidates,
                 key=lambda oid: (root.visits.get(oid, 0),
                                  root.values.get(oid, 0), oid))
    return SearchResult(
        method=method, chosen=chosen,
        root_visits=dict(root.visits), root_values=dict(root.values),
        rollouts=budget,
        nodes_expanded=1 + len(children),
        unique_states=len(seen_keys),
        transposition_hits=transposition_hits,
    )
