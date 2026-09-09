"""Closed-loop develop-city option: interrupt, censor, resolve; advisory only."""
import copy

from civ_arena.agents.build_option import (
    MAX_ADVISORY_CITIES,
    DevelopmentOptionMonitor,
    barbarians_near_owned_cities,
)
from civ_arena.agents.economic_forecast import build_forecast


def city(cid="c0:1", queue=None, owner=0, coord="0,0"):
    return {"city_id": cid, "owner": owner, "coord": coord,
            "production_queue": [] if queue is None else queue,
            "population": 3}


def unit(uid="u0:1", owner=0, kind="SCOUT", coord="0,0", barbarian=False):
    return {"unit_id": uid, "owner_id": owner, "type": kind, "coord": coord,
            "is_barbarian": barbarian}


def forecast(item="MONUMENT", turns=3, turn=10, cid="c0:1"):
    return build_forecast(turn=turn, city_id=cid, item_id=item,
                          row={"item_id": item, "kind": "building",
                               "cost": 25, "turns": turns})


def events_by_kind(events):
    return {event["event"]: event for event in events}


def test_pending_build_on_time_resolves_nothing():
    monitor = DevelopmentOptionMonitor(0)
    monitor.register(forecast())
    assert monitor.observe(turn=11, cities=[city(queue=["MONUMENT"])],
                           units=[unit()]) == []
    assert monitor.observe(turn=12, cities=[city(queue=["MONUMENT"])],
                           units=[unit()]) == []


def test_completion_records_outcome_and_clears_pending():
    monitor = DevelopmentOptionMonitor(0)
    monitor.register(forecast())
    events = monitor.observe(turn=13, cities=[city(queue=[])], units=[unit()])
    outcome = events_by_kind(events)["completed"]
    assert outcome["verdict"] == "in_estimated_window"
    assert monitor.advisory(turn=13, cities=[city(queue=[])], units=[unit()],
                            production={},
                            preferences=[])["recent_outcomes"] == [outcome]
    assert monitor.observe(turn=14, cities=[city(queue=[])], units=[unit()]) == []


def test_threat_interrupt_censors_before_completion_and_survives_it():
    monitor = DevelopmentOptionMonitor(0)
    monitor.register(forecast())
    raider = unit("u9:1", owner=1, kind="WARRIOR", coord="2,0", barbarian=True)
    frozen = copy.deepcopy(monitor._pending["c0:1"]["forecast"])
    events = monitor.observe(turn=11, cities=[city(queue=["MONUMENT"])],
                             units=[unit(), raider])
    censor = events_by_kind(events)["threat_censored"]
    assert censor["threat_unit_ids"] == ["u9:1"]
    assert monitor._pending["c0:1"]["forecast"] == frozen  # forecast never edited
    events = monitor.observe(turn=13, cities=[city(queue=[])], units=[unit(), raider])
    completed = events_by_kind(events)["completed"]
    assert completed["censored"] == "threat_interrupt"
    assert completed["censored_turn"] == 11


def test_defender_build_is_not_censored():
    monitor = DevelopmentOptionMonitor(0)
    monitor.register(forecast(item="WARRIOR", turns=2))
    raider = unit("u9:1", owner=1, kind="WARRIOR", coord="2,0", barbarian=True)
    events = monitor.observe(turn=11, cities=[city(queue=["WARRIOR"])],
                             units=[unit(), raider])
    assert events == []
    events = monitor.observe(turn=12, cities=[city(queue=[])], units=[unit(), raider])
    assert events_by_kind(events)["completed"].get("censored") is None


def test_censor_event_fires_once_per_pending_build():
    monitor = DevelopmentOptionMonitor(0)
    monitor.register(forecast())
    raider = unit("u9:1", owner=1, kind="WARRIOR", coord="2,0", barbarian=True)
    first = monitor.observe(turn=11, cities=[city(queue=["MONUMENT"])],
                            units=[unit(), raider])
    assert [event["event"] for event in first] == ["threat_censored"]
    again = monitor.observe(turn=12, cities=[city(queue=["MONUMENT"])],
                            units=[unit(), raider])
    assert again == []
    assert monitor._pending["c0:1"]["censored_turn"] == 11


def test_healthy_countdown_does_not_censor():
    """GetTurnsLeft is a countdown: turns decrement while completion holds."""
    monitor = DevelopmentOptionMonitor(0)
    monitor.register(forecast(turns=3, turn=10))  # implied completion turn 13
    for turn, remaining in ((11, 2), (12, 1)):
        events = monitor.observe(
            turn=turn, cities=[city(queue=["MONUMENT"])], units=[unit()],
            catalogs={"c0:1": [{"item_id": "MONUMENT", "kind": "building",
                                "cost": 25, "turns": remaining}]})
        assert events == []
    assert monitor._pending["c0:1"]["censored"] is None


