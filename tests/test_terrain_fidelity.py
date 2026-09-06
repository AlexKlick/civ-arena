"""Native terrain is observational data, never a replacement movement class."""
import copy
import json
from types import SimpleNamespace

import pytest

from civ_arena.agents.llm.context_curator import CONTEXT_MARKER, ContextCurator
from civ_arena.agents.llm.terrain_evidence import cold_biome_evidence
from civ_arena.arena.referee import MatchAborted
from civ_arena.arena.visibility import VisibilityPolicy
from civ_arena.game.adapter import ObserveKind, ObserveRequest
from civ_arena.game.civ6 import response_parser
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from civ_arena.game.terrain_metadata import native_terrain
from test_context_curator import World


def parse(token, key='0|0'):
    return response_parser.parse_visible_map([
        'VMAP|3', 'TURN|7', f'TILEROW|{key}|{token}|true|0|', '---END---'])['tiles']


@pytest.mark.parametrize('token,normalized,biome,hills', [
    ('TERRAIN_TUNDRA', 'PLAINS', 'TUNDRA', False),
    ('TERRAIN_PLAINS', 'PLAINS', 'PLAINS', False),
    ('TERRAIN_SNOW', 'DESERT', 'SNOW', False),
    ('TERRAIN_DESERT', 'DESERT', 'DESERT', False),
    ('TERRAIN_TUNDRA_HILLS', 'HILL', 'TUNDRA', True),
    ('TERRAIN_SNOW_HILLS', 'HILL', 'SNOW', True),
    ('TERRAIN_DESERT_HILLS', 'HILL', 'DESERT', True),
    ('GRASS_HILLS', 'HILL', 'GRASS', True),
])
def test_native_biome_survives_lossy_movement_normalization(token, normalized, biome, hills):
    tile = parse(token)['0,0']
    assert tile['terrain'] == normalized
    assert tile['native_terrain'] == {'type': token, 'biome': biome, 'hills': hills}


@pytest.mark.parametrize('token', ['?', 'bad words', 'TERRAIN_' + 'A' * 10000,
                                    'TERRAIN_WEIRD_HILLS', 'TERRAIN_λ'])
def test_unknown_or_malformed_native_vocabulary_is_bounded_without_invented_biome(token):
    tile = parse(token)['0,0']
    assert tile['terrain'] == 'PLAINS'
    assert tile['native_terrain']['biome'] is None and tile['native_terrain']['hills'] is None
    assert len(json.dumps(tile['native_terrain'])) < 130
    assert tile['native_terrain']['type'] == ('TERRAIN_WEIRD_HILLS'
                                              if token == 'TERRAIN_WEIRD_HILLS' else None)


def test_projection_keeps_static_metadata_but_never_hidden_owner_or_unseen_tile():
    tile = {**parse('TERRAIN_TUNDRA')['0,0'], 'owner': 1, 'city': 'secret_city'}
    tile['native_terrain']['hidden_owner'] = 99
    doc = {'turn': 7, 'tiles': {'0,0': tile, '0,1': copy.deepcopy(tile),
                               '0,2': copy.deepcopy(tile)}}
    projected = VisibilityPolicy().project(doc, 'visible_map', 0,
                                          frozenset({'0,0'}), frozenset({'0,1'}))
    assert set(projected['tiles']) == {'0,0', '0,1'}
    assert projected['tiles']['0,1'] == {'coord': '0,1', 'terrain': 'PLAINS',
                                        'native_terrain': native_terrain('TERRAIN_TUNDRA')}
    assert projected['tiles']['0,0']['owner_id'] == 1
    assert 'hidden_owner' not in json.dumps(projected)
    projected['tiles']['0,1']['native_terrain']['biome'] = 'DESERT'
    assert tile['native_terrain']['biome'] == 'TUNDRA'


def test_simulator_terrain_has_no_fabricated_native_metadata():
    doc = {'turn': 1, 'tiles': {'0,0': {'terrain': 'PLAINS', 'owner': 0, 'city': ''}}}
    projected = VisibilityPolicy().project(doc, 'visible_map', 0,
                                          frozenset({'0,0'}), frozenset())
    assert 'native_terrain' not in projected['tiles']['0,0']
    evidence = cold_biome_evidence(projected['tiles'], '0,1')
    assert evidence['sectors']['south'] == {'known': 1, 'cold': 0, 'noncold': 0, 'unknown': 1}
    assert evidence['map_edge_hypothesis'] is None


