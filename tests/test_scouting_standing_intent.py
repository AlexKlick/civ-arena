"""Persistent scouting roles across real controller/curator/closure transitions."""
from __future__ import annotations

import pytest

from civ_arena.agents.llm.runtime import LLMAgentRuntime
from civ_arena.agents.llm.strategic_controller import StrategicController
from civ_arena.agents.runtime import AgentProfile
from civ_arena.config import LLMSpec
from fakes import FakeModel, use
from test_scouting import plan, snapshot, unit
from test_strategic_controller import Facade


class FrozenClosureFacade(Facade):
    """Projected state double; first move submits without reaching its destination."""
    def __init__(self):
        super().__init__()
        self.turn = 1
        self.units[0].update(type="WARRIOR", movement=0, fortified=False)

    async def move_unit(self, unit_id, dest, **kwargs):
        self.calls.append(("move_unit", unit_id, dest))
        row = next(u for u in self.units if u["unit_id"] == unit_id)
        if self.turn == 1:
            row["movement"] = 2  # restored allowance, request has not moved yet
        else:
            row.update(coord=dest, movement=0, fortified=False)
        return {"status": "accepted", "result": {"detail": "old raw engine position"}}

    async def fortify(self, unit_id, **kwargs):
        self.calls.append(("fortify", unit_id))
        next(u for u in self.units if u["unit_id"] == unit_id).update(
            fortified=True, movement=0)
        return {"status": "accepted"}

    async def end_turn(self):
        self.calls.append("end_turn")
        remaining = [u["unit_id"] for u in self.units if u["movement"] > 0 and not u["fortified"]]
        return ({"status": "rejected", "rejection": "unmoved_units", "unmoved_units": remaining}
                if remaining else {"status": "accepted"})


@pytest.mark.parametrize("first_override", ["move", "hold"])
async def test_assigned_scout_resumes_after_closure_or_expired_hold_without_model_request(
        first_override):
    order = {"unit_id": "u0:1", "action": first_override}
    if first_override == "move":
        order["dest"] = "1,0"
    model = FakeModel([[use("submit_directive", {
        "scouting": {"unit_types": ["WARRIOR"], "selection": "best"},
        "tactical_overrides": [order]})]])
    runtime = LLMAgentRuntime.build(AgentProfile(
        "fixture", 0, "llm", 7, llm=LLMSpec("http://unused", "UNUSED", "fake")), client=model)
    audit = []
    controller = StrategicController("standing-intent-fixture", audit=audit.append,
                                     opening_units_frozen=True)
    runtime.configure_strategic_controller(controller)
    facade = FrozenClosureFacade()
    runtime.begin_turn(1)
    await runtime.take_turn(facade)
    first_calls = list(facade.calls)
    assert facade.units[0]["coord"] == "0,0"
    assert facade.units[0]["fortified"] is True
    assert ("fortify", "u0:1") in first_calls
    assert sum(call == "end_turn" for call in first_calls) == (2 if first_override == "move" else 1)
    if first_override == "hold":
        assert not any(isinstance(call, tuple) and call[0] == "move_unit" for call in first_calls)
        first_graph = next(row["graph"] for row in audit if row["audit"] == "strategy_graph")
        assert first_graph["decisions"][0]["reason"] == "explicit_tactical_hold"

    facade.turn = 2
    # Trusted fresh live lease: actual movement stays zero under the opening freeze.
    facade.units[0]["movement"] = 0
    before_posts = model.posts_sent
    runtime.begin_turn(2)
    await runtime.take_turn(facade)
    second_calls = facade.calls[len(first_calls):]
    assert model.posts_sent - before_posts == 0
    assert model.posts_sent == 1
    assert ("move_unit", "u0:1", "1,0") in second_calls
    assert facade.units[0]["coord"] == "1,0"
    assert facade.units[0]["fortified"] is False
    assert second_calls[-1] == "end_turn"
    second_graph = next(row["graph"] for row in audit
                        if row["audit"] == "strategy_graph" and row["turn"] == 2)
    assert second_graph["decisions"][0]["standing_order_resolution"] == (
        "persistent_scouting_assignment")
    execution = second_graph["execution"][0]
    assert execution["before"]["fortified"] is True  # no fabricated observation
    assert execution["before"]["movement"] == 0
    assert execution["after"]["coord"] == "1,0"


def test_unassigned_fortified_unit_keeps_existing_standing_order():
    warrior = unit(kind="WARRIOR")
    warrior["fortified"] = True
    result = plan(snapshot([warrior]), {"scouting": {"unit_types": ["SCOUT"]}})
    assert result["decisions"][0]["reason"] == "existing_standing_order"
    assert result["decisions"][0]["selected"] is None


def test_assigned_fortified_unit_with_spent_movement_does_not_get_an_extra_attempt():
    warrior = unit(kind="WARRIOR", movement=0)
    warrior["fortified"] = True
    decision = plan(snapshot([warrior]))["decisions"][0]
    assert decision["reason"] == "movement_spent"
    assert decision["selected"] is None


def test_reactivated_scout_still_respects_visible_threat_exclusions():
    warrior = unit(kind="WARRIOR")
    warrior["fortified"] = True
    state = snapshot([warrior, unit("u1:1", coord="1,0", owner=1)])
    decision = plan(state, {"scouting": {"policy": "cautious"}})["decisions"][0]
    assert decision["standing_order_resolution"] == "persistent_scouting_assignment"
    assert decision["selected"]["action"] == "fortify"
    assert all(row["excluded"] for row in decision["candidates"])
