"""Dispersed owned scouts must not overflow a request through repeated map keys."""
import copy
import json
from pathlib import Path

import pytest

from civ_arena.agents.llm import context_curator
from civ_arena.agents.llm.context_curator import CONTEXT_MARKER, ContextCurator
from civ_arena.agents.llm.decision_packet import decision_snapshot
from civ_arena.agents.llm.runtime import LLMAgentRuntime
from civ_arena.agents.llm.strategic_controller import StrategicController
from civ_arena.agents.llm.terrain_context import (
    compact_entities,
    compact_terrain,
    entity_rows,
    terrain_rows,
)
from civ_arena.agents.runtime import AgentProfile
from civ_arena.arena.referee import MatchAborted
from civ_arena.config import LLMSpec
from civ_arena.game.sim.state import tiles_within
from fakes import FakeModel, use


class RetainedProjection:
    def __init__(self):
        fixture = Path(__file__).parent / 'fixtures' / 'context_turn28.json'
        self.state = json.loads(fixture.read_text())['state']
        self.calls = []

    def __getattr__(self, name):
        async def read(**kwargs):
            self.calls.append((name, kwargs))
            return copy.deepcopy(self.state[name])
        return read


def test_compact_terrain_roundtrip_preserves_projection_and_absence():
    native = {'type': 'TERRAIN_TUNDRA_HILLS', 'biome': 'TUNDRA', 'hills': True}
    doc = {'terrain': [
        {'coord': '0,0', 'terrain': 'HILL', 'native_terrain': native,
         'owner_id': -1, 'city_id': ''},
        {'coord': '0,1', 'terrain': 'HILL', 'native_terrain': native},
        {'coord': '1,0', 'terrain': 'PLAINS', 'owner_id': 0, 'city_id': 'c0:123'},
        {'coord': '1,1', 'terrain': 'PLAINS'}], 'terrain_omitted': 300}
    before = copy.deepcopy(doc)
    compact = compact_terrain(doc)
    assert compact['terrain_native_palette'] == [native]
    assert terrain_rows(compact) == doc['terrain']
    assert compact['terrain_omitted'] == 300
    assert compact == compact_terrain(doc)
    assert doc == before
    assert terrain_rows(doc) == doc['terrain']


async def test_recorded_turn28_fits_without_dropping_owned_actors_or_adjacent_tiles(monkeypatch):
    facade = RetainedProjection()
    curator = ContextCurator(facade, 1, 7000)
    await curator.refresh()
    with monkeypatch.context() as patch:
        patch.setattr(context_curator, 'compact_terrain', lambda doc: doc)
        patch.setattr(context_curator, 'compact_entities', lambda doc: doc)
        with pytest.raises(MatchAborted, match='context budget'):
            curator.render()
    before = copy.deepcopy(curator.state)
    encoded = curator.render()
    assert len(encoded) <= 7000
    doc = json.loads(encoded.removeprefix(CONTEXT_MARKER))
    assert doc['terrain_encoding'] == 'column_rows_v1'
    assert {u['unit_id'] for u in doc['own_units']} == {
        u['unit_id'] for u in facade.state['get_units']}
    anchors = [u['coord'] for u in doc['own_units'] + doc['own_cities']]
    expected = {key for key in curator.state['get_visible_map']['tiles']
                if any(context_curator.distance(key, at) <= 1 for at in anchors)}
    expanded = terrain_rows(doc)
    assert expected <= {row['coord'] for row in expanded}
    for row in expanded:
        assert row == {'coord': row['coord'],
                       **curator.state['get_visible_map']['tiles'][row['coord']]}
    assert len(expanded) + doc['terrain_omitted'] == len(
        curator.state['get_visible_map']['tiles'])
    assert curator.state == before
    assert curator.render() == encoded


