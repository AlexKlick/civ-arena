"""Closed-loop evidence uses projected own positions and completed turn clocks."""
from __future__ import annotations

import copy

import pytest

from civ_arena.agents.llm.runtime import LLMAgentRuntime
from civ_arena.agents.llm.strategic_controller import StrategicController
from civ_arena.agents.runtime import AgentProfile
from civ_arena.agents.scouting import MAX_UNITS, ScoutingFeedback
from civ_arena.arena.referee import MatchAborted
from civ_arena.config import LLMSpec
from fakes import FakeModel, use
from test_scouting import plan, snapshot, unit
from test_scouting_standing_intent import FrozenClosureFacade


def pending(uid="u0:131073", origin="0,0", dest="1,0", status="accepted", after=None):
    return {"execution": [{"unit_id": uid, "action": "move_unit", "status": status,
            "args": {"dest": dest}, "before": {"owned": True, "coord": origin},
            "after": {"owned": True, "coord": origin if after is None else after}}]}


def begin(feedback, state=None, turn=2, completed=1):
    return feedback.begin_turn(snapshot() if state is None else state,
                               player_id=0, turn=turn, completed_turn=completed)


@pytest.mark.parametrize("status,after", [("rejected", "0,0"), ("accepted", "1,0")])
def test_rejected_or_displaced_attempts_do_not_create_pending_suppression(status, after):
    feedback = ScoutingFeedback()
    assert feedback.remember_completed(pending(status=status, after=after), turn=1) == []
    assert begin(feedback)["suppressed"] == {}


@pytest.mark.parametrize("changed", ["later_arrival", "other_displacement", "lost", "foreign"])
def test_delayed_displacement_or_loss_clears_pending_without_inferred_failure(changed):
    feedback = ScoutingFeedback()
    feedback.remember_completed(pending(), turn=1)
    entities = ([] if changed == "lost" else [unit(
        coord={"later_arrival": "1,0", "other_displacement": "0,1"}.get(changed, "0,0"),
        owner=1 if changed == "foreign" else 0)])
    audit = begin(feedback, snapshot(entities))
    assert not audit["confirmed"] and not audit["suppressed"]
    assert audit["forgotten"] == [{"unit_id": "u0:131073", "reason": "lost_or_changed_origin"}]


@pytest.mark.parametrize("turn,completed", [(3, 2), (2, 0), (1, 1)])
def test_unconfirmed_gap_never_turns_stale_attempt_into_cooldown(turn, completed):
    feedback = ScoutingFeedback()
    feedback.remember_completed(pending(), turn=1)
    audit = begin(feedback, turn=turn, completed=completed)
    assert not audit["confirmed"] and not audit["suppressed"]
    assert audit["forgotten"][0]["reason"] == "unconfirmed_turn_gap"


def test_confirmation_suppresses_for_exactly_three_own_turns_then_expires():
    feedback = ScoutingFeedback()
    feedback.remember_completed(pending(), turn=1)
    first = begin(feedback)
    assert first["confirmed"] == [{"unit_id": "u0:131073", "origin": "0,0", "dest": "1,0",
                                    "issued_turn": 1, "confirmed_turn": 2, "expires_turn": 5}]
    for turn in (2, 3, 4):
        audit = first if turn == 2 else begin(feedback, turn=turn, completed=turn - 1)
        decision = plan(nonprogress=audit["suppressed"], turn=turn)["decisions"][0]
        row = next(c for c in decision["candidates"] if c["dest"] == "1,0")
        assert row["excluded"] == "observed_nonprogress_cooldown"
        assert row["probability"] == 0
    expired = begin(feedback, turn=5, completed=4)
    assert expired["suppressed"] == {}
    assert expired["expired"] == [{"unit_id": "u0:131073", "dest": "1,0"}]


@pytest.mark.parametrize("action", ["hold", "move"])
def test_current_tactical_override_has_priority_over_cooldown(action):
    feedback = ScoutingFeedback()
    feedback.remember_completed(pending(), turn=1)
    suppression = begin(feedback)["suppressed"]
    order = {"unit_id": "u0:131073", "action": action}
    if action == "move":
        order["dest"] = "1,0"
    decision = plan(directive={"tactical_overrides": [order]},
                    nonprogress=suppression)["decisions"][0]
    assert decision["override"] is True
    assert decision["selected"]["action"] == ("fortify" if action == "hold" else "move_unit")
    if action == "move":
        assert decision["selected"]["args"]["dest"] == "1,0"


def test_all_candidates_suppressed_holds_and_spent_movement_still_stops():
    state = snapshot(radius=1)
    suppression = {"u0:131073": {"origin": "0,0", "destinations": {
        dest: {"issued_turn": 1, "confirmed_turn": 2, "expires_turn": 5}
        for dest in state["get_visible_map"]["tiles"] if dest != "0,0"}}}
    decision = plan(state, nonprogress=suppression)["decisions"][0]
    assert decision["selected"]["action"] == "fortify"
    state["get_units"][0]["movement"] = 0
    assert plan(state, nonprogress=suppression)["decisions"][0]["reason"] == "movement_spent"


