"""ChaosDirector: canned unauthorized mutations + stock-AI-style interference.

Test instrument ONLY. It fires raw mutations through the adapter's chaos
helpers during a lease, exactly like an unsuppressed built-in AI or a lying
engine would. Assert-on-both-sides tests verify the mutation actually landed
BEFORE asserting the watchdog caught it — a watchdog that fires on nothing
cannot pass.
"""

from __future__ import annotations

import enum
from collections import deque
from dataclasses import dataclass
from typing import Any


class MutationSpec(enum.StrEnum):
    MOVE_UNCOMMANDED_UNIT = "move_uncommanded_unit"
    FLIP_PRODUCTION = "flip_production"
    CHANGE_RESEARCH = "change_research"
    SPAWN_FREE_UNIT = "spawn_free_unit"
    STEAL_GOLD = "steal_gold"
    AMBIENT_LIKE_TRAP = "ambient_like_trap"  # ambient-shaped but UNDECLARED


@dataclass
class ChaosEvent:
    spec: MutationSpec
    hook: str = "act"  # "begin_phase" | "act" | "end_phase"
    offset: int = 0  # skip this many matching hooks before firing


class ChaosDirector:
    def __init__(self, events: list[ChaosEvent]) -> None:
        self._pending: deque[ChaosEvent] = deque(events)
        self.fired: list[MutationSpec] = []

    @property
    def armed(self) -> int:
        return len(self._pending)

    def maybe_fire(self, hook: str, adapter: Any) -> None:
        if not self._pending:
            return
        event = self._pending[0]
        if event.hook != hook:
            return
        if event.offset > 0:
            event.offset -= 1
            return
        self._pending.popleft()
        self.fired.append(event.spec)
        _fire(event.spec, adapter)


def _phase_player(adapter: Any) -> int:
    return adapter.state.phase_player


def _fire(spec: MutationSpec, adapter: Any) -> None:
    state = adapter.state
    pid = _phase_player(adapter)
    if pid == -1:
        pid = 0

    if spec is MutationSpec.MOVE_UNCOMMANDED_UNIT:
        units = sorted(
            (u for u in state.units.values() if u["owner"] == pid),
            key=lambda u: u["unit_id"],
        )
        if not units:
            return
        victim = units[-1]  # last unit = least likely to be the one just commanded
        from civ_arena.game.sim.state import neighbors

        for nq, nr in sorted(neighbors(victim["q"], victim["r"])):
            if state.tile(nq, nr) is not None:
                adapter.chaos_move_unit(victim["unit_id"], nq, nr)
                return

    elif spec is MutationSpec.FLIP_PRODUCTION:
        cities = sorted(
            (c for c in state.cities.values() if c["owner"] == pid),
            key=lambda c: c["city_id"],
        )
        if cities:
            adapter.chaos_set("city", cities[0]["city_id"], "production_queue",
                              ["SCOUT"])

    elif spec is MutationSpec.CHANGE_RESEARCH:
        adapter.chaos_set("player", str(pid), "researching", "WRITING")

    elif spec is MutationSpec.SPAWN_FREE_UNIT:
        units = sorted(
            (u for u in state.units.values() if u["owner"] == pid),
            key=lambda u: u["unit_id"],
        )
        spot = (units[0]["q"], units[0]["r"]) if units else (0, 0)
        adapter.chaos_spawn_unit(pid, "WARRIOR", *spot)

    elif spec is MutationSpec.STEAL_GOLD:
        adapter.chaos_set("player", str(pid), "gold", max(0, state.player(pid)["gold"] - 50))

    elif spec is MutationSpec.AMBIENT_LIKE_TRAP:
        # An ambient-SHAPED mutation (a production yield tick) that was never
        # declared in the phase manifest — and whose origin tag lies. The
        # watchdog must flag it because authorization comes from the ledger.
        cities = sorted(
            (c for c in state.cities.values() if c["owner"] == pid),
            key=lambda c: c["city_id"],
        )
        if cities:
            adapter.chaos_set("city", cities[0]["city_id"], "production_bucket",
                              cities[0]["production_bucket"] + 5, origin="ambient")