async def test_entire_strategy_request_including_changes_stays_under_existing_cap():
    facade = RetainedProjection()
    spec = LLMSpec('http://unused.invalid', 'UNUSED', 'fixture', max_result_chars=8000)
    model = FakeModel([[use('submit_directive', {'version': 1})]])
    runtime = LLMAgentRuntime.build(AgentProfile('b', 1, 'llm', 22, llm=spec), client=model)
    runtime.begin_turn(28)
    curator = ContextCurator(facade, 1, 8000)
    await curator.refresh()
    controller = StrategicController('compact-context')
    prior = decision_snapshot(curator, 24)
    for uid in ('u1:327683', 'u1:393220'):
        del prior['units'][uid]
    controller._decision_observation = prior
    calls = copy.deepcopy(facade.calls)
    await controller._decide(runtime, curator, ['research_choice_required'])
    context = model.requests[0]['messages'][0]['content']
    assert len(context) <= 8000
    metadata, state = context.split('\n', 1)
    changes = json.loads(metadata)['decision_packet']['changes_since_strategy_request']
    assert changes['units']['newly_owned']['items'] == ['u1:327683', 'u1:393220']
    assert json.loads(state.removeprefix(CONTEXT_MARKER))['terrain_encoding'] == 'column_rows_v1'
    assert model.posts_sent == 1
    assert facade.calls == calls


async def test_unrepresentable_critical_roster_still_fails_before_provider():
    facade = RetainedProjection()
    curator = ContextCurator(facade, 1, 1500)
    await curator.refresh()
    with pytest.raises(MatchAborted, match='context budget'):
        curator.render()


async def test_supported_sixteen_unit_roster_keeps_every_adjacent_tile_and_actor():
    facade = RetainedProjection()
    original = facade.state['get_units'][0]
    units, tiles = [], {}
    for i in range(16):
        units.append({**original, 'unit_id': f'u1:{100000+i}', 'coord': f'{i*5},0'})
        for q, r in tiles_within((i*5, 0), 1):
            tiles[f'{q},{r}'] = {'coord': f'{q},{r}', 'terrain': 'PLAINS',
                'native_terrain': {'type': 'TERRAIN_PLAINS', 'biome': 'PLAINS', 'hills': False},
                'owner_id': -1, 'city_id': ''}
    facade.state['get_units'] = units
    facade.state['get_visible_map']['tiles'] = tiles
    curator = ContextCurator(facade, 1, 6500)  # leaves 1500 for strategy metadata
    await curator.refresh()
    rendered = curator.render()
    assert len(rendered) <= 6500
    doc = json.loads(rendered.removeprefix(CONTEXT_MARKER))
    assert len(entity_rows(doc, 'own_units')) == 16
    assert {u['unit_id'] for u in entity_rows(doc, 'own_units')} == {
        u['unit_id'] for u in units}
    assert {row['coord'] for row in terrain_rows(doc)} == set(tiles)


def test_actor_tables_preserve_absence_false_zero_and_explicit_null():
    doc = {'own_units': [{'unit_id': 'u0:1', 'movement': 0, 'is_barbarian': False},
                         {'unit_id': 'u0:2', 'movement': 1}],
           'own_cities': [{'city_id': 'c0:1', 'hp': None}],
           'visible_foreign_units': [{'unit_id': 'u63:1', 'is_barbarian': True}],
           'visible_foreign_cities': []}
    before = copy.deepcopy(doc)
    packed = compact_entities(doc)
    assert {kind: entity_rows(packed, kind) for kind in doc} == doc
    assert 'own_cities' not in packed['entity_columns']
    assert packed == compact_entities(doc)
    assert doc == before


async def test_source_classified_visible_barbarian_reaches_model_context():
    facade = RetainedProjection()
    facade.state['get_units'].append({'unit_id': 'u63:1', 'owner_id': 63,
        'coord': '43,12', 'type': 'WARRIOR', 'is_barbarian': True, 'hp_bucket': 4})
    curator = ContextCurator(facade, 1, 8000)
    await curator.refresh()
    doc = json.loads(curator.render().removeprefix(CONTEXT_MARKER))
    assert entity_rows(doc, 'visible_foreign_units')[0]['is_barbarian'] is True
