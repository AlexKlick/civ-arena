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


def growth_fixture(*, units=None, controls=None):
    from civ_arena.agents.growth_policy import GrowthControls, GrowthPolicy
    from test_growth_policy import option, unit
    state = snapshot()
    for tile in state['get_visible_map']['tiles'].values():
        tile['terrain'] = 'GRASSLAND'
    state['get_units'] = units if units is not None else [unit(kind='WARRIOR', power=20)]
    rows = [option('WARRIOR', power=20), district(), project()]
    growth = GrowthPolicy(0, controls=controls or GrowthControls(max_cities=1),
                          mission_execution=True)
    growth.begin_turn(state, turn=1, catalogs={'c0:1': rows})
    return growth, state, rows


def test_growth_uses_infrastructure_after_healthy_minimum_army():
    growth, state, rows = growth_fixture()
    out = choose(rows, state=state, growth=growth)
    assert out['item_id'] == 'DISTRICT_CAMPUS'
    assert out['growth']['military_capacity_slots'] == 1
    assert out['growth']['military_cap'] == 8
    assert not next(r for r in out['candidates'] if r['kind'] == 'unit')['eligible']


def test_confirmed_defense_gap_precedes_productive_preferences():
    from test_growth_policy import unit
    growth, state, rows = growth_fixture(units=[
        unit('barb', owner=63, coord='1,0', power=20, is_barbarian=True)])
    out = choose(rows, state=state, growth=growth, prefs=[project()['item_id']])
    assert out['item_id'] == 'WARRIOR'
    assert out['growth']['mode'] == 'defend'


def test_growth_cannot_relax_base_project_or_projected_target_guard():
    growth, state, _ = growth_fixture()
    assert choose([district(placements=['0,-1'])], state=state, growth=growth)['item_id'] is None
    assert choose([project('PROJECT_MANHATTAN_PROJECT')], state=state,
                  growth=growth)['item_id'] is None


@pytest.mark.parametrize('queued', ['DISTRICT_CAMPUS', 'PROJECT_ENHANCE_DISTRICT_CAMPUS'])
def test_observed_productive_kind_survives_active_catalog_suppression(queued):
    growth, state, rows = growth_fixture()
    growth.complete_turn(1)
    state['get_cities'].append({'city_id': 'c0:2', 'owner_id': 0, 'coord': '3,0',
                                'production_queue': [queued]})
    rows = [rows[0], {'item_id': 'GRANARY', 'kind': 'building'}]
    growth.begin_turn(state, turn=2, catalogs={'c0:1': rows})
    out = choose(rows, state=state, growth=growth)
    assert out['growth']['military_capacity_slots'] == 1
    assert out['growth']['unclassified_capacity_reservations'] == []


def test_unobserved_queue_kind_still_reserves_unknown_capacity():
    growth, state, rows = growth_fixture()
    state['get_cities'].append({'city_id': 'c0:2', 'owner_id': 0, 'coord': '3,0',
                                'production_queue': ['PROJECT_UNOBSERVED_CUSTOM']})
    out = choose(rows, state=state, growth=growth)
    assert out['growth']['military_capacity_slots'] == 2
    assert out['growth']['unclassified_capacity_reservations'] == [
        {'city_id': 'c0:2', 'item_id': 'PROJECT_UNOBSERVED_CUSTOM'}]


def test_kind_collision_cannot_free_a_unit_capacity_slot():
    growth, state, _ = growth_fixture()
    with pytest.raises(ValueError, match='conflicting observed production kind'):
        growth.refresh(state, catalogs={'c0:1': [{'item_id': 'WARRIOR', 'kind': 'building'}]})


async def test_curated_economy_submits_district_then_project_without_model_discovery():
    from civ_arena.agents.llm.context_curator import ContextCurator
    from test_growth_integration import GrowthFacade, setup

    class ProductiveFacade(GrowthFacade):
        async def set_city_production(self, city_id, item_id, dest=None):
            self.calls.append(('set_city_production', city_id, item_id, dest))
            self.cities[0]['production_queue'] = [item_id]
            return {'status': 'accepted'}

    controller, runtime, model, _, records = setup()
    growth, state, rows = growth_fixture()
    controller._growth = growth
    facade = ProductiveFacade()
    facade.units, facade.cities = state['get_units'], state['get_cities']
    facade.tiles, facade.catalog = state['get_visible_map']['tiles'], rows
    runtime.begin_turn(1)
    directive = validate_directive({}, player_id=0, owned_unit_ids={'u0:1'})
    curator = ContextCurator(facade, 0, 8000)
    await curator.refresh()
    assert '"placements":["2,0","1,0","0,1"]' in curator.render()
    await controller._economy(runtime, curator, directive)
    assert facade.calls[-1] != 'end_turn'  # economy proof only
    assert ('set_city_production', 'c0:1', 'DISTRICT_CAMPUS', '0,1') in facade.calls
    assert not curator.dirty
    assert facade.cities[0]['production_queue'] == ['DISTRICT_CAMPUS']
    growth.complete_turn(1)
    facade.cities[0]['production_queue'] = []  # fixture-observed district completion
    facade.catalog = [rows[0], project()]
    growth.begin_turn(state, turn=2, catalogs={'c0:1': facade.catalog})
    runtime.begin_turn(2)
    await controller._economy(runtime, ContextCurator(facade, 0, 8000), directive)
    assert ('set_city_production', 'c0:1', project()['item_id'], None) in facade.calls
    assert model.posts_sent == 0
    assert len([r for r in records if r['audit'] == 'strategy_economy']) == 2


@pytest.mark.parametrize('status', ['accepted', 'rejected'])
def test_district_attempt_invalidates_map_economics_and_all_demanded_catalogs(status):
    from civ_arena.agents.llm.context_curator import ContextCurator
    curator = ContextCurator(None, 0, 8000)
    curator.dirty.clear()
    curator.production = {'c0:1': [district()], 'c0:2': [project()]}
    curator.action_result('set_city_production', {'city_id': 'c0:1',
                          'item_id': 'DISTRICT_CAMPUS', 'dest': '0,1'}, {'status': status})
    assert curator.dirty == {'get_visible_map', 'get_cities', 'get_overview'}
    assert curator.production_dirty == {'c0:1', 'c0:2'}
    assert curator.research_dirty


@pytest.mark.parametrize('row', [district(), project()])
async def test_productive_rejection_stops_without_second_submission_or_model_retry(row):
    from unittest.mock import AsyncMock

    from civ_arena.agents.llm.context_curator import ContextCurator
    from civ_arena.arena.referee import MatchAborted
    from test_growth_integration import GrowthFacade, setup

    controller, runtime, model, _, _ = setup()
    growth, state, _ = growth_fixture()
    controller._growth = growth
    facade = GrowthFacade()
    facade.units, facade.cities = state['get_units'], state['get_cities']
    facade.tiles, facade.catalog = state['get_visible_map']['tiles'], [row]
    facade.set_city_production = AsyncMock(return_value={'status': 'rejected'})
    runtime.begin_turn(1)
    directive = validate_directive({}, player_id=0, owned_unit_ids={'u0:1'})
    with pytest.raises(MatchAborted, match='productive production rejected'):
        await controller._economy(runtime, ContextCurator(facade, 0, 8000), directive)
    assert facade.set_city_production.await_count == 1 and model.posts_sent == 0
    assert 'end_turn' not in facade.calls
