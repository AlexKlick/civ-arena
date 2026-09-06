"""Persistent recovery from observed HP; no model network or native game calls."""
import copy
from dataclasses import replace

import pytest

from civ_arena.agents.llm.runtime import LLMAgentRuntime
from civ_arena.agents.llm.strategic_controller import StrategicController
from civ_arena.agents.recovery import RecoveryPolicy, RecoveryTracker, observed_health
from civ_arena.agents.runtime import AgentProfile
from civ_arena.agents.scouting import MAX_UNITS, plan_scouting
from civ_arena.arena.referee import MatchAborted
from civ_arena.config import LLMSpec
from fakes import FakeModel, use
from test_strategic_controller import Facade, advance


def unit(hp=35, **kw):
    return {"unit_id": "u0:1", "owner_id": 0, "type": "WARRIOR", "coord": "0,0",
            "movement": 2, "hp": hp, "max_hp": 100, "health_valid": True,
            "fortified": False, **kw}


def proposal(tracker, units, turn):
    return tracker.begin_turn(units, player_id=0, turn=turn, completed_turn=turn - 1)


def graph(units, overlay, directive=None):
    return plan_scouting({"get_units": units, "get_visible_map": {"tiles": {
        "0,0": {"terrain": "PLAINS"}, "1,0": {"terrain": "PLAINS"}}}},
        directive={} if directive is None else directive, player_id=0,
        match_id="health", agent_id="a", turn=1, recovery=overlay)


def test_hysteresis_suspends_same_mission_until_observed_threshold():
    tracker = RecoveryTracker()
    directive = {"scouting": {"unit_types": ["WARRIOR"], "selection": "best"}}
    original = copy.deepcopy(directive)
    for turn, hp in enumerate([35, 70, 89], 1):
        units = [unit(hp, fortified=True)]
        state = proposal(tracker, units, turn)
        decision = graph(units, state["units"], directive)["decisions"][0]
        assert decision["selected"] is None
        assert decision["reason"] == "recovery_existing_standing_order"
        tracker.commit(state)
    healthy = [unit(90, fortified=True)]
    state = proposal(tracker, healthy, 4)
    assert state["units"] == {}
    assert state["transitions"][0]["reason"] == "observed_recovery_complete"
    decision = graph(healthy, state["units"], directive)["decisions"][0]
    assert decision["selected"]["action"] == "move_unit"
    assert directive == original


def test_entry_boundary_and_non_100_native_maximum():
    tracker = RecoveryTracker()
    assert not proposal(tracker, [unit(70)], 1)["units"]
    assert not proposal(tracker, [unit(140, max_hp=200)], 1)["units"]
    assert proposal(tracker, [unit(139, max_hp=200)], 1)["units"]


@pytest.mark.parametrize("changes", [
    {"hp": None}, {"hp": True}, {"hp": float("nan")}, {"hp": -1},
    {"max_hp": None}, {"max_hp": 0}, {"max_hp": True}, {"max_hp": 1_000_001},
    {"hp": 101}, {"health_valid": False}, {"health_valid": "true"}, {"health_valid": None},
])
def test_unavailable_invalid_health_never_proves_healthy(changes):
    actor = unit(100, **changes) if "hp" not in changes else unit(**changes)
    assert observed_health(actor) == {"hp": None, "max_hp": None, "health_valid": False}
    state = proposal(RecoveryTracker(), [actor], 1)
    assert state["units"]["u0:1"]["state"] == "health_unavailable"
    assert graph([actor], state["units"])["decisions"][0]["selected"]["action"] == "fortify"


def test_legacy_missing_maximum_is_unknown_and_cannot_complete_recovery():
    tracker = RecoveryTracker()
    tracker.commit(proposal(tracker, [unit()], 1))
    legacy = unit(100)
    legacy.pop("max_hp")
    legacy.pop("health_valid")
    state = proposal(tracker, [legacy], 2)
    assert state["units"]["u0:1"]["recovering"]
    assert state["transitions"] == []
    tracker.commit(state)
    assert proposal(tracker, [unit(80)], 3)["units"]


