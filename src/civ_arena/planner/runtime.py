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

M16a — resume without amnesia: the runtime journals each completed
turn's projected observations plus its option state to a side artifact
(``PlannerJournal``, injected through the coordinator's service binder)
and rebuilds the belief at the first post-resume turn by replaying the
journal through the SAME ``observe_*`` methods — rebuilt == live by
construction. A resumed planner is therefore bit-deterministic with its
uninterrupted twin (test-pinned via final_state_hash); the journal is
advisory only (see planner/journal.py for the trust model). The
per-decision search trace stays in-memory — it is an analysis artifact,
not gameplay state, and the harness persists it separately.
"""

from __future__ import annotations

import random
from typing import Any

from civ_arena.game.sim.state import SimState
from civ_arena.planner.belief import PlannerBelief, build_state_doc
from civ_arena.planner.executor import execute_plan
from civ_arena.planner.options import OPTIONS
from civ_arena.planner.proposer import build_request, compile_proposal
from civ_arena.planner.search import search_option

SEARCH_METHOD = "mcgs"
SEARCH_BUDGET = 12
EPOCH_TURNS = 3
RESELECT_EVERY = 3


class PlannerRuntime:
    """Belief-fair option planner. Deterministic in (player_id, seed)."""

    def __init__(self, player_id: int, seed: int, *,
                 method: str = SEARCH_METHOD, budget: int = SEARCH_BUDGET,
                 proposer: Any = None) -> None:
        self.player_id = player_id
        self.rng = random.Random(seed)
        self.method = method
        self.budget = budget
        self.belief = PlannerBelief(player_id)
        self.active: str | None = None
        self.chosen_at_turn = 0
        self.trace: list[dict[str, Any]] = []
        self.journal: Any = None  # PlannerJournal, injected via bind_services
        self._restored = False
        self.proposer = proposer  # ModelClient | None (M16b, untrusted prior)

    async def _propose(self, bstate: SimState, turn: int) -> tuple[
            list[str] | None, dict[str, Any]]:
        """Ask the untrusted proposer for a ranking; compile it to a LEGAL
        prior. ModelUnavailable / unparseable replies degrade to no prior —
        the proposer never blocks or breaks the match."""
        from civ_arena.agents.llm.client import ModelUnavailable, text_of

        req = build_request(bstate, self.player_id)
        try:
            reply = await self.proposer.create(
                system=req["system"], messages=req["messages"], tools=[])
            text = text_of(reply)
        except ModelUnavailable:
            return None, {"turn": turn, "error": "model_unavailable"}
        proposal = compile_proposal(text, bstate, self.player_id)
        doc: dict[str, Any] = {"turn": turn, "ranked": proposal.ranked,
                               "assumptions": proposal.raw_assumptions,
                               "contingencies": proposal.raw_contingencies}
        return (proposal.ranked or None), doc

    async def aclose(self) -> None:
        if self.proposer is not None:
            close = getattr(self.proposer, "aclose", None)
            if callable(close):
                await close()

    def bind_services(self, *, diary: Any = None, strategy: Any = None,
                      journal: Any = None) -> None:
        """Coordinator service hook (M16a): accept the seat's journal."""
        if journal is not None:
            self.journal = journal

    def _restore_from_journal(self, turn: int) -> None:
        """First take_turn after (re)construction: replay strictly-earlier
        journal entries through the observe_* seam, restoring fog memory
        and the option state exactly as the pre-crash runtime held it."""
        if self._restored or self.journal is None:
            self._restored = True
            return
        self._restored = True
        docs = self.journal.replay_upto(turn)
        for doc in docs:
            self.belief.observe_overview(doc["overview"])
            self.belief.observe_units(doc["units"], doc["turn"])
            self.belief.observe_cities(doc["cities"], doc["turn"])
            self.belief.observe_map(doc["map"])
        if docs:
            self.active = docs[-1]["active"]
            self.chosen_at_turn = docs[-1]["chosen_at_turn"]

    async def take_turn(self, facade: Any) -> None:
        overview = await facade.get_overview()
        turn = overview["turn"]
        self._restore_from_journal(turn)
        units_doc = await facade.get_units()
        cities_doc = await facade.get_cities()
        map_doc = await facade.get_visible_map()
        self.belief.observe_overview(overview)
        self.belief.observe_units(units_doc, turn)
        self.belief.observe_cities(cities_doc, turn)
        self.belief.observe_map(map_doc)

        bstate = SimState.from_doc(build_state_doc(self.belief, seed=turn))
        needs_choice = (
            self.active is None
            or OPTIONS[self.active].termination(bstate, self.player_id)
            or not OPTIONS[self.active].initiation(bstate, self.player_id)
            or turn - self.chosen_at_turn >= RESELECT_EVERY)
        if needs_choice:
            prior: list[str] | None = None
            proposal_doc: dict[str, Any] | None = None
            if self.proposer is not None:
                prior, proposal_doc = await self._propose(bstate, turn)
            result = search_option(
                self.belief, self.player_id, method=self.method,
                budget=self.budget, epoch_turns=EPOCH_TURNS, seed=turn,
                prior=prior)
            self.active = result.chosen
            self.chosen_at_turn = turn
            entry = {"turn": turn, **result.to_doc()}
            if proposal_doc is not None:
                entry["proposal"] = proposal_doc
            self.trace.append(entry)

        plan = OPTIONS[self.active].compile_step(bstate, self.player_id)
        await execute_plan(facade, self.belief, self.player_id, plan, seed=turn)
        await facade.end_turn()
        if self.journal is not None:
            # appended only after a COMPLETED turn: a crash mid-turn leaves
            # no entry, so the journal never claims a turn that did not end
            self.journal.append(turn, {
                "turn": turn, "overview": overview, "units": units_doc,
                "cities": cities_doc, "map": map_doc,
                "active": self.active, "chosen_at_turn": self.chosen_at_turn,
            })
