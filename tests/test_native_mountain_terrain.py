"""Native mountain rows remain impassable through projection and scouting."""
from unittest.mock import ANY, AsyncMock

import pytest

from civ_arena.agents.llm.terrain_evidence import cold_biome_evidence
from civ_arena.agents.scouting import plan_scouting, run_scouting
from civ_arena.arena.visibility import VisibilityPolicy
from civ_arena.game.civ6.response_parser import parse_visible_map
from test_scouting import IDENTITY, snapshot

BIOMES = ('GRASS', 'PLAINS', 'DESERT', 'TUNDRA', 'SNOW')
NEIGHBORS = ('1,0', '0,1', '-1,1', '-1,0', '0,-1', '1,-1')


@pytest.mark.parametrize('biome', BIOMES)
@pytest.mark.parametrize('prefix', ['TERRAIN_', ''])
def test_exact_native_mountain_maps_to_impassable_class_with_original_provenance(biome, prefix):
    token = f'{prefix}{biome}_MOUNTAIN'
    parsed = parse_visible_map(['VMAP|3', 'TURN|1', f'TILEROW|1|0|{token}|true|-1|'])
    assert parsed['unknown_terrain'] == 0
    assert parsed['tiles']['1,0'] == {
        'terrain': 'MOUNTAIN', 'owner': -1, 'city': '',
        'native_terrain': {'type': token, 'biome': biome, 'hills': False},
    }


@pytest.mark.parametrize('name', ['WEIRD_MOUNTAIN', 'TUNDRA_MOUNTAIN_HILLS',
                                  'TUNDRA_HILLS_MOUNTAIN', 'COAST_MOUNTAIN',
                                  'MOUNTAIN', 'TUNDRA_MOUNTAINS'])
def test_unknown_mountain_like_names_are_not_invented_base_terrain(name):
    token = f'TERRAIN_{name}'
    parsed = parse_visible_map(['VMAP|3', 'TURN|1', f'TILEROW|1|0|{token}|false||'])
    assert parsed['unknown_terrain'] == 1
    # Retain the existing unknown-name policy; do not guess from a suffix.
    assert parsed['tiles']['1,0'] == {
        'terrain': 'PLAINS', 'native_terrain': {'type': token, 'biome': None, 'hills': None},
    }


def projected_state(biome, remembered, safe_destination=True):
    rows = ['VMAP|3', 'TURN|1', 'TILEROW|0|0|TERRAIN_GRASS|true|-1|']
    for key in NEIGHBORS:
        terrain = 'TERRAIN_PLAINS' if safe_destination and key == '1,0' else (
            f'TERRAIN_{biome}_MOUNTAIN')
        visible = 'false' if remembered and terrain.endswith('_MOUNTAIN') else 'true'
        rows.append(f'TILEROW|{key.replace(",", "|")}|{terrain}|{visible}|-1|')
    parsed = parse_visible_map(rows)
    projected = VisibilityPolicy().project(
        parsed, 'visible_map', 0, parsed['visible'],
        frozenset(parsed['tiles']) - parsed['visible'])
    state = snapshot(radius=1)
    state['get_visible_map'] = projected
    return state


@pytest.mark.parametrize('biome', BIOMES)
@pytest.mark.parametrize('remembered', [False, True])
async def test_projected_native_mountains_are_excluded_from_routine_scouting(biome, remembered):
    state = projected_state(biome, remembered)
    directive = {'scouting': {'selection': 'best'}}
    graph = plan_scouting(state, directive=directive, **IDENTITY)
    decision = graph['decisions'][0]
    assert decision['selected']['args']['dest'] == '1,0'
    for candidate in decision['candidates']:
        if candidate['dest'] == '1,0':
            assert candidate['excluded'] is None and candidate['probability'] == 1
        else:
            tile = state['get_visible_map']['tiles'][candidate['dest']]
            assert tile['terrain'] == 'MOUNTAIN'
            assert tile['native_terrain'] == {
                'type': f'TERRAIN_{biome}_MOUNTAIN', 'biome': biome, 'hills': False}
            if remembered:
                assert 'owner_id' not in tile and 'city_id' not in tile
            assert candidate['excluded'] == 'unsupported_or_nonland_terrain'
            assert candidate['probability'] == 0
    execute = AsyncMock(return_value={'status': 'accepted'})
    await run_scouting(state, directive=directive, execute=execute,
                       refresh=AsyncMock(return_value=state), **IDENTITY)
    execute.assert_awaited_once_with(
        'move_unit', {'unit_id': 'u0:131073', 'dest': '1,0', 'idempotency_key': ANY})


def test_fully_mountain_ring_holds_without_attempting_a_move():
    graph = plan_scouting(projected_state('PLAINS', True, safe_destination=False),
                          directive={}, **IDENTITY)
    decision = graph['decisions'][0]
    assert decision['selected']['action'] == 'fortify'
    assert all(row['excluded'] == 'unsupported_or_nonland_terrain'
               and row['probability'] == 0 for row in decision['candidates'])


def test_known_cold_mountain_biome_is_evidence_but_not_a_hill_or_map_boundary_proof():
    parsed = parse_visible_map([
        'VMAP|3', 'TURN|1', 'TILEROW|0|1|TERRAIN_TUNDRA_MOUNTAIN|false||',
        'TILEROW|0|-1|TERRAIN_PLAINS|true|-1|'])
    evidence = cold_biome_evidence(parsed['tiles'], '0,0')
    assert evidence['sectors']['north'] == {'known': 1, 'cold': 1, 'noncold': 0, 'unknown': 0}
    assert evidence['map_edge_hypothesis'] == 'possible_northern_periphery_unverified'
    assert 'No map bounds' in evidence['limits']
    assert parsed['tiles']['0,1']['native_terrain']['hills'] is False
