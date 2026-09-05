"""Live pacing fixtures; no network, desktop, or tuner access."""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

from civ_arena.agents.llm.runtime import LLMAgentRuntime
from civ_arena.agents.runtime import AgentProfile
from civ_arena.arena.referee import MatchAborted
from civ_arena.config import LLMSpec
from fakes import FakeModel, text, use

SPEC = LLMSpec(base_url="http://unused.invalid/v1", api_key_env="UNUSED",
               model_id="fixture", max_tokens=4096, max_tool_rounds=16,
               max_requests_per_match=2000)


class Facade:
    def __init__(self):
        self.calls = []
        self.changes = 0
        self.reject_action = False
        self.closed = False

    async def get_units(self):
        self.calls.append(("get_units", {}))
        return [{"unit_id": "u0:10", "owner_id": 0, "coord": "50,50",
                 "type": "WARRIOR", "movement": 2}]

    async def get_cities(self):
        self.calls.append(("get_cities", {}))
        return [{"city_id": "c0:7", "owner": 0, "coord": "50,50"},
                {"city_id": "c1:7", "owner_id": 1, "coord": "52,50"}]

    async def get_overview(self):
        self.calls.append(("get_overview", {}))
        return {"you": {"gold": 100, "researching": ""}, "changes": self.changes}

    async def get_available_research(self):
        self.calls.append(("get_available_research", {}))
        return [{"tech_id": "POTTERY", "cost": 25}]

    async def get_visible_map(self):
        self.calls.append(("get_visible_map", {}))
        return {"turn": 1, "tiles": {"50,50": {"terrain": "PLAINS", "owner_id": 0},
                                      "51,50": {"terrain": "HILLS"}}}

    async def get_available_production(self, city_id):
        self.calls.append(("get_available_production", {"city_id": city_id}))
        assert city_id == "c0:7", "foreign cities must not be queried"
        return [{"item_id": "SCOUT", "cost": 30}]

    async def fortify(self, unit_id):
        self.calls.append(("fortify", {"unit_id": unit_id}))
        self.changes += 1
        return {"status": "rejected" if self.reject_action else "accepted"}

    async def end_turn(self):
        self.calls.append(("end_turn", {}))
        self.closed = True
        return {"status": "accepted"}

    async def recall_lessons(self, query):
        self.calls.append(("recall_lessons", {"query": query}))
        return {"status": "accepted", "lessons": []}


def runtime(client, *, paced=True, recall=False):
    profile = AgentProfile(agent_id="private-agent-name", player_id=0, policy="llm",
                           seed=9, llm=SPEC)
    rt = LLMAgentRuntime.build(profile, client=client)
    if paced:
        rt.configure_turn_pacing(recall_available=recall)
    rt.begin_turn(1)
    return rt


async def test_briefing_precedes_model_and_prefetches_only_owned_city_options():
    model = FakeModel([[use("fortify", {"unit_id": "u0:10"}), use("end_turn")]])
    facade = Facade()
    await runtime(model).take_turn(facade)
    assert facade.calls[:6] == [
        ("get_visible_map", {}), ("get_units", {}), ("get_cities", {}),
        ("get_overview", {}), ("get_available_production", {"city_id": "c0:7"}),
        ("get_available_research", {})]
    assert facade.calls[6][0] == "fortify" and facade.closed
    request = model.requests[0]
    assert "Controller context" in str(request["messages"][1]["content"])
    assert "private-agent-name" not in json.dumps(request["messages"])
    assert "recall_lessons" not in {tool["name"] for tool in request["tools"]}
    assert SPEC.max_tokens == 4096 and SPEC.max_tool_rounds == 16
    assert SPEC.max_requests_per_match == 2000


async def test_briefing_is_bounded_and_map_prioritizes_visible_owned_position():
    facade = Facade()
    rt = runtime(FakeModel([[use("end_turn")]]))
    visible = {"tiles": {f"{i},0": {"terrain": "PLAINS"} for i in range(100)}}
    visible["tiles"]["50,50"] = {"terrain": "HILLS", "owner_id": 0}
    async def huge_map():
        return visible
    facade.get_visible_map = huge_map
    briefing = await rt._turn_briefing(facade)
    assert len(briefing) <= SPEC.max_result_chars
    assert "terrain_omitted" in briefing
    assert '"coord":"50,50"' in briefing


@pytest.mark.parametrize("failure", ["exception", "rejection"])
async def test_failed_briefing_never_becomes_empty_state_or_model_action(failure):
    facade, model = Facade(), FakeModel([[use("end_turn")]])
    async def bad_read():
        if failure == "exception":
            raise ConnectionError("observation unavailable")
        return {"status": "rejected", "rejection": "lease_expired"}
    facade.get_units = bad_read
    with pytest.raises((ConnectionError, MatchAborted)):
        await runtime(model).take_turn(facade)
    assert model.posts_sent == 0 and not facade.closed


@pytest.mark.parametrize("rejected", [False, True])
async def test_same_batch_read_cache_invalidated_by_any_mutation_attempt(rejected):
    model = FakeModel([[use("get_overview", id="a"), use("get_overview", id="b"),
                        use("fortify", {"unit_id": "u0:10"}),
                        use("get_overview", id="c"), use("end_turn")]])
    facade = Facade()
    facade.reject_action = rejected
    await runtime(model).take_turn(facade)
    # Legacy model reads use curator cache, even within a mutation batch.
    assert sum(name == "get_overview" for name, _ in facade.calls) == 1


