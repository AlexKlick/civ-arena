"""M15d — PlannerRuntime: the ``planner`` policy behind the tool facade.

Per turn: observe (the four projection kinds) → fold into the M15b
belief → if no active option, or it terminated, or the reselection
cadence is due, run ``search_option`` → compile the active option's step
→ execute through the M15c guarded executor → end turn.

Search parameters are fixed constants this cycle (no config surface —
the Experiment-3 harness varies them programmatically). The per-decision
search trace accumulates on ``self.trace`` as canonical-ready docs
(integers only); the experiment harness persists it as the
``runs/<id>/planner/`` side artifact — the arena log never carries it.
"""

from __future__ import annotations

import random
from typing import Any

from civ_arena.game.sim.state import SimState
from civ_arena.planner.belief import PlannerBelief, build_state_doc
from civ_arena.planner.executor import execute_plan
from civ_arena.planner.options import OPTIONS
from civ_arena.planner.search import search_option

SEARCH_METHOD = "mcgs"
SEARCH_BUDGET = 12
EPOCH_TURNS = 3
RESELECT_EVERY = 3


class PlannerRuntime:
    """Belief-fair option planner. Deterministic in (player_id, seed)."""

    def __init__(self, player_id: int, seed: int, *,
                 method: str = SEARCH_METHOD, budget: int = SEARCH_BUDGET) -> None:
        self.player_id = player_id
        self.rng = random.Random(seed)
        self.method = method
        self.budget = budget
        self.belief = PlannerBelief(player_id)
        self.active: str | None = None
        self.chosen_at_turn = 0
        self.trace: list[dict[str, Any]] = []

    async def take_turn(self, facade: Any) -> None:
        overview = await facade.get_overview()
        self.belief.observe_overview(overview)
        turn = overview["turn"]
        self.belief.observe_units(await facade.get_units(), turn)
        self.belief.observe_cities(await facade.get_cities(), turn)
        self.belief.observe_map(await facade.get_visible_map())

        bstate = SimState.from_doc(build_state_doc(self.belief, seed=turn))
        needs_choice = (
            self.active is None
            or OPTIONS[self.active].termination(bstate, self.player_id)
            or not OPTIONS[self.active].initiation(bstate, self.player_id)
            or turn - self.chosen_at_turn >= RESELECT_EVERY)
        if needs_choice:
            result = search_option(
                self.belief, self.player_id, method=self.method,
                budget=self.budget, epoch_turns=EPOCH_TURNS, seed=turn)
            self.active = result.chosen
            self.chosen_at_turn = turn
            self.trace.append({"turn": turn, **result.to_doc()})

        plan = OPTIONS[self.active].compile_step(bstate, self.player_id)
        await execute_plan(facade, self.belief, self.player_id, plan, seed=turn)
        await facade.end_turn()
