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
turn's END-of-turn belief snapshot plus option state to a side artifact
(``PlannerJournal``, injected through the coordinator's service binder)
and restores it exactly at the first post-resume turn (rewind-aware).
With NO live proposer a resumed planner is bit-deterministic with its
uninterrupted twin (test-pinned via final_state_hash). DECLARED LIMIT
(Codex M16b P1-1): a LIVE proposer breaks resume-determinism — the
model is re-asked at post-resume reselections and may rank differently,
so proposer matches are replay-safe (the log re-issues recorded calls)
but not resume-identical; completed turns are never re-asked (the
restored option state carries them), so the exposure is bounded to
genuinely new decisions. Proposer spend is capped runtime-side and
JOURNALED, so resume cannot reset the budget. The journal is advisory
only (see planner/journal.py for the trust model). The per-decision
search trace stays in-memory — analysis artifact, not gameplay state.
"""

from __future__ import annotations

import copy
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
# Runtime-side proposer spend authority (the HTTP client enforces its own
# LLMSpec budget too): the counter is JOURNALED with the belief snapshot,
# so a resumed proposer cannot reset its match budget (Codex M16b P1-2).
PROPOSER_POST_CAP = 64


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
        self._processed_through = 0  # last turn this runtime completed
        self.proposer = proposer  # ModelClient | None (M16b, untrusted prior)
        self.proposer_posts = 0   # journaled; resume cannot reset the budget

    async def _propose(self, bstate: SimState, turn: int) -> tuple[
            list[str] | None, dict[str, Any]]:
        """Ask the untrusted proposer for a ranking; compile it to a LEGAL
        prior. ANY failure at the model boundary degrades to no prior —
        ModelUnavailable, malformed provider shapes (TypeError/ValueError/
        KeyError from a non-string text block), budget exhaustion — the
        proposer never blocks or breaks the match."""
        from civ_arena.agents.llm.client import ModelUnavailable, text_of

        if self.proposer_posts >= PROPOSER_POST_CAP:
            return None, {"turn": turn, "error": "proposer_budget_exhausted"}
        req = build_request(bstate, self.player_id)
        try:
            reply = await self.proposer.create(
                system=req["system"], messages=req["messages"], tools=[])
            text = text_of(reply)
        except (ModelUnavailable, TypeError, ValueError, KeyError):
            return None, {"turn": turn, "error": "model_unavailable"}
        self.proposer_posts += 1
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
        """Rewind-aware restore (Codex M16a P2): restore when the runtime is
        BLANK (fresh construction — nothing observed yet) or when the
        incoming turn rewinds to or behind turns this instance already
        processed (a reused runtime resuming an earlier checkpoint). A
        runtime progressing forward through its own turns restores never.
        The latest strictly-earlier snapshot is EXACT end-of-turn state
        (mid-turn reconciliations included), assigned directly onto a
        fresh belief — no observation replay, no drift."""
        blank = not self.belief.own_player
        if self.journal is None or (turn > self._processed_through and not blank):
            return
        docs = self.journal.replay_upto(turn)
        if not docs:
            return
        last = docs[-1]
        belief = PlannerBelief(self.player_id)
        belief.own_player = copy.deepcopy(last["belief"]["own_player"])
        # JSON round-trips int dict keys as strings — normalize back so a
        # post-restore observation (int keys) never mixes with snapshot
        # keys in the same dict
        belief.public_players = {
            int(k): copy.deepcopy(v)
            for k, v in last["belief"]["public_players"].items()}
        belief.own_units = copy.deepcopy(last["belief"]["own_units"])
        belief.own_cities = copy.deepcopy(last["belief"]["own_cities"])
        belief.foreign_units = copy.deepcopy(last["belief"]["foreign_units"])
        belief.foreign_cities = copy.deepcopy(last["belief"]["foreign_cities"])
        belief.tiles = copy.deepcopy(last["belief"]["tiles"])
        belief.turn = last["belief"]["turn"]
        belief.current_observable = set()
        belief.current_foreign_ids = set()
        self.belief = belief
        self.active = last["active"]
        self.chosen_at_turn = last["chosen_at_turn"]
        self.proposer_posts = int(last.get("proposer_posts", 0))

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
        self._processed_through = turn
        if self.journal is not None:
            # appended only after a COMPLETED turn: a crash mid-turn leaves
            # no entry. The snapshot is END-of-turn belief state — the
            # executor's mid-turn reconciliations (retired kill targets)
            # are part of it, so a rebuild cannot resurrect phantoms.
            b = self.belief
            self.journal.append(turn, {
                "turn": turn,
                "belief": {
                    "own_player": b.own_player, "public_players": b.public_players,
                    "own_units": b.own_units, "own_cities": b.own_cities,
                    "foreign_units": b.foreign_units,
                    "foreign_cities": b.foreign_cities,
                    "tiles": b.tiles, "turn": b.turn,
                },
                "active": self.active, "chosen_at_turn": self.chosen_at_turn,
                "proposer_posts": self.proposer_posts,
            })