async def test_reads_not_cached_across_model_rounds_or_turns():
    model = FakeModel([[use("get_overview")], [use("get_overview"), use("end_turn")]])
    facade, rt = Facade(), runtime(model)
    await rt.take_turn(facade)
    assert sum(name == "get_overview" for name, _ in facade.calls) == 1
    rt.begin_turn(2)
    await rt.take_turn(facade)
    assert sum(name == "get_overview" for name, _ in facade.calls) == 2


async def test_unavailable_recall_never_calls_facade_even_if_model_asks():
    model = FakeModel([[use("recall_lessons", {"query": "opening"})], [use("end_turn")]])
    facade = Facade()
    await runtime(model).take_turn(facade)
    assert not any(name == "recall_lessons" for name, _ in facade.calls)
    result = model.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "unavailable" in result["content"]
    hint = model.requests[1]["messages"][-1]["content"][-1]["text"]
    assert "No game action has been accepted" in hint


async def test_available_recall_retains_real_tool_and_facade_dispatch():
    model = FakeModel([[use("recall_lessons", {"query": "opening"}), use("end_turn")]])
    facade = Facade()
    await runtime(model, recall=True).take_turn(facade)
    assert "recall_lessons" in {tool["name"] for tool in model.requests[0]["tools"]}
    assert any(name == "recall_lessons" for name, _ in facade.calls)


async def test_paced_prose_still_closes_via_existing_completeness_repair():
    model = FakeModel([[text("Finished")]])
    facade = Facade()
    calls = 0
    async def end():
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"status": "rejected", "rejection": "unmoved_units", "unmoved_units": ["u0:10"]}
        facade.closed = True
        return {"status": "accepted"}
    facade.end_turn = end
    await runtime(model).take_turn(facade)
    assert facade.closed and calls == 2
    assert facade.calls[-1] == ("fortify", {"unit_id": "u0:10"})


class BriefingAwareFixture(FakeModel):
    """Fixture decision rule, not an inference about live MiniMax behavior."""
    async def create(self, *, system, messages, tools):
        if self.posts_sent or any("Controller context" in str(m["content"])
                                  for m in messages):
            self.script = [[use("fortify", {"unit_id": "u0:10"}), use("end_turn")]]
        else:
            self.script = [[use("get_units"), use("get_cities"), use("get_overview"),
                            use("get_available_research"), use("get_visible_map"),
                            use("get_available_production", {"city_id": "c0:7"})]]
        return await super().create(system=system, messages=messages, tools=tools)


async def fixture_comparison():
    result = {"scope": "synthetic model fixture; not live latency or MiniMax proof", "variants": {}}
    for paced in (False, True):
        model, facade = BriefingAwareFixture([]), Facade()
        rt = runtime(model, paced=paced)
        before = copy.deepcopy(SPEC)
        started = time.perf_counter_ns()
        await rt.take_turn(facade)
        result["variants"]["paced" if paced else "legacy"] = {
            "model_requests": model.posts_sent,
            "facade_calls": len(facade.calls), "closed": facade.closed,
            "elapsed_ns_non_live": time.perf_counter_ns() - started,
            "accepted_game_actions": facade.changes}
        assert before == SPEC
    return result


async def test_briefing_aware_fixture_saves_request_without_skipping_actions(tmp_path):
    result = await fixture_comparison()
    Path(tmp_path / "fixture-comparison.json").write_text(json.dumps(result, indent=2))
    before, after = (result["variants"][name] for name in ("legacy", "paced"))
    assert (before["model_requests"], after["model_requests"]) == (2, 1)
    assert before["facade_calls"] == after["facade_calls"] == 8
    assert before["accepted_game_actions"] == after["accepted_game_actions"] == 1
    assert before["closed"] and after["closed"]


async def test_real_session_prefetches_owned_city_projection_and_replays(tmp_path):
    from civ_arena.replay import replay_run
    from test_llm_runtime import make_arena

    model = FakeModel([[use("found_city", {"unit_id": "u1"}), use("end_turn")],
                       [use("end_turn")]])
    arena, _ = make_arena(tmp_path, model, max_turns=2)
    arena.runtimes[0].configure_turn_pacing(recall_available=False)
    summary = await arena.run()
    assert summary["aborted"] is None
    production = [r for r in arena.log.records() if r["kind"] == "TOOL_CALL"
                  and r.get("agent_id") == "roman" and r.get("tool") == "get_available_production"]
    assert production and all(r["turn"] == 2 for r in production)
    assert all(arena.adapter.state.city(r["args"]["city_id"])["owner"] == 0 for r in production)
    assert production[0]["args"]["city_id"] in json.dumps(
        model.requests[1]["messages"][1]["content"])
    replay = await replay_run(arena.run_dir, arena.spec, tmp_path / "replay")
    assert replay["identical"], replay


async def test_opening_visibility_refresh_precedes_entity_projection():
    facade = Facade()
    original_units = facade.get_units
    original_map = facade.get_visible_map
    refreshed = False

    async def refresh_map():
        nonlocal refreshed
        refreshed = True
        return await original_map()

    async def projected_units():
        assert refreshed, "stale sight must not be used for projected entity reads"
        return await original_units()

    facade.get_visible_map = refresh_map
    facade.get_units = projected_units
    await runtime(FakeModel([[use("end_turn")]])).take_turn(facade)
    assert facade.calls[0][0] == "get_visible_map"
