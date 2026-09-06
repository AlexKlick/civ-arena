"""Native evidence shares the strategic request budget, including metadata."""
import json

import pytest

from civ_arena.agents.llm.context_curator import CONTEXT_MARKER, ContextCurator
from civ_arena.agents.llm.runtime import LLMAgentRuntime
from civ_arena.agents.llm.strategic_controller import StrategicController
from civ_arena.agents.runtime import AgentProfile
from civ_arena.arena.referee import MatchAborted
from civ_arena.config import LLMSpec
from civ_arena.game.terrain_metadata import native_terrain
from fakes import FakeModel, use
from test_context_curator import World


@pytest.mark.parametrize('budget', [8000, 1800])
async def test_complete_native_evidence_request_keeps_metadata_inside_budget(budget):
    world = World()
    original = world.get_visible_map
    async def observed_map():
        doc = await original()
        for key, tile in doc['tiles'].items():
            tile['native_terrain'] = native_terrain(
                'TERRAIN_TUNDRA_HILLS' if int(key.split(',')[1]) > 21 else 'TERRAIN_PLAINS')
        return doc
    world.get_visible_map = observed_map
    spec = LLMSpec('http://unused.invalid', 'UNUSED', 'fixture', max_result_chars=budget)
    model = FakeModel([[use('submit_directive', {'version': 1})]])
    runtime = LLMAgentRuntime.build(AgentProfile('a', 0, 'llm', 1, llm=spec), client=model)
    runtime.begin_turn(1)
    curator = ContextCurator(world, 0, budget)
    await curator.refresh()
    controller = StrategicController('native-budget')
    if budget == 1800:
        with pytest.raises(MatchAborted, match='context budget'):
            await controller._decide(runtime, curator, ['initial_strategy'])
        assert model.posts_sent == 0
        return
    await controller._decide(runtime, curator, ['initial_strategy'])
    assert model.posts_sent == 1
    context = model.requests[0]['messages'][0]['content']
    assert len(context) <= budget
    metadata, state = context.split('\n', 1)
    assert json.loads(metadata)['decision_packet']['observed_turn'] == 1
    view = json.loads(state.removeprefix(CONTEXT_MARKER))
    assert view['cold_biome_evidence']['sectors']['north']['cold'] == 12
    assert any(tile['native_terrain']['biome'] == 'TUNDRA' for tile in view['terrain'])
