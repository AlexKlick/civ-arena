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

M19b — case base (optional ``case_base``): retrieval-as-evidence prior.
The world's signature is computed from the SAME determinized world the
trace root_key comes from (``bstate``), the immutable artifact ranks
options by mean differential among takens, the ranking is compiled
legal-now (the compile_proposal narrowing) and merged AFTER any live
proposer ranking. Per-turn failures degrade to no case prior; the prior
rides the TRACE (``case_prior`` entry), never the event log; the case
hit/miss counters are journaled diagnostics that never affect ranking.
Resume binds the artifact BYTES: the journaled digest is checked against
the loaded artifact and a mismatch degrades the leg to unprimed with an
on-disk ``artifact_mismatch`` flag — never a silent evidence swap.

M20b — contextual bandit (optional ``bandit``): online within-match
learning prior. At each needs_choice the pending PREVIOUS decision is
settled first — advantage = ``value_of(bstate_now) - value_of(the
previous decision's bstate)`` (value_of over the two successive decision
worlds, integer), ``bandit.update(prev_context, prev_option, advantage)``
— then the CURRENT world is bucketed (``context_bucket``) and the menu
ranked by the bandit, compiled legal-now, and merged into the prior
AFTER the proposer AND the case prior (merge order: proposer → case →
bandit — the live signal first, then retrieved evidence, then the
online-learned ranking; chained ``_merge_prior`` calls, first occurrence
wins). Fail-soft exactly like ``_case_prior`` (``bandit_prior`` trace
entry). The pending decision (last_decision_turn/context/option/value)
rides the journal's ``bandit`` block BESIDE the bandit state and is
restored at resume — journaling it is what keeps the resume bit-identity
pin holding: without it the first post-resume decision would settle no
pending (one update lost) and the learned state would diverge from the
uninterrupted twin's. DECLARED LIMIT: a crash BETWEEN the in-memory
pending update and the next journal append still loses that one update
(the bandit is advisory learning state; gameplay determinism is bounded
to the missing update's ranking effect and the pin covers the resumed
leg). An unarmed resume over an armed journal (or vice versa) ignores
the block, mirroring the case-stats additive rule.
"""

from __future__ import annotations

import copy
import random
from collections.abc import Iterable
from typing import Any

from civ_arena.game.sim.state import SimState
from civ_arena.game.sim.value import value_of
from civ_arena.planner.bandit import Bandit, context_bucket
from civ_arena.planner.belief import PlannerBelief, build_state_doc
from civ_arena.planner.casebase import signature_from_state
from civ_arena.planner.executor import execute_plan
from civ_arena.planner.options import OPTIONS
from civ_arena.planner.proposer import build_request, compile_proposal
from civ_arena.planner.search import _candidates, search_option

# M19 lane-0: the M16 gate ruling (program doc §7, c1827df) says the
# graph-search layer is NOT carried forward — sim-scale default = MCTS.
# The constant had stayed "mcgs" since before the ruling; every
# config-driven match (live legs included) has been running MCGS. Flipped
# to match the ruling; the live legs change behavior from the next game on.
SEARCH_METHOD = "mcts"
SEARCH_BUDGET = 12
EPOCH_TURNS = 3
RESELECT_EVERY = 3
# Runtime-side proposer spend authority (the HTTP client enforces its own
# LLMSpec budget too): the counter is JOURNALED with the belief snapshot,
# so a resumed proposer cannot reset its match budget (Codex M16b P1-2).
PROPOSER_POST_CAP = 64


def _compile_legal_ranking(ranked: Iterable[str], state: SimState,
                           pid: int) -> list[str]:
    """A ranked id list -> LEGAL-now ranking, mirroring compile_proposal's
    narrowing EXACTLY: non-str/unknown/duplicate ids drop (first kept),
    only initiation-true options survive. Shared by the case-prior and
    bandit-prior paths (renamed from ``_compile_case_ranking`` when the
    bandit joined) — any prior can only permute, never widen the search's
    choice set."""
    out: list[str] = []
    for oid in ranked:
        if not isinstance(oid, str) or oid in out or oid not in OPTIONS:
            continue
        if OPTIONS[oid].initiation(state, pid):
            out.append(oid)
    return out


def _merge_prior(proposer_ranked: list[str] | None,
                 case_ranked: list[str] | None) -> list[str] | None:
    """Proposer FIRST (the live, more specific signal), then case-ranked,
    then bandit-ranked ids not already present — the bandit leg merges by
    a second chained call (``_merge_prior(prior, bandit_ranked)``);
    associative through dict.fromkeys, first occurrence wins. Both
    optional — both empty stays None (unprimed)."""
    merged = list(dict.fromkeys(list(proposer_ranked or [])
                                + list(case_ranked or [])))
    return merged or None


class PlannerRuntime:
    """Belief-fair option planner. Deterministic in (player_id, seed)."""

    def __init__(self, player_id: int, seed: int, *,
                 method: str = SEARCH_METHOD, budget: int = SEARCH_BUDGET,
                 proposer: Any = None, case_base: Any = None,
                 bandit: Any = None) -> None:
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
        self.case_base = case_base  # casebase.CaseBase | None (M19b prior)
        self.case_hits = 0         # diagnostics only, journaled as case_stats
        self.case_misses = 0
        self._case_artifact_mismatch = False  # set on resume artifact swap
        self.bandit = bandit  # bandit.Bandit | None (M20b online prior)
        # the PENDING decision (in-memory, journaled beside the bandit
        # state): {"turn", "context", "option", "value"} of the last
        # completed selection, settled at the NEXT needs_choice
        self._bandit_pending: dict[str, Any] | None = None

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

    def _case_prior(self, bstate: SimState,
                    turn: int) -> tuple[list[str] | None, dict[str, Any]]:
        """M19b retrieval-as-evidence prior: signature of the CURRENT
        determinized world, historical option ranking, compiled legal-now.
        ``bstate`` is the exact world the trace root_key is computed from —
        search_option rebuilds ``build_state_doc(belief, seed=turn)`` with
        the same seed and the same (unmutated) belief, so
        signature_from_state(bstate) == projection of the recorded root_key
        BY CONSTRUCTION (pinned by test_signature_matches_root_key). The
        whole chain is fail-soft: the artifact was validated at
        construction, but ANY per-turn failure degrades to no case prior —
        the case base never blocks or breaks the match."""
        try:
            sig = signature_from_state(bstate, self.player_id)
            ranked = self.case_base.rank(sig)
            hit = 1 if self.case_base.hit(sig) else 0
            if hit:
                self.case_hits += 1
            else:
                self.case_misses += 1
            compiled = _compile_legal_ranking(ranked, bstate, self.player_id)
            doc: dict[str, Any] = {"turn": turn, "ranked": compiled,
                                   "signature": sig, "hit": hit}
            return (compiled or None), doc
        except Exception:
            return None, {"turn": turn, "error": "case_prior_failed"}

    def _case_stats_doc(self) -> dict[str, Any]:
        """The journal's case_stats block: the artifact digest in force,
        the cumulative counters, and — after a resume-time artifact swap
        degraded the runtime — the on-disk flag that says so."""
        stats: dict[str, Any] = {
            "artifact_sha256": (self.case_base.artifact_sha256
                                if self.case_base is not None else None),
            "hits": self.case_hits,
            "misses": self.case_misses,
        }
        if self._case_artifact_mismatch:
            stats["artifact_mismatch"] = True
        return stats

    def _bandit_prior(self, bstate: SimState,
                      turn: int) -> tuple[list[str] | None, dict[str, Any]]:
        """M20b online contextual prior. FIRST settles the pending previous
        decision: advantage = value_of over the two successive decision
        worlds (bstate NOW minus the bstate the pending was recorded at),
        fed to ``bandit.update`` — then buckets the CURRENT world, ranks
        the candidate menu (the same ``_candidates`` menu the search sees
        — bstate is the exact world the trace root_key comes from, so
        menu == trace candidates by construction), and compiles the
        ranking legal-now. Fail-soft exactly like ``_case_prior``: ANY
        per-turn failure degrades to no bandit prior and the match
        continues; the update is inside the same boundary (a poisoned
        bandit never blocks or breaks the match)."""
        try:
            if self._bandit_pending is not None:
                advantage = (value_of(bstate, self.player_id)
                             - int(self._bandit_pending["value"]))
                self.bandit.update(self._bandit_pending["context"],
                                   self._bandit_pending["option"], advantage)
            ctx = context_bucket(bstate, self.player_id)
            ranked = self.bandit.rank(ctx, _candidates(bstate, self.player_id))
            compiled = _compile_legal_ranking(ranked, bstate, self.player_id)
            doc: dict[str, Any] = {"turn": turn, "ranked": compiled,
                                   "context": list(ctx)}
            return (compiled or None), doc
        except Exception:
            return None, {"turn": turn, "error": "bandit_prior_failed"}

    def _bandit_journal_doc(self) -> dict[str, Any]:
        """The journal's bandit block: the bandit state doc plus the four
        pending-decision fields (journaling the pending is what holds the
        resume bit-identity pin — see the module docstring)."""
        doc: dict[str, Any] = dict(self.bandit.to_doc())
        pending = self._bandit_pending
        doc["last_decision_turn"] = pending["turn"] if pending else None
        doc["last_context"] = list(pending["context"]) if pending else None
        doc["last_option"] = pending["option"] if pending else None
        doc["last_value"] = pending["value"] if pending else None
        return doc

    @staticmethod
    def _bandit_pending_from_doc(doc: dict[str, Any]) -> dict[str, Any] | None:
        """Parse the journal block's pending fields back to the in-memory
        shape (JSON round-trips the context tuple as a list of ints)."""
        ctx = doc.get("last_context")
        option = doc.get("last_option")
        value = doc.get("last_value")
        turn = doc.get("last_decision_turn")
        if (not isinstance(ctx, list) or not isinstance(option, str)
                or not isinstance(value, int) or isinstance(value, bool)
                or not isinstance(turn, int) or isinstance(turn, bool)):
            return None
        return {"turn": turn, "context": tuple(int(x) for x in ctx),
                "option": option, "value": value}

    async def _filter_to_wire_vocabulary(
            self, facade: Any, bstate: Any,
            plan: list[tuple[str, dict[str, Any]]]
    ) -> list[tuple[str, dict[str, Any]]]:
        """The sim-to-real vocabulary seam (M17a): compiled plans carry
        SIM-table ids, but the engine on the wire offers its own research
        and production vocabulary (the fake's 4-tech subset today; Civ VI
        ids on the real engine). An id the wire never offered is REPLACED
        with the first offered id that passes sim prevalidation (prereq-
        aware, deterministic) and dropped only when nothing substitutes.
        The referee stays the authority; this filter stops known-dead
        calls before they become referee spend."""
        from civ_arena.game.sim.rules import check_action

        try:
            offered = sorted({e.get("tech_id") for e
                              in await facade.get_available_research()} - {None})
            prod: dict[str, list[str]] = {}
            for cid in {a.get("city_id") for t, a in plan
                        if t in ("set_city_production", "purchase") and a.get("city_id")}:
                prod[cid] = sorted(
                    {e.get("item_id") for e
                     in await facade.get_available_production(cid)} - {None})
        except Exception:
            return plan  # an observation failure must not eat the turn
        out: list[tuple[str, dict[str, Any]]] = []
        for tool, args in plan:
            if tool == "set_research" and args.get("tech_id") not in offered:
                sub = next((x for x in offered if check_action(
                    bstate, self.player_id, "set_research",
                    {"tech_id": x}) is None), None)
                if sub is None:
                    continue
                args = {**args, "tech_id": sub}
            if (tool in ("set_city_production", "purchase")
                    and args.get("city_id") in prod
                    and args.get("item_id") not in prod[args["city_id"]]):
                cid = args["city_id"]
                sub = next((x for x in prod[cid] if check_action(
                    bstate, self.player_id, tool,
                    {**args, "item_id": x}) is None), None)
                if sub is None:
                    continue
                args = {**args, "item_id": sub}
            out.append((tool, args))
        return out

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
        # the sim-frame origin rides the journal (a JSON round-trip makes
        # it a list); None stays None — a pre-origin snapshot re-derives
        origin = last["belief"].get("origin")
        belief.origin = (int(origin[0]), int(origin[1])) if origin else None
        belief.current_observable = set()
        belief.current_foreign_ids = set()
        self.belief = belief
        self.active = last["active"]
        self.chosen_at_turn = last["chosen_at_turn"]
        self.proposer_posts = int(last.get("proposer_posts", 0))
        # M19b case counters ride the journal the same way (additive: an old
        # journal without case_stats restores to zeros). DIAGNOSTICS ONLY —
        # the case prior stays a pure function of (belief, immutable
        # artifact), so restoring these counters can never shift ranking and
        # resume bit-identity holds.
        stats = last.get("case_stats") or {}
        self.case_hits = int(stats.get("hits", 0))
        self.case_misses = int(stats.get("misses", 0))
        # The journal binds the artifact BYTES the prior leg ran with. A
        # swapped (or removed) artifact across legs must never silently
        # re-prime the match with different evidence: on digest mismatch the
        # runtime DEGRADES explicitly — unprimed for the rest of the match —
        # and the next journal append flags the swap on disk
        # (case_stats.artifact_mismatch). No journaled digest (old journal
        # or a prior unarmed leg) leaves the construction-time arming as is.
        journal_sha = stats.get("artifact_sha256")
        if journal_sha:
            current = (self.case_base.artifact_sha256
                       if self.case_base is not None else None)
            if current != journal_sha:
                self.case_base = None
                self._case_artifact_mismatch = True
        # M20b additive restore: the bandit state AND the pending decision
        # ride the journal's bandit block. An armed runtime over a journal
        # WITHOUT a bandit block (old journal, or an unarmed first leg)
        # keeps its construction-time arming, mirroring the case-stats
        # rule; an unarmed runtime ignores the block. A bandit block that
        # refuses validation degrades the leg to unprimed (the journal is
        # advisory; the referee is untouched) — never a crash mid-resume.
        bandit_doc = last.get("bandit")
        if self.bandit is not None and isinstance(bandit_doc, dict):
            try:
                self.bandit = Bandit.from_doc(bandit_doc)
                self._bandit_pending = self._bandit_pending_from_doc(bandit_doc)
            except Exception:
                self.bandit = None
                self._bandit_pending = None

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
            case_doc: dict[str, Any] | None = None
            bandit_doc: dict[str, Any] | None = None
            if self.proposer is not None:
                prior, proposal_doc = await self._propose(bstate, turn)
            if self.case_base is not None:
                case_ranked, case_doc = self._case_prior(bstate, turn)
                if case_ranked:
                    prior = _merge_prior(prior, case_ranked)
            if self.bandit is not None:
                # settles the pending decision (the advantage update)
                # BEFORE choosing, then ranks — proposer → case → bandit
                bandit_ranked, bandit_doc = self._bandit_prior(bstate, turn)
                if bandit_ranked:
                    prior = _merge_prior(prior, bandit_ranked)
            result = search_option(
                self.belief, self.player_id, method=self.method,
                budget=self.budget, epoch_turns=EPOCH_TURNS, seed=turn,
                prior=prior)
            self.active = result.chosen
            self.chosen_at_turn = turn
            if self.bandit is not None:
                # record the NEW pending: this search's chosen option and
                # the value of THIS decision world — settled at the next
                # needs_choice. Recorded even when the bandit chain failed
                # this turn (value bookkeeping is bandit-health-neutral);
                # one pending at a time, always the latest decision.
                try:
                    self._bandit_pending = {
                        "turn": turn,
                        "context": context_bucket(bstate, self.player_id),
                        "option": result.chosen,
                        "value": value_of(bstate, self.player_id),
                    }
                except Exception:
                    self._bandit_pending = None
            entry = {"turn": turn, **result.to_doc()}
            if proposal_doc is not None:
                entry["proposal"] = proposal_doc
            if case_doc is not None:
                entry["case_prior"] = case_doc
            if bandit_doc is not None:
                entry["bandit_prior"] = bandit_doc
            self.trace.append(entry)

        plan = OPTIONS[self.active].compile_step(bstate, self.player_id)
        plan = await self._filter_to_wire_vocabulary(facade, bstate, plan)
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
                    "origin": b.origin,
                },
                "active": self.active, "chosen_at_turn": self.chosen_at_turn,
                "proposer_posts": self.proposer_posts,
                "case_stats": self._case_stats_doc(),
                # M20b additive block: armed legs only (an unarmed leg's
                # snapshot keeps the pre-M20b shape exactly)
                **({"bandit": self._bandit_journal_doc()}
                   if self.bandit is not None else {}),
            })
