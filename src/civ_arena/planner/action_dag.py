"""M15c — the within-turn action DAG: typed edges, canonical partial order.

A plan is an ordered list of (tool, args) actions for ONE turn. The DAG
types the dependencies between them, derived statically from the rules'
resource vocabulary (the RejectionReason taxonomy):

- ``MUST_PRECEDE`` — same-unit sequences (movement is consumed, attacks
  zero it, a found_city consumes the settler): the plan author's order is
  the dependency; and purchase→purchase (gold draws down in plan order).
- ``MUTEX`` — two actions that overwrite the same single-slot decision
  (two set_research; two set_city_production on one city). A plan
  containing a MUTEX pair is refused at build time: last-write-wins
  ambiguity is a plan bug, not something to order around.
- everything else ``COMMUTES`` (edge absence).

``canonical_order`` topologically sorts with a deterministic tie-break
(tool ladder, then numeric entity id, then plan position). That is the
partial-order reduction: any two plans over the same action set whose
differences lie only in commuting actions execute in the SAME sequence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TOOL_LADDER = ("set_research", "set_city_production", "purchase", "found_city",
               "move_unit", "attack", "fortify")


def _unit_of(tool: str, args: dict[str, Any]) -> str | None:
    if tool in ("move_unit", "attack", "fortify", "found_city"):
        return args.get("unit_id")
    return None


def _city_of(tool: str, args: dict[str, Any]) -> str | None:
    if tool in ("set_city_production", "purchase"):
        return args.get("city_id")
    return None


def _entity_ord(tool: str, args: dict[str, Any]) -> int:
    eid = _unit_of(tool, args) or _city_of(tool, args) or ""
    return int(eid[1:]) if eid[1:].isdigit() else 0


@dataclass
class ActionDag:
    """Nodes are plan indices; ``preceders[j]`` = indices that must run first."""

    actions: list[tuple[str, dict[str, Any]]]
    preceders: dict[int, set[int]] = field(default_factory=dict)
    dependents: dict[int, set[int]] = field(default_factory=dict)

    def add_edge(self, i: int, j: int) -> None:
        self.preceders.setdefault(j, set()).add(i)
        self.dependents.setdefault(i, set()).add(j)


def build_dag(actions: list[tuple[str, dict[str, Any]]]) -> ActionDag:
    """Derive the dependency edges; refuse MUTEX-ambiguous plans loudly."""
    dag = ActionDag(actions=list(actions))
    for j in range(len(actions)):
        tool_j, args_j = actions[j]
        for i in range(j):
            tool_i, args_i = actions[i]
            unit_i, unit_j = _unit_of(tool_i, args_i), _unit_of(tool_j, args_j)
            if unit_i is not None and unit_i == unit_j:
                dag.add_edge(i, j)  # same-unit sequence: author order binds
                continue
            if tool_i == "purchase" and tool_j == "purchase":
                dag.add_edge(i, j)  # gold draws down in plan order
                continue
            if tool_i == "set_research" and tool_j == "set_research":
                raise ValueError(
                    "MUTEX plan: two set_research actions in one turn")
            if (tool_i == "set_city_production"
                    and tool_j == "set_city_production"
                    and _city_of(tool_i, args_i) == _city_of(tool_j, args_j)):
                raise ValueError(
                    f"MUTEX plan: two set_city_production actions for "
                    f"{_city_of(tool_i, args_i)} in one turn")
    return dag


def canonical_order(dag: ActionDag) -> list[int]:
    """Deterministic topological order: among ready nodes, smallest
    (tool-ladder rank, entity numeric id, plan position) runs first."""

    def key(idx: int) -> tuple[int, int, int]:
        tool, args = dag.actions[idx]
        rank = TOOL_LADDER.index(tool) if tool in TOOL_LADDER else len(TOOL_LADDER)
        return (rank, _entity_ord(tool, args), idx)

    pending = set(range(len(dag.actions)))
    done: set[int] = set()
    out: list[int] = []
    while pending:
        ready = [i for i in pending if dag.preceders.get(i, set()) <= done]
        assert ready, "cycle in action DAG (unreachable: edges follow plan order)"
        nxt = min(ready, key=key)
        pending.discard(nxt)
        done.add(nxt)
        out.append(nxt)
    return out