async def test_adapter_remembers_native_type_without_refetching_hidden_metadata(monkeypatch):
    units = [{'owner': 0, 'q': 0, 'r': 0}]
    monkeypatch.setattr(response_parser, 'parse_units', lambda *a, **kw: copy.deepcopy(units))
    monkeypatch.setattr(response_parser, 'parse_cities', lambda *a, **kw: [])
    map_queries = []
    async def read(lua, **kwargs):
        if 'TILEROW' in lua:
            map_queries.append(lua)
            q, r = units[0]['q'], units[0]['r']
            terrain = 'TERRAIN_TUNDRA' if q == 0 else 'TERRAIN_DESERT'
            return ['VMAP|3', 'TURN|7', f'TILEROW|{q}|{r}|{terrain}|true|1|old_city']
        return []
    async def write(lua, **kwargs):
        return []
    adapter = FireTunerAdapter('unused.invalid', 0)
    adapter._conn = SimpleNamespace(execute_read=read, execute_write=write)
    first = await adapter.observe(ObserveRequest(kind=ObserveKind.VISIBLE_MAP, player_id=0))
    first['tiles']['0,0']['native_terrain']['biome'] = 'DESERT'
    assert adapter._map_vis[0]['tiles']['0,0']['native_terrain']['biome'] == 'TUNDRA'
    first['tiles']['0,0']['native_terrain']['biome'] = 'TUNDRA'
    units[0].update(q=10, r=10)
    second = await adapter.observe(ObserveRequest(kind=ObserveKind.VISIBLE_MAP, player_id=0))
    assert second['tiles']['0,0'] == {'terrain': 'PLAINS',
                                      'native_terrain': native_terrain('TERRAIN_TUNDRA')}
    assert first['tiles']['0,0']['native_terrain'] == second['tiles']['0,0']['native_terrain']
    assert 'owner' not in second['tiles']['0,0'] and 'city' not in second['tiles']['0,0']
    assert len(map_queries) == 2 and '{0,0}' not in map_queries[1]
    assert adapter.visibility_for(1) == (frozenset(), frozenset())


async def test_adapter_rejects_unrequested_tile_before_populating_memory(monkeypatch):
    monkeypatch.setattr(response_parser, 'parse_units', lambda *a, **kw: [])
    monkeypatch.setattr(response_parser, 'parse_cities', lambda *a, **kw: [])
    async def read(lua, **kwargs):
        return ['VMAP|3', 'TURN|7', 'TILEROW|99|99|TERRAIN_SNOW|false|1|hidden'] \
            if 'TILEROW' in lua else []
    adapter = FireTunerAdapter('unused.invalid', 0)
    adapter._conn = SimpleNamespace(execute_read=read, execute_write=read)
    with pytest.raises(ValueError, match='unrequested coordinates'):
        await adapter.observe(ObserveRequest(kind=ObserveKind.VISIBLE_MAP, player_id=0))
    assert adapter.visibility_for(0) == (frozenset(), frozenset())


@pytest.mark.parametrize('direction,sign', [('north', 1), ('south', -1)])
def test_cold_direction_is_observed_concentration_not_unexplored_warmth(direction, sign):
    tiles = {f'0,{sign}': {'native_terrain': native_terrain('TERRAIN_TUNDRA_HILLS')},
             f'1,{sign}': {'native_terrain': native_terrain('TERRAIN_SNOW')},
             f'0,{-sign}': {'native_terrain': native_terrain('TERRAIN_PLAINS')},
             f'1,{-sign}': {'terrain': 'PLAINS'}}
    evidence = cold_biome_evidence(tiles, '0,0')
    opposite = 'south' if direction == 'north' else 'north'
    assert evidence['axis'] == 'increasing_r_is_north'
    assert evidence['sectors'][direction] == {'known': 2, 'cold': 2, 'noncold': 0, 'unknown': 0}
    assert evidence['sectors'][opposite] == {'known': 2, 'cold': 0, 'noncold': 1, 'unknown': 1}
    assert evidence['map_edge_hypothesis'] == f'possible_{direction}ern_periphery_unverified'
    del tiles[f'0,{-sign}']
    assert cold_biome_evidence(tiles, '0,0')['map_edge_hypothesis'] is None


async def test_native_metadata_and_directional_summary_fit_the_complete_curated_budget():
    world = World()
    original = world.get_visible_map
    async def observed_map():
        doc = await original()
        for key, tile in doc['tiles'].items():
            tile['native_terrain'] = native_terrain(
                'TERRAIN_TUNDRA_HILLS' if int(key.split(',')[1]) > 21 else 'TERRAIN_PLAINS')
        return doc
    world.get_visible_map = observed_map
    curator = ContextCurator(world, 0, 8000)
    await curator.refresh()
    rendered = curator.render()
    assert len(rendered) <= 8000
    doc = json.loads(rendered[len(CONTEXT_MARKER):])
    assert doc['cold_biome_evidence']['reference_coord'] == '3,21'
    assert doc['cold_biome_evidence']['sectors']['north']['cold'] == 12
    assert any(row['native_terrain']['biome'] == 'TUNDRA' for row in doc['terrain'])
    curator.budget = 500
    with pytest.raises(MatchAborted, match='critical owned state'):
        curator.render()


def test_coast_is_not_warm_land_evidence_for_a_cold_edge_hypothesis():
    tiles = {'0,1': {'native_terrain': native_terrain('TERRAIN_TUNDRA')},
             '0,-1': {'native_terrain': native_terrain('TERRAIN_COAST')}}
    evidence = cold_biome_evidence(tiles, '0,0')
    assert evidence['sectors']['south']['unknown'] == 1
    assert evidence['map_edge_hypothesis'] is None
