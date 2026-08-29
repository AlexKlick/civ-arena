"""Deterministic review triggers and verdicts — pure, no arena imports.

Verdicts are DERIVED, never stored: the renderer recomputes "due this turn"
from (claims, facts, turn) at render time, so resume/replay need no extra
story for them. The M11 comparison contract is single-direction: a metric
claim is met when the observed value >= target; "at most" phrasing belongs
in the text and is scored self_assess (a cmp field is additive later).

Facts come from the player's own observation digests only — never AMBIENT —
so scoring under fog is correct by construction: an own city captured by the
enemy drops out of the next get_cities digest and the metric follows.
"""

from __future__ import annotations

from typing import Any

from civ_arena.strategy.claims import Goal, Prediction
from civ_arena.strategy.facts import Facts

# claim metric -> the facts sample key the observation digests carry
_FACT_KEY = {
    "cities": "own_cities",
    "units": "own_units",
    "techs": "techs",
    "gold": "gold",
    "population": "own_population",
}

MET = "met"
MISSED = "missed"
SELF_ASSESS = "self_assess"


def metric_value(
    facts: Facts, player_id: int, metric: str, turn: int,
) -> int | str | None:
    return facts.value(player_id, _FACT_KEY.get(metric, metric), turn)


def due_goals(store: Any, player_id: int, turn: int) -> list[Goal]:
    """Active goals whose deadline has arrived (or passed: a skipped review
    stays visible until the model amends or closes the goal)."""
    return [g for g in store.active_goals(player_id)
            if g.by_turn != 0 and g.by_turn <= turn]


def due_predictions(store: Any, player_id: int, turn: int) -> list[Prediction]:
    """Open predictions at or past their review turn — due until superseded:
    an unresolved prediction remains unresolved."""
    return [p for p in store.open_predictions(player_id)
            if p.review_turn <= turn]


def verdict(
    claim: Goal | Prediction, facts: Facts, player_id: int, turn: int,
) -> str:
    """met | missed | self_assess. Vision can only falsify claims about the
    player's OWN observable metrics: no metric, a subject the arena cannot
    see, or no sample at or before the review turn all degrade to
    self_assess — the model judges those and records a lesson."""
    if claim.metric == "":
        return SELF_ASSESS
    if getattr(claim, "subject_id", "") != "":
        return SELF_ASSESS
    value = metric_value(facts, player_id, claim.metric, turn)
    if value is None:
        return SELF_ASSESS
    return MET if value >= claim.target else MISSED