def test_stall_review_once_per_no_gain_streak_and_damage_requires_new_observation():
    tracker = RecoveryTracker()
    reasons = []
    for turn, hp in enumerate([35] * 7 + [25, 25, 40, 40, 40, 40], 1):
        state = proposal(tracker, [unit(hp)], turn)
        reasons.extend((turn, reason) for reason in state["review_reasons"])
        tracker.commit(state)
    assert reasons == [(4, "recovery_no_observed_gain"),
                       (8, "recovery_under_damage"), (13, "recovery_no_observed_gain")]


def test_proposals_do_not_advance_until_completion_and_loss_removes_overlay():
    tracker = RecoveryTracker()
    state = proposal(tracker, [unit()], 1)
    assert proposal(tracker, [unit()], 1) == state
    with pytest.raises(ValueError, match="consecutive"):
        proposal(tracker, [unit()], 2)
    tracker.commit(state)
    with pytest.raises(ValueError, match="exactly once"):
        tracker.commit(state)
    captured = unit(owner_id=1)
    next_state = proposal(tracker, [captured], 2)
    assert next_state["units"] == {}
    assert next_state["transitions"] == [{"unit_id": "u0:1", "reason": "no_longer_observed_owned"}]


@pytest.mark.parametrize("kw", [{"enter_below_percent": 0}, {"enter_below_percent": True},
    {"resume_at_percent": 70}, {"resume_at_percent": 101}, {"no_gain_turns": 0},
    {"no_gain_turns": 61}, {"no_gain_turns": False}])
def test_policy_rejects_invalid_bounds(kw):
    with pytest.raises(ValueError):
        RecoveryPolicy(**kw)


def test_recovery_orders_prioritized_with_existing_execution_cap():
    units = [unit(100, unit_id=f"u0:{i:03d}") for i in range(MAX_UNITS + 5)]
    units[-1]["hp"] = 20
    overlay = proposal(RecoveryTracker(), units, 1)["units"]
    plan = graph(units, overlay)
    assert len(plan["decisions"]) == MAX_UNITS
    assert plan["decisions"][0]["unit_id"] == units[-1]["unit_id"]
    assert plan["decisions"][0]["selected"]["action"] == "fortify"
    assert plan["deferred_units"] == 5
    with pytest.raises(ValueError, match="owned"):
        graph(units, {"u1:1": {}})


class HealthFacade(Facade):
    def __init__(self):
        super().__init__()
        self.units = [unit()]

    async def fortify(self, unit_id, **kwargs):
        result = await super().fortify(unit_id, **kwargs)
        next(u for u in self.units if u["unit_id"] == unit_id)["fortified"] = True
        return result

    async def move_unit(self, unit_id, dest, **kwargs):
        result = await super().move_unit(unit_id, dest, **kwargs)
        next(u for u in self.units if u["unit_id"] == unit_id)["fortified"] = False
        return result


def setup(directive=None):
    model = FakeModel([[use("submit_directive", {} if directive is None else directive)]])
    llm = replace(LLMSpec("http://unused", "UNUSED", "fake"), max_tool_rounds=1)
    runtime = LLMAgentRuntime.build(AgentProfile("a", 0, "llm", 4, llm=llm), client=model)
    records = []
    controller = StrategicController("health", cadence=60, audit=records.append)
    return controller, runtime, model, HealthFacade(), records


async def test_real_controller_recovers_resumes_without_model_requests_each_turn():
    controller, runtime, model, facade, records = setup()
    for turn, hp in enumerate([35, 50, 80, 90], 1):
        facade.units[0]["hp"] = hp
        await advance(controller, runtime, facade, turn)
    assert model.posts_sent == 1
    assert [call[0] for call in facade.calls if isinstance(call, tuple)] == ["fortify", "move_unit"]
    graphs = [row["graph"] for row in records if row["audit"] == "strategy_graph"]
    assert graphs[0]["execution"][0]["before"]["health"]["hp"] == 35
    assert graphs[0]["execution"][0]["after"]["health"]["hp"] == 35
    assert not graphs[1]["execution"] and not graphs[2]["execution"]
    assert not graphs[3]["recovery"]["units"]


async def test_explicit_emergency_move_interrupts_once_without_cancelling_recovery():
    controller, runtime, model, facade, records = setup({"tactical_overrides": [
        {"unit_id": "u0:1", "action": "move", "dest": "1,0"}]})
    await advance(controller, runtime, facade, 1)
    await advance(controller, runtime, facade, 2)
    assert model.posts_sent == 1
    actions = [call[0] for call in facade.calls if isinstance(call, tuple)]
    assert actions == ["move_unit", "fortify"]
    graph1 = next(r["graph"] for r in records if r["audit"] == "strategy_graph")
    decision = graph1["execution"][0]["decision"]
    assert decision["recovery_resolution"] == "one_turn_tactical_interruption"
    assert controller._recovery._units["u0:1"]["recovering"]
    assert controller.directive["tactical_overrides"] == []