def test_feedback_retains_at_most_six_adjacent_destinations_and_sixteen_units():
    feedback = ScoutingFeedback()
    state = snapshot([unit(f"u0:{i}") for i in range(MAX_UNITS + 4)])
    destinations = ["1,0", "1,-1", "0,-1", "-1,0", "-1,1", "0,1"]
    for turn, dest in enumerate(destinations, 1):
        executions = [pending(u["unit_id"], dest=dest)["execution"][0]
                      for u in state["get_units"]]
        remembered = feedback.remember_completed({"execution": executions}, turn=turn)
        assert len(remembered) == MAX_UNITS
        audit = begin(feedback, state, turn + 1, turn)
        assert len(audit["suppressed"]) == MAX_UNITS
        assert all(len(row["destinations"]) <= 6 for row in audit["suppressed"].values())
    # A nonadjacent attempt is not added to adjacent-target history.
    assert feedback.remember_completed(pending(dest="9,9"), turn=7) == []


def test_feedback_plan_deterministic_across_projected_iteration_order():
    left, right = ScoutingFeedback(), ScoutingFeedback()
    for f in (left, right):
        f.remember_completed(pending(), turn=1)
    state = snapshot()
    reversed_state = copy.deepcopy(state)
    reversed_state["get_visible_map"]["tiles"] = dict(reversed(list(
        state["get_visible_map"]["tiles"].items())))
    a, b = begin(left, state), begin(right, reversed_state)
    assert a == b
    assert plan(state, nonprogress=a["suppressed"]) == plan(
        reversed_state, nonprogress=b["suppressed"])


class ClosedLoopFacade(FrozenClosureFacade):
    """Two observed adjacent candidates; one restores allowance without moving."""
    def __init__(self):
        super().__init__()
        self.blocked_dest = "-1,0"

    async def get_visible_map(self):
        self.calls.append("map")
        return {"tiles": {c: {"terrain": "PLAINS"} for c in ["0,0", "-1,0", "1,0"]}}

    async def move_unit(self, unit_id, dest, **kwargs):
        self.calls.append(("move_unit", unit_id, dest))
        row = next(u for u in self.units if u["unit_id"] == unit_id)
        if dest == self.blocked_dest:
            row["movement"] = 2
        else:
            row.update(coord=dest, movement=0, fortified=False)
        return {"status": "accepted", "result": {"detail": "not canonical projected position"}}


def controller_fixture():
    model = FakeModel([[use("submit_directive", {
        "scouting": {"unit_types": ["WARRIOR"], "selection": "best"}})]])
    runtime = LLMAgentRuntime.build(AgentProfile(
        "fixture", 0, "llm", 7, llm=LLMSpec("http://unused", "UNUSED", "fake")), client=model)
    audit = []
    controller = StrategicController("nonprogress-fixture", audit=audit.append,
                                     opening_units_frozen=True)
    runtime.configure_strategic_controller(controller)
    return controller, runtime, model, ClosedLoopFacade(), audit


async def test_real_controller_confirms_nonprogress_then_alternative_on_quiet_turn():
    controller, runtime, model, facade, audit = controller_fixture()
    for turn in (1, 2, 3):
        facade.turn = turn
        facade.units[0]["movement"] = 0
        prior_calls, prior_posts = len(facade.calls), model.posts_sent
        runtime.begin_turn(turn)
        await runtime.take_turn(facade)
        calls = facade.calls[prior_calls:]
        assert sum(isinstance(c, tuple) and c[0] == "move_unit" for c in calls) == 1
        assert model.posts_sent - prior_posts == int(turn == 1)
        assert calls[-1] == "end_turn"
        if turn == 1:
            assert facade.units[0]["coord"] == "0,0"
            assert ("fortify", "u0:1") in calls
            assert calls.count("end_turn") == 2  # completeness repair, strict closure
        elif turn == 2:
            assert ("move_unit", "u0:1", "1,0") in calls
            assert facade.units[0]["coord"] == "1,0"
    graphs = [row["graph"] for row in audit if row["audit"] == "strategy_graph"]
    first = graphs[0]["execution"][0]
    assert first["status"] == "accepted" and first["observation_changed"] is True
    assert first["position_changed"] is False and first["destination_observed"] is False
    assert first["movement_allowance_changed"] is True
    assert first["movement_outcome"] == "submitted_displacement_unconfirmed"
    assert graphs[1]["nonprogress_feedback"]["confirmed"][0]["dest"] == "-1,0"
    assert graphs[1]["execution"][0]["position_changed"] is True
    assert graphs[2]["nonprogress_feedback"]["suppressed"] == {}
    assert model.posts_sent == 1
    assert controller._last_turn == 3


async def test_failed_closure_never_commits_pending_confirmation():
    controller, runtime, _, facade, _ = controller_fixture()

    async def fail_close(_facade):
        raise MatchAborted("closure fixture remains open")

    runtime._close_turn = fail_close
    runtime.begin_turn(1)
    with pytest.raises(MatchAborted, match="closure fixture"):
        await runtime.take_turn(facade)
    assert controller._scouting_feedback._pending == {}
    assert controller._last_turn == 0
    assert controller._failed is True


async def test_controller_gap_stops_before_new_actions_or_provider_calls():
    controller, runtime, model, facade, _ = controller_fixture()
    runtime.begin_turn(1)
    await runtime.take_turn(facade)
    calls = len(facade.calls)
    runtime.begin_turn(3)
    with pytest.raises(MatchAborted, match="skipped turns"):
        await runtime.take_turn(facade)
    assert len(facade.calls) == calls
    assert model.posts_sent == 1
