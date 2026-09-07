"""Productive choices use only offered, projected targets and preserve army caps."""
import copy

import pytest

from civ_arena.agents.production_policy import choose_production
from civ_arena.agents.productive_policy import placement_choice, production_args
from civ_arena.agents.strategy_directive import validate_directive


def snapshot():
    return {'get_units': [], 'get_cities': [
        {'city_id': 'c0:1', 'owner_id': 0, 'coord': '0,0', 'production_queue': []}],
        'get_visible_map': {'tiles': {
            '0,1': {'owner_id': 0, 'city_id': ''},
            '1,0': {'owner_id': 0, 'city_id': ''},
            '2,0': {'owner_id': 0, 'city_id': ''},
            '0,0': {'owner_id': 0, 'city_id': 'c0:1'},
            '-1,0': {'owner_id': 1, 'city_id': ''},
            '0,-1': {'terrain': 'GRASSLAND'},
        }}}


def district(**extra):
    return {'item_id': 'DISTRICT_CAMPUS', 'kind': 'district', 'cost': 90, 'turns': 9,
            'placements': ['2,0', '1,0', '0,1'], **extra}


def project(item='PROJECT_ENHANCE_DISTRICT_CAMPUS'):
    return {'item_id': item, 'kind': 'project', 'cost': 40, 'turns': 4}


def choose(options, *, state=None, prefs=(), growth=None):
    state = state or snapshot()
    directive = validate_directive({'production_preferences': list(prefs)}, player_id=0,
                                   owned_unit_ids={u['unit_id'] for u in state['get_units']})
    original = copy.deepcopy((state, options, directive))
    out = choose_production(state, player_id=0, city_id='c0:1', options=options,
                            directive=directive, growth_policy=growth)
    assert (state, options, directive) == original
    return out


def test_district_closes_empty_supported_catalog_with_audited_target():
    state, row = snapshot(), district()
    out = choose([row], state=state)
    assert out['item_id'] == 'DISTRICT_CAMPUS'
    assert production_args(out, [row], state, player_id=0, city_id='c0:1') == {
        'city_id': 'c0:1', 'item_id': 'DISTRICT_CAMPUS', 'dest': '0,1'}
    assert out['candidates'][0]['placement']['limits'][0] == 'no_adjacency_or_yield_optimization'


@pytest.mark.parametrize('dest', ['-1,0', '0,-1', '99,99', '0,0'])
def test_foreign_remembered_missing_or_city_tiles_never_selected(dest):
    state, row = snapshot(), district(placements=[dest])
    out = choose([row], state=state)
    assert out['item_id'] is None
    with pytest.raises(ValueError, match='no current projected placement'):
        production_args({'item_id': row['item_id']}, [row], state,
                        player_id=0, city_id='c0:1')


@pytest.mark.parametrize('owner', [None, False, True, '0', -1])
def test_unavailable_or_noninteger_owner_does_not_authorize_placement(owner):
    state = snapshot()
    state['get_visible_map']['tiles']['0,1']['owner_id'] = owner
    assert choose([district(placements=['0,1'])], state=state)['item_id'] is None


@pytest.mark.parametrize('placements', [None, '0,1', ['0,1', '0,1'], ['00,1'],
                                        ['0,+1'], ['0,1'] * 257])
def test_malformed_or_unbounded_placement_catalog_fails(placements):
    with pytest.raises(ValueError):
        choose([district(placements=placements)])


def test_target_is_recomputed_after_ownership_change():
    state, row = snapshot(), district()
    out = choose([row], state=state)
    state['get_visible_map']['tiles']['0,1']['owner_id'] = 1
    assert production_args(out, [row], state, player_id=0, city_id='c0:1')['dest'] == '1,0'


def test_target_choice_is_independent_of_native_enumeration_order():
    a = placement_choice(district(), snapshot(), player_id=0, city_id='c0:1')
    b = placement_choice(district(placements=['0,1', '2,0', '1,0']), snapshot(),
                         player_id=0, city_id='c0:1')
    assert a == b


def test_project_fallback_requires_observed_available_identity():
    out = choose([project()])
    assert out['item_id'] == 'PROJECT_ENHANCE_DISTRICT_CAMPUS'
    assert production_args(out, [project()], snapshot(), player_id=0, city_id='c0:1') == {
        'city_id': 'c0:1', 'item_id': 'PROJECT_ENHANCE_DISTRICT_CAMPUS'}
    with pytest.raises(ValueError, match='one exact offered item'):
        production_args(out, [], snapshot(), player_id=0, city_id='c0:1')


def test_nonroutine_project_requires_explicit_model_preference():
    row = project('PROJECT_MANHATTAN_PROJECT')
    assert choose([row])['item_id'] is None
    assert choose([row], prefs=[row['item_id']])['item_id'] == row['item_id']


def test_new_infrastructure_precedes_unrequested_repeat_project():
    assert choose([project(), district()])['item_id'] == 'DISTRICT_CAMPUS'
    assert choose([project(), district()], prefs=[project()['item_id']])['item_id'] == \
        project()['item_id']


@pytest.mark.parametrize('row', [district(item_id='CAMPUS'), project('CAMPUS')])
def test_unprefixed_productive_identity_fails(row):
    with pytest.raises(ValueError, match='exact native prefixed'):
        choose([row])


def test_legacy_production_args_do_not_gain_dest_or_null_keys():
    row = {'item_id': 'MONUMENT', 'kind': 'building'}
    out = choose([row])
    assert production_args(out, [row], snapshot(), player_id=0, city_id='c0:1') == {
        'city_id': 'c0:1', 'item_id': 'MONUMENT'}


def test_active_queue_cannot_be_replaced_by_a_project_or_district():
    state = snapshot()
    state['get_cities'][0]['production_queue'] = ['DISTRICT_CAMPUS']
    with pytest.raises(ValueError, match='never replaces an active queue'):
        choose([project(), district()], state=state)
