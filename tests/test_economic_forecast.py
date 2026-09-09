"""S3 forecast contract: unsupported-explicit, pre-action, no hindsight edits."""
import copy

import pytest

from civ_arena.agents.economic_forecast import (
    MAX_COMPARE_CANDIDATES,
    SELECTION_ALGORITHM,
    build_forecast,
    classify_outcome,
    compare_alternatives,
    forecast_completion_turn,
)


def row(item="MONUMENT", kind="building", cost=25, turns=3, **extra):
    return {"item_id": item, "kind": kind, "cost": cost, "turns": turns, **extra}


def candidate(item, kind="building", eligible=True, reason="building_available", **extra):
    return {"item_id": item, "kind": kind, "eligible": eligible, "reason": reason, **extra}


def horizons(record):
    return {entry["horizon"]: entry["value"] for entry in record["ranges"]
            if entry["metric"] == "completes_within_horizon"}


def test_contract_fields_are_distinct_classes():
    record = build_forecast(turn=10, city_id="c0:1", item_id="MONUMENT", row=row())
    assert set(record) == {"version", "city_id", "item_id", "item_kind", "issued_turn",
                           "observed", "assumptions", "ranges", "unsupported"}
    # observed facts carry turn-stamped accessors only; every assumption names
    # its invalidation; every range names its method; unsupported is prose.
    assert record["observed"]["turn"] == 10
    assert all(set(a) == {"id", "source", "invalidation"} for a in record["assumptions"])
    assert all(set(r) == {"metric", "lower", "upper", "method"}
               for r in record["ranges"] if r["metric"] == "production_completion_turn")
    assert all(isinstance(u, str) for u in record["unsupported"])
    assert "invalidation" not in str(record["observed"])


def test_missing_accessors_are_unsupported_never_fabricated():
    record = build_forecast(turn=10, city_id="c0:1", item_id="MONUMENT",
                            row={"item_id": "MONUMENT", "kind": "building"})
    assert record["ranges"] == ()
    assert "engine_turns_estimate" in record["unsupported"]
    # a missing accessor is None, never a guessed zero
    assert record["observed"]["engine_turns_estimate"] is None
    assert record["observed"]["catalog_cost"] is None
    classify = classify_outcome(forecast=record, observed_turn=12, queue_items=[])
    assert classify["verdict"] == "completion_unsupported"


def test_completion_interval_and_s3_horizons():
    record = build_forecast(turn=10, city_id="c0:1", item_id="MONUMENT", row=row(turns=3))
    assert forecast_completion_turn(record) == 13
    assert horizons(record) == {1: False, 3: True, 5: True}


def test_issued_forecast_is_never_edited_by_outcomes():
    record = build_forecast(turn=10, city_id="c0:1", item_id="MONUMENT", row=row(turns=3))
    frozen = copy.deepcopy(record)
    outcome = classify_outcome(forecast=record, observed_turn=13, queue_items=[])
    assert record == frozen
    assert outcome is not None and outcome["outcome"] == "completed"
    assert "verdict" not in record


@pytest.mark.parametrize("observed_turn,queue,expected", [
    (12, ["MONUMENT"], None),
    (13, ["MONUMENT"], None),
    (14, ["MONUMENT"], "overdue_pending"),
    (11, [], "completed"),
    (13, [], "completed"),
    (15, [], "completed"),
    (12, ["WARRIOR"], "invalidated_queue_changed"),
])
def test_classify_verdicts(observed_turn, queue, expected):
    record = build_forecast(turn=10, city_id="c0:1", item_id="MONUMENT", row=row(turns=3))
    outcome = classify_outcome(forecast=record, observed_turn=observed_turn,
                               queue_items=queue)
    if expected is None:
        assert outcome is None
        return
    assert outcome["outcome"] == expected
    if expected == "completed":
        # interval [13, 13]: earlier than the lower bound is a miss too
        assert outcome["verdict"] == ("early" if observed_turn < 13
                                      else "in_estimated_window" if observed_turn <= 13
                                      else "late")


