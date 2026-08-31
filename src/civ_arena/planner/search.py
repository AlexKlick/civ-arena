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
    candidates: list[str]
    root_key: str
    seed: int
    budget: int
    epoch_turns: int
    prior: list[str]
    root_visits: dict[str, int]
    root_values: dict[str, int]
    rollouts: int
    nodes_expanded: int
    unique_states: int          # distinct ABSTRACT states reached (both methods)
    transposition_hits: int     # arrivals at a known state via a NEW root option

    def to_doc(self) -> dict[str, Any]:
        return {
            "method": self.method, "chosen": self.chosen,
            "candidates": list(self.candidates),
            "root_key": self.root_key,
            "seed": self.seed, "budget": self.budget,
            "epoch_turns": self.epoch_turns, "prior": list(self.prior),
            "root_visits": dict(sorted(self.root_visits.items())),
            "root_values": dict(sorted(self.root_values.items())),
            "rollouts": self.rollouts, "nodes_expanded": self.nodes_expanded,
            "unique_states": self.unique_states,
            "transposition_hits": self.transposition_hits,
        }


def abstract_key(state: SimState, pid: int) -> str:
    """Exact integer strategic abstraction — the MCGS equivalence key.

    Coarse on the axes options do not read (gold in buckets of 25, unit
    COUNTS by type) but exact on everything that changes which options are
    live and what their steps produce: the INITIATION VECTOR (two states
    offering different option candidacies never merge — an enemy stepping
    adjacent flips ``defend`` on and splits the node), current research,
    and per-city development (buildings, queue head). Merged nodes are
    BELIEF-level aggregates: sharing across determinized worlds is the
    point (variance smoothing over unknowns), not an accident.
    """
    doc: dict[str, Any] = {
        "turn": state.turn,
        "candidates": _candidates(state, pid),
    }
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
            "researching": p["researching"],
            "units": dict(sorted(units.items())),
            "development": sorted(
                [c["city_id"], ",".join(c["buildings"]),
                 c["production_queue"][0] if c["production_queue"] else ""]
                for c in cities),
        }
    return canonical(doc)


def _apply_step(state: SimState, pid: int, option: Option) -> None:
    for tool, args in option.compile_step(state, pid):
        if check_action(state, pid, tool, args) is None:
            apply_action(state, pid, tool, args)


def _simulate_epoch(state: SimState, pid: int, option: Option, turns: int,
                    *, skip_first_ambient: bool = False) -> None:
    """Simulate ``turns`` full turns under ``option``.

    Phase alignment: the LIVE observation the belief was built from was
    taken AFTER begin_phase ran the ambient batch, so the FIRST simulated
    turn of a search must act without a leading ambient or it
    double-applies one batch (a 45/50 queue would complete a turn early).
    A mid-epoch terminated option falls back to the ``tech_race`` filler —
    the runtime would re-search, which one rollout cannot afford; the
    filler is the declared stand-in for "keep doing something sane".
    """
    opp_ids = sorted(int(p) for p in state.players if int(p) != pid)
    filler = OPTIONS["tech_race"]
    for i in range(turns):
        if not (skip_first_ambient and i == 0):
            run_ambient(state, pid)
        active = filler if option.termination(state, pid) else option
        _apply_step(state, pid, active)
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


def _prior_order(candidates: list[str], prior: list[str] | None) -> list[str]:
    """Reorder candidates so prior-ranked options are visited FIRST under
    the untried-first rule (stable within groups; unknown ids ignored —
    the prior can only permute, never add). This is the M16b proposer's
    entire authority: with a budget below the candidate count it decides
    what gets explored; with a full budget it fades to visit order."""
    if not prior:
        return candidates
    rank = {oid: i for i, oid in enumerate(prior)}
    return sorted(candidates, key=lambda oid: (rank.get(oid, len(rank)), oid))


def search_option(
    belief: PlannerBelief,
    pid: int,
    *,
    method: str,
    budget: int = 16,
    epoch_turns: int = 3,
    seed: int = 0,
    prior: list[str] | None = None,
) -> SearchResult:
    """Pick the next option for ``pid`` under the given rollout budget."""
    assert method in ("mcts", "mcgs")
    root_state = SimState.from_doc(build_state_doc(belief, seed))
    candidate_set = _candidates(root_state, pid) or ["tech_race"]
    root_candidates = _prior_order(candidate_set, prior)

    root = _Node()
    children: dict[str, _Node] = {}
    # instrumentation is method-independent: abstract states reached, and
    # the set of root options that reached each (a TRANSPOSITION is an
    # arrival at a known state via a NEW root option — same-parent
    # revisits are just node revisits, trees do those too)
    state_parents: dict[str, set[str]] = {}
    transposition_hits = 0

    for i in range(budget):
        world = SimState.from_doc(build_state_doc(belief, seed * 10_007 + i + 1))
        oid1 = root.uct_pick(root_candidates)
        _simulate_epoch(world, pid, OPTIONS[oid1], epoch_turns,
                        skip_first_ambient=True)

        akey = abstract_key(world, pid)
        parents = state_parents.setdefault(akey, set())
        if parents and oid1 not in parents:
            transposition_hits += 1
        parents.add(oid1)

        key = f"path:{oid1}" if method == "mcts" else akey
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
        candidates=list(candidate_set),
        root_key=abstract_key(root_state, pid),
        seed=seed, budget=budget, epoch_turns=epoch_turns,
        prior=list(prior or []),
        root_visits=dict(root.visits), root_values=dict(root.values),
        rollouts=budget,
        nodes_expanded=1 + len(children),
        unique_states=len(state_parents),
        transposition_hits=transposition_hits,
    )