def test_completion_date_slippage_censors_once():
    monitor = DevelopmentOptionMonitor(0)
    monitor.register(forecast(turns=3, turn=10))  # implied completion turn 13
    stalled = monitor.observe(
        turn=11, cities=[city(queue=["MONUMENT"])], units=[unit()],
        catalogs={"c0:1": [{"item_id": "MONUMENT", "kind": "building",
                            "cost": 25, "turns": 3}]})
    assert [event["event"] for event in stalled] == ["rate_estimate_changed"]
    assert stalled[0]["recorded_completion_turn"] == 13
    assert stalled[0]["implied_completion_turn"] == 14
    repeat = monitor.observe(
        turn=12, cities=[city(queue=["MONUMENT"])], units=[unit()],
        catalogs={"c0:1": [{"item_id": "MONUMENT", "kind": "building",
                            "cost": 25, "turns": 3}]})
    assert repeat == []
    completed = monitor.observe(turn=14, cities=[city(queue=[])], units=[unit()])
    assert completed[0]["censored"] == "rate_estimate_changed"
    assert completed[0]["censored_turn"] == 11


def test_completed_item_is_not_rate_censored_by_its_own_fresh_row():
    """R3 regression: a repeatable item's fresh catalog row estimates a NEW
    build; read after completion it must not rate-censor the build it just
    finished reporting as complete."""
    monitor = DevelopmentOptionMonitor(0)
    monitor.register(forecast(item="WARRIOR", turns=2, turn=10))  # completes turn 12
    events = monitor.observe(
        turn=12, cities=[city(queue=[])], units=[unit()],
        catalogs={"c0:1": [{"item_id": "WARRIOR", "kind": "unit",
                            "cost": 30, "turns": 2}]})
    completed = events_by_kind(events)["completed"]
    assert completed["verdict"] == "in_estimated_window"
    assert completed.get("censored") is None
    assert "rate_estimate_changed" not in events_by_kind(events)


def test_replaced_item_with_fresh_row_invalidates_without_rate_censor():
    """R4 regression: a replaced build resolves as invalidated_queue_changed;
    the monitored item's fresh row — conflicting estimate included — is a NEW
    build's countdown and must not fire the rate recheck."""
    monitor = DevelopmentOptionMonitor(0)
    monitor.register(forecast())  # MONUMENT, completion turn 13
    events = monitor.observe(
        turn=11, cities=[city(queue=["GRANARY"])], units=[unit()],
        catalogs={"c0:1": [{"item_id": "MONUMENT", "kind": "building",
                            "cost": 25, "turns": 9}]})  # implied 20 != 13
    assert events_by_kind(events)["invalidated_queue_changed"]["item_id"] == "MONUMENT"
    assert "rate_estimate_changed" not in events_by_kind(events)
    assert monitor._pending == {}


def test_item_behind_another_queue_head_is_not_rechecked():
    """Head equality, not membership: a monitored item sitting second in the
    queue is no longer the build its fresh catalog row speaks for."""
    monitor = DevelopmentOptionMonitor(0)
    monitor.register(forecast())
    events = monitor.observe(
        turn=11, cities=[city(queue=["GRANARY", "MONUMENT"])], units=[unit()],
        catalogs={"c0:1": [{"item_id": "MONUMENT", "kind": "building",
                            "cost": 25, "turns": 9}]})
    assert events_by_kind(events)["invalidated_queue_changed"]["item_id"] == "MONUMENT"
    assert "rate_estimate_changed" not in events_by_kind(events)


def test_zero_or_absent_turns_row_never_censors():
    monitor = DevelopmentOptionMonitor(0)
    monitor.register(forecast(turns=3, turn=10))
    for bad in (0, None):
        events = monitor.observe(
            turn=11, cities=[city(queue=["MONUMENT"])], units=[unit()],
            catalogs={"c0:1": [{"item_id": "MONUMENT", "kind": "building",
                                "cost": 25, "turns": bad}]})
        assert events == []


def test_threat_outside_radius_does_not_censor():
    monitor = DevelopmentOptionMonitor(0)
    monitor.register(forecast())
    distant = unit("u9:1", owner=1, kind="WARRIOR", coord="9,9", barbarian=True)
    assert monitor.observe(turn=11, cities=[city(queue=["MONUMENT"])],
                           units=[unit(), distant]) == []


