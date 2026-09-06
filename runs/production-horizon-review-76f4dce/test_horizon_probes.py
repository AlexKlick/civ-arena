"""Read-only probes against frozen integrated source; no native or network clients."""
import copy
import inspect
import json
from unittest.mock import AsyncMock

import pytest

from civ_arena.agents.llm.client import ModelReply
from civ_arena.agents.llm.runtime import LLMAgentRuntime
from civ_arena.agents.llm.strategic_controller import StrategicController
from civ_arena.agents.production_policy import choose_production
from civ_arena.agents.runtime import AgentProfile
from civ_arena.agents.strategy_directive import validate_directive
from civ_arena.arena.referee import MatchAborted
from civ_arena.config import LLMSpec
from civ_arena.game.civ6 import lua_translator, response_parser
from civ_arena.session.tools import set_city_production


def owned(item="SCOUT", uid="u0:1", owner=0, **fields):
    return {"unit_id": uid, "owner": owner, "type": item, "coord": "0,0", "hp": 100,
            "movement": 0, **fields}


def city(cid="c0:1", queue=None):
    return {"city_id": cid, "owner": 0, "coord": "0,0", "population": 3,
            "production_queue": queue or []}


def strategy(target=None):
    doc = {"production_preferences": ["SCOUT"]}
    if target is not None:
        doc["unit_targets"] = {"SCOUT": target}
    return doc


class Model:
    def __init__(self, replies):
        self.replies = replies
        self.posts_sent = 0
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        value = self.replies[min(self.posts_sent, len(self.replies) - 1)]
        self.posts_sent += 1
        return ModelReply([{"type": "tool_use", "id": str(self.posts_sent),
                            "name": "submit_directive", "input": value}],
                          "tool_use", "offline-probe", 1, 1)


class Facade:
    def __init__(self, count=2, cities=None):
        self.units = [owned(uid=f"u0:{i}") for i in range(count)]
        self.cities = cities or [city()]
        self.actions = []
        self.catalog_queries = []
        self.catalog = [{"item_id": "SCOUT", "kind": "unit", "cost": 30, "turns": 3}]

    async def get_visible_map(self):
        return {"tiles": {"0,0": {"terrain": "PLAINS"}, "1,0": {"terrain": "PLAINS"}}}

    async def get_units(self):
        return copy.deepcopy(self.units)

    async def get_cities(self):
        return copy.deepcopy(self.cities)

    async def get_overview(self):
        return {"you": {"researching": "MINING"}, "public": {}}

    async def get_available_production(self, city_id):
        self.catalog_queries.append(city_id)
        return copy.deepcopy(self.catalog)

    async def set_city_production(self, city_id, item_id):
        self.actions.append(("set_city_production", city_id, item_id))
        next(c for c in self.cities if c["city_id"] == city_id)["production_queue"] = [item_id]
        return {"status": "accepted"}

    async def end_turn(self):
        self.actions.append(("end_turn",))
        return {"status": "accepted"}


def controller(monkeypatch, replies):
    monkeypatch.setattr("civ_arena.agents.llm.strategic_controller.run_scouting",
                        AsyncMock(return_value={"decisions": [], "execution": []}))
    model = Model(replies)
    llm = LLMSpec("http://unused", "UNUSED", "offline-probe", max_result_chars=8000)
    runtime = LLMAgentRuntime.build(AgentProfile("probe", 0, "llm", 1, llm=llm), client=model)
    runtime.begin_turn(1)
    records = []
    return StrategicController("readonly-horizon-probe", audit=records.append), runtime, model, records


async def test_current_model_path_can_raise_owned_plus_queued_target(monkeypatch):
    agent, runtime, model, records = controller(monkeypatch, [strategy(), strategy(4)])
    facade = Facade(cities=[city(), city("c0:2", ["SCOUT"])])
    await agent.take_turn(runtime, facade)
    assert model.posts_sent == 2
    metadata, rendered = model.requests[-1]["messages"][0]["content"].split("\n", 1)
    assert json.loads(metadata)["reasons"] == ["production_targets_satisfied"]
    assert '"production_options"' in rendered and '"SCOUT"' in rendered
    assert '"production_queue":["SCOUT"]' in rendered
    assert agent.directive["unit_targets"] == {"SCOUT": 4}
    assert facade.actions == [("set_city_production", "c0:1", "SCOUT"), ("end_turn",)]
    policy = [r["production_policy"] for r in records if r.get("production_policy")][-1]
    candidate = next(c for c in policy["candidates"] if c["item_id"] == "SCOUT")
    assert (candidate["owned"], candidate["queued"], candidate["target"]) == (2, 1, 4)