async def test_stalled_recovery_shares_one_request_budget_and_does_not_repeat():
    controller, runtime, model, facade, records = setup()
    for turn in range(1, 8):
        await advance(controller, runtime, facade, turn)
    assert model.posts_sent == 2
    requests = [r for r in records if r["audit"] == "strategy_request"]
    assert [r["turn"] for r in requests] == [1, 4]
    assert "recovery_no_observed_gain" in requests[1]["user_context"]
    assert '"recovery"' in requests[1]["user_context"]
    assert all(r["attempt"] == 1 for r in requests)


async def test_failed_closure_never_commits_recovery_state():
    controller, runtime, model, facade, records = setup()
    facade.closures = [{"status": "rejected", "rejection": "unexpected_failure"}]
    with pytest.raises(MatchAborted):
        await advance(controller, runtime, facade, 1)
    assert controller._recovery._completed_turn == 0
    assert controller._recovery._units == {}


async def test_unknown_health_visible_to_model_and_never_treated_as_full_hp():
    import json

    from civ_arena.agents.llm.context_curator import CONTEXT_MARKER
    controller, runtime, model, facade, records = setup()
    facade.units[0].update(hp=None, max_hp=None, health_valid=False)
    await advance(controller, runtime, facade, 1)
    metadata_text, context_text = model.requests[0]["messages"][0]["content"].split("\n", 1)
    metadata = json.loads(metadata_text)
    assert metadata["recovery"]["units"][0][1:4] == ["health_unavailable", None, None]
    context = json.loads(context_text.removeprefix(CONTEXT_MARKER))
    assert context["own_units"][0]["hp"] is None
    assert context["own_units"][0]["health_valid"] is False
    assert not any(isinstance(call, tuple) and call[0] == "move_unit" for call in facade.calls)


def test_explicit_combat_override_keeps_recovery_and_observed_target_guards():
    units = [unit(), unit(100, unit_id="u1:1", owner_id=1, coord="1,0")]
    tracker = RecoveryTracker()
    overlay = proposal(tracker, units, 1)
    directive = {"tactical_overrides": [{"unit_id": "u0:1", "action": "attack",
                                         "target_id": "u1:1"}]}
    decision = graph(units, overlay["units"], directive)["decisions"][0]
    assert decision["selected"]["action"] == "attack"
    assert decision["recovery_resolution"] == "one_turn_tactical_interruption"
    tracker.commit(overlay)
    assert proposal(tracker, units, 2)["units"]["u0:1"]["recovering"]
    directive["tactical_overrides"][0]["target_id"] = "u1:missing"
    refused = graph(units, overlay["units"], directive)["decisions"][0]
    assert refused["selected"] is None


def test_missing_health_validity_is_unknown_even_with_plausible_hp():
    actor = unit(100)
    actor.pop("health_valid")
    assert not observed_health(actor)["health_valid"]
    assert proposal(RecoveryTracker(), [actor], 1)["units"]


async def test_later_action_refresh_new_damage_holds_and_persists_other_unit():
    controller, runtime, model, facade, records = setup()
    facade.units = [unit(100), unit(100, unit_id="u0:2")]
    original = facade.move_unit

    async def move_unit(unit_id, dest, **kwargs):
        result = await original(unit_id, dest, **kwargs)
        if unit_id == "u0:1" and facade.units[1]["hp"] == 100:
            facade.units[1]["hp"] = 35
        return result

    facade.move_unit = move_unit
    await advance(controller, runtime, facade, 1)
    assert ("fortify", "u0:2") in facade.calls
    assert not any(isinstance(c, tuple) and c[:2] == ("move_unit", "u0:2")
                   for c in facade.calls)
    assert controller._recovery._units["u0:2"]["recovering"]
    facade.units[1]["hp"] = 80
    await advance(controller, runtime, facade, 2)
    assert "u0:2" in controller._recovery._units
    assert not any(isinstance(c, tuple) and c[:2] == ("move_unit", "u0:2")
                   for c in facade.calls)