def test_queue_change_invalidates_option():
    monitor = DevelopmentOptionMonitor(0)
    monitor.register(forecast())
    events = monitor.observe(turn=12, cities=[city(queue=["WARRIOR"])], units=[unit()])
    assert events_by_kind(events)["invalidated_queue_changed"]["item_id"] == "MONUMENT"
    assert monitor.advisory(turn=12, cities=[city(queue=["WARRIOR"])], units=[unit()],
                            production={}, preferences=[])["pending"] == []


def test_overdue_reported_once_while_still_pending():
    monitor = DevelopmentOptionMonitor(0)
    monitor.register(forecast())  # estimated completion turn 13
    assert monitor.observe(turn=14, cities=[city(queue=["MONUMENT"])],
                           units=[unit()])[0]["event"] == "overdue_pending"
    assert monitor.observe(turn=15, cities=[city(queue=["MONUMENT"])],
                           units=[unit()]) == []
    assert "c0:1" in monitor._pending


def test_different_reissue_supersedes_pending_plan():
    monitor = DevelopmentOptionMonitor(0)
    monitor.register(forecast())
    monitor.register(forecast(item="GRANARY", turn=11))
    outcomes = monitor.advisory(turn=11, cities=[city()], units=[unit()],
                                production={}, preferences=[])["recent_outcomes"]
    assert outcomes[0]["event"] == "invalidated_superseded"
    assert monitor._pending["c0:1"]["forecast"]["item_id"] == "GRANARY"


def test_lost_city_invalidates():
    monitor = DevelopmentOptionMonitor(0)
    monitor.register(forecast())
    events = monitor.observe(turn=11, cities=[], units=[unit()])
    assert events_by_kind(events)["invalidated_city_unobserved"]
    assert monitor._pending == {}


def test_barbarian_detection_uses_production_policy_observable():
    raider = unit("u9:1", owner=1, kind="WARRIOR", coord="2,0", barbarian=True)
    far_raider = unit("u9:2", owner=1, kind="WARRIOR", coord="9,9", barbarian=True)
    foreign_army = unit("u8:1", owner=2, kind="WARRIOR", coord="1,0", barbarian=False)
    own = unit()
    assert barbarians_near_owned_cities(
        [raider, far_raider, foreign_army, own], [city()], player_id=0) == ["u9:1"]


def test_advisory_shape_bounded_and_non_executive():
    monitor = DevelopmentOptionMonitor(0)
    monitor.register(forecast())
    cities = [city(f"c0:{i}") for i in range(1, 5)]
    production = {f"c0:{i}": [{"item_id": "MONUMENT", "kind": "building",
                               "cost": 25, "turns": 3}] for i in range(1, 5)}
    block = monitor.advisory(turn=12, cities=cities, units=[unit()],
                             production=production, preferences=["MONUMENT"])
    assert block["authority"] == "advisory_only_executor_unchanged"
    assert block["basis"]
    assert len(block["city_plans"]) == MAX_ADVISORY_CITIES
    assert set(block["city_plans"]) == {"c0:1", "c0:2"}
    assert block["pending"][0]["item_id"] == "MONUMENT"
    assert "execute" not in str(block) and "tool_call" not in str(block)
    queued = city("c0:9", queue=["WALLS"])
    assert monitor.advisory(turn=12, cities=[*cities, queued], units=[unit()],
                            production=production,
                            preferences=[])["city_plans"].keys() == {"c0:1", "c0:2"}


def test_advisory_carries_confirmed_threats_without_flipping_recommendation():
    monitor = DevelopmentOptionMonitor(0)
    raider = unit("u9:1", owner=1, kind="WARRIOR", coord="2,0", barbarian=True)
    block = monitor.advisory(
        turn=12, cities=[city()], units=[unit(), raider],
        production={"c0:1": [{"item_id": "MONUMENT", "kind": "building",
                              "cost": 25, "turns": 3}]},
        preferences=["MONUMENT"])
    plan = block["city_plans"]["c0:1"]
    # Threat is visible to the model; the shortfall is unknown, so the
    # recommendation stands and the contingency names the preempt condition.
    assert plan["observed_threat_unit_ids"] == ["u9:1"]
    assert plan["selection"]["recommended"] == "MONUMENT"
    assert plan["threat_contingency"]["preempt_condition"] == \
        "defenders_owned_queued_reserved < defense_goal and an eligible defender exists"
    assert plan["threat_contingency"]["shortfall_basis"] == \
        "unknown_inventory_not_evaluated"