async def test_source_limit_32_cannot_be_lifted_by_model(monkeypatch):
    agent, runtime, model, records = controller(monkeypatch, [strategy(32), strategy(33)])
    facade = Facade(count=32)
    with pytest.raises(MatchAborted, match="invalid_args/schema_or_ownership"):
        await agent.take_turn(runtime, facade)
    assert model.posts_sent == 3  # initial + refresh + one format repair
    assert not facade.actions
    assert records[-1]["audit"] == "strategy_failed"


async def test_active_unknown_queue_alone_is_preserved_without_catalog_request(monkeypatch):
    agent, runtime, model, records = controller(monkeypatch, [strategy()])
    facade = Facade(cities=[city(queue=["UNKNOWN_PRODUCTION_-123"])])
    await agent.take_turn(runtime, facade)
    assert not facade.catalog_queries
    assert facade.actions == [("end_turn",)]


@pytest.mark.parametrize("hash_value,raises", [(123, False), (-123, True)])
async def test_unknown_active_hash_can_abort_different_idle_city(monkeypatch, hash_value, raises):
    agent, runtime, model, records = controller(monkeypatch, [strategy()])
    parsed = response_parser.parse_cities([
        f"CITYROW|c0:1|0|Existing|0|0|3|UNKNOWN_PRODUCTION_{hash_value}",
        "CITYROW|c0:2|0|Idle|1|0|3|-"], qualified=True)
    for c in parsed:
        c["coord"] = f"{c['q']},{c['r']}"
    facade = Facade(count=0, cities=parsed)
    if raises:
        with pytest.raises(ValueError, match="invalid exact item ID"):
            await agent.take_turn(runtime, facade)
        assert not facade.actions
    else:
        await agent.take_turn(runtime, facade)
        assert facade.actions == [("set_city_production", "c0:2", "SCOUT"), ("end_turn",)]
    assert facade.catalog_queries == ["c0:2"]


@pytest.mark.parametrize("kind", ["district", "project"])
def test_native_parser_has_no_district_or_project_contract(kind):
    with pytest.raises(ValueError, match="unknown production kind"):
        response_parser.parse_available_production([f"ITEMROW|{kind}|CAMPUS|60|5"])


def test_generated_catalog_and_action_have_only_unit_and_building_kinds():
    read = lua_translator.available_production_read("c0:1")
    action = lua_translator.set_city_production("c0:1", "CAMPUS")
    assert "for row in GameInfo.Units()" in read
    assert "for row in GameInfo.Buildings()" in read
    assert "GameInfo.Districts" not in read + action
    assert "GameInfo.Projects" not in read + action
    assert "PARAM_X" not in action and "PARAM_Y" not in action
    assert set(inspect.signature(set_city_production).parameters) == {
        "ctx", "city_id", "item_id", "idempotency_key"}


def test_defender_count_is_opening_allowlist_not_all_owned_combat_units():
    state = {"get_units": [owned("CROSSBOWMAN", f"u0:{i}") for i in range(3)] + [
        owned("WARRIOR", "u63:1", owner=63, coord="1,0", is_barbarian=True)],
        "get_cities": [city()]}
    directive = validate_directive({}, player_id=0,
                                   owned_unit_ids={f"u0:{i}" for i in range(3)})
    result = choose_production(state, player_id=0, city_id="c0:1", directive=directive,
                               options=[{"item_id": "ARCHER", "kind": "unit"}])
    assert result["defenders_owned_queued_reserved"] == 0
    assert result["item_id"] == "ARCHER"
    assert result["reason"] == "confirmed_nearby_barbarian_defense"


def test_modern_only_catalog_cannot_gain_default_defensive_priority():
    state = {"get_units": [owned("CROSSBOWMAN"), owned("WARRIOR", "u63:1", owner=63,
        coord="1,0", is_barbarian=True)], "get_cities": [city()]}
    directive = validate_directive({}, player_id=0, owned_unit_ids={"u0:1"})
    result = choose_production(state, player_id=0, city_id="c0:1", directive=directive,
                               options=[{"item_id": "CROSSBOWMAN", "kind": "unit"}])
    assert result["item_id"] is None
    assert result["candidates"][0]["target"] == 1