def test_forecast_rejects_fabricated_inputs():
    with pytest.raises(ValueError):
        build_forecast(turn=0, city_id="c0:1", item_id="MONUMENT", row=row())
    with pytest.raises(ValueError):
        build_forecast(turn=10, city_id="", item_id="MONUMENT", row=row())
    with pytest.raises(ValueError):
        build_forecast(turn=10, city_id="c0:1", item_id="MONUMENT", row=None)


def test_zero_or_noninteger_turns_is_not_a_completion_basis():
    for bad in (0, -3, 2.5, True, "3", None):
        record = build_forecast(turn=10, city_id="c0:1", item_id="MONUMENT",
                                row={"item_id": "MONUMENT", "kind": "building",
                                     "turns": bad})
        assert record["ranges"] == ()
        assert record["observed"]["engine_turns_estimate"] is None
        assert "engine_turns_estimate" in record["unsupported"]


def test_comparison_bounded_deterministic_and_weighted():
    candidates = [candidate(item) for item in
                  ("WARRIOR", "SCOUT", "GRANARY", "MONUMENT", "WALLS", "BARRACKS")]
    catalog = [row("MONUMENT", turns=3), row("GRANARY", turns=4), row("WALLS", turns=5),
               row("BARRACKS", turns=6), row("WARRIOR", kind="unit", turns=2),
               row("SCOUT", kind="unit", turns=3)]
    record = compare_alternatives(turn=10, city_id="c0:1", candidates=candidates,
                                  catalog=catalog, preferences=["MONUMENT"])
    assert len(record["candidates"]) <= MAX_COMPARE_CANDIDATES
    assert record["selection"]["algorithm"] == SELECTION_ALGORITHM
    assert record["selection"]["deterministic"] is True
    assert record["selection"]["recommended"] == "MONUMENT"
    assert record["selection"]["weights"]
    assert record["candidate_coverage"].startswith("bounded")
    ineligible = candidate("SETTLER", kind="unit", eligible=False,
                           reason="unit_target_satisfied")
    again = compare_alternatives(turn=10, city_id="c0:1",
                                 candidates=candidates + [ineligible],
                                 catalog=catalog, preferences=["MONUMENT"])
    assert again == record


def test_comparison_threat_branch_preempts_investment():
    candidates = [candidate("MONUMENT"),
                  candidate("ARCHER", kind="unit", reason="below_unit_target",
                            effective=0)]
    catalog = [row("MONUMENT", turns=3),
               row("ARCHER", kind="unit", cost=35, turns=2)]
    record = compare_alternatives(turn=10, city_id="c0:1", candidates=candidates,
                                  catalog=catalog, preferences=["MONUMENT"],
                                  threats=("u9:1",),
                                  defense_context={'defenders': 0, 'defense_goal': 2})
    assert record["selection"]["recommended"] == "ARCHER"
    assert record["observed_threat_unit_ids"] == ["u9:1"]
    assert record["threat_contingency"]["consequence"].startswith("defense")
    assert record["threat_contingency"]["defense_goal"] == 2
    assert record["threat_contingency"]["shortfall_basis"] == "policy_result"
    assert record["candidates"][0]["completion_turn"] == 12


def test_comparison_threat_without_shortfall_keeps_growth_order():
    candidates = [candidate("MONUMENT"),
                  candidate("ARCHER", kind="unit", reason="below_unit_target",
                            effective=0)]
    catalog = [row("MONUMENT", turns=3),
               row("ARCHER", kind="unit", cost=35, turns=2)]
    # policy numbers say the defense goal is already satisfied: no preempt.
    # The eligible ARCHER contributes ZERO — the two defenders live in owned
    # inventory the candidate set cannot see, so the old eligible-candidate
    # sum would report a shortfall and flip the recommendation to ARCHER;
    # only the policy result's own totals keep the growth order.
    no_shortfall = compare_alternatives(turn=10, city_id="c0:1", candidates=candidates,
                                        catalog=catalog, preferences=["MONUMENT"],
                                        threats=("u9:1",),
                                        defense_context={'defenders': 2,
                                                         'defense_goal': 2})
    assert no_shortfall["selection"]["recommended"] == "MONUMENT"
    assert no_shortfall["threat_contingency"]["defenders_owned_queued_reserved"] == 2
    assert no_shortfall["threat_contingency"]["shortfall_basis"] == "policy_result"
    unknown = compare_alternatives(turn=10, city_id="c0:1", candidates=candidates,
                                   catalog=catalog, preferences=["MONUMENT"],
                                   threats=("u9:1",))
    # threats visible, shortfall unknown: recorded, contingency carried,
    # recommendation not silently flipped
    assert unknown["selection"]["recommended"] == "MONUMENT"
    assert unknown["observed_threat_unit_ids"] == ["u9:1"]
    assert unknown["threat_contingency"]["shortfall_basis"] == \
        "unknown_inventory_not_evaluated"


def test_comparison_threat_shortfall_without_eligible_defender_falls_through():
    candidates = [candidate("GRANARY"), candidate("MONUMENT")]
    catalog = [row("GRANARY", turns=4), row("MONUMENT", turns=3)]
    record = compare_alternatives(turn=10, city_id="c0:1", candidates=candidates,
                                  catalog=catalog, preferences=["MONUMENT"],
                                  threats=("u9:1",),
                                  defense_context={'defenders': 0, 'defense_goal': 2})
    # choose_production finds no eligible defender and falls through to the
    # normal cascade (preferred MONUMENT), never alphabetical GRANARY
    assert record["selection"]["recommended"] == "MONUMENT"


def test_comparison_unit_tier_mirrors_policy_fallback_order():
    candidates = [candidate("ARCHER", kind="unit", reason="below_unit_target"),
                  candidate("BUILDER", kind="unit", reason="below_unit_target")]
    catalog = [row("ARCHER", kind="unit", turns=2), row("BUILDER", kind="unit", turns=3)]
    record = compare_alternatives(turn=10, city_id="c0:1", candidates=candidates,
                                  catalog=catalog, preferences=[])
    # choose_production's unit order is BUILDER first, never alphabetical
    assert record["selection"]["recommended"] == "BUILDER"


def test_counterfactual_composes_engine_estimates():
    candidates = [candidate("MONUMENT"), candidate("WARRIOR", kind="unit",
                                                   reason="below_unit_target")]
    catalog = [row("MONUMENT", turns=3), row("WARRIOR", kind="unit", turns=2)]
    record = compare_alternatives(turn=10, city_id="c0:1", candidates=candidates,
                                  catalog=catalog, preferences=["MONUMENT"])
    branch = record["counterfactual"]
    assert branch["first_item_id"] == "MONUMENT"
    assert branch["second_item_id"] == "WARRIOR"
    assert branch["second_completion_turn"] == 15  # 10 + 3 + 2 at constant rates
    assert branch["invalidation"]


def test_static_effects_only_when_supplied():
    candidates = [candidate("MONUMENT")]
    catalog = [row("MONUMENT", turns=3)]
    absent = compare_alternatives(turn=10, city_id="c0:1", candidates=candidates,
                                  catalog=catalog, preferences=["MONUMENT"])
    assert "static_yield_and_maintenance_effects" in absent["unsupported"]
    statics = {"MONUMENT": {"yields": {"culture": 1}, "maintenance": 1}}
    present = compare_alternatives(turn=10, city_id="c0:1", candidates=candidates,
                                   catalog=catalog, preferences=["MONUMENT"],
                                   statics=statics)
    entry = present["candidates"][0]
    assert entry["static_effects"] == statics["MONUMENT"]
    assert "static_yield_and_maintenance_effects" not in present["unsupported"]


def test_comparison_without_eligible_candidates():
    record = compare_alternatives(turn=10, city_id="c0:1",
                                  candidates=[candidate("MONUMENT", eligible=False,
                                                        reason="unit_target_satisfied")],
                                  catalog=[row()], preferences=[])
    assert record["candidates"] == []
    assert record["selection"]["recommended"] is None
    assert record["counterfactual"] is None
