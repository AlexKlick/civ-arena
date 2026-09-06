"""Growth foundations use projected fixtures and local Lua only; no live execution."""
import copy
import shutil
import subprocess

import pytest

from civ_arena.agents.growth_policy import GrowthControls, GrowthPolicy
from civ_arena.agents.production_policy import choose_production
from civ_arena.agents.strategy_directive import validate_directive
from civ_arena.game.civ6 import lua_translator as lt
from civ_arena.game.civ6.response_parser import parse_available_production


def option(item='MUSKETMAN', *, power=55, role='military', domain='DOMAIN_LAND'):
    return {'item_id': item, 'kind': 'unit', 'cost': 100, 'turns': 4,
            'unit_capabilities': {'combat': power if role == 'military' else 0, 'ranged': 0,
                                  'domain': domain, 'found_city': role == 'settler',
                                  'build_charges': 3 if role == 'builder' else 0}}


def unit(uid='u0:1', *, owner=0, kind='MUSKETMAN', coord='0,0', power=55, **extra):
    return {'unit_id': uid, 'owner_id': owner, 'type': kind, 'coord': coord,
            'strength': power, 'ranged_strength': 0, 'hp': 100, 'max_hp': 100,
            'health_valid': True, 'movement': 2, **extra}


def city(cid='c0:1', *, coord='0,0', queue=None):
    return {'city_id': cid, 'owner_id': 0, 'coord': coord,
            'production_queue': queue or []}


def state(units=(), cities=None):
    return {'get_units': list(units), 'get_cities': cities or [city()],
            'get_visible_map': {'tiles': {f'{q},{r}': {'terrain': 'GRASSLAND',
                                                     'owner_id': -1, 'city_id': ''}
                                       for q in range(8) for r in range(-2, 3)}}}


BUILDING = {'item_id': 'GRANARY', 'kind': 'building', 'cost': 50, 'turns': 2}
CATALOG = [option(), option('SETTLER', role='settler'), option('BUILDER', role='builder'),
           BUILDING]


def policy(snapshot, *, controls=None, catalogs=None):
    growth = GrowthPolicy(0, controls=controls)
    growth.begin_turn(snapshot, turn=1, catalogs=catalogs or {'c0:1': CATALOG})
    return growth


def choose(growth, snapshot, *, options=None, targets=None, reservations=None):
    directive = validate_directive({'production_preferences': ['MUSKETMAN', 'SETTLER', 'GRANARY'],
                                    'unit_targets': targets or {}}, player_id=0,
                                   owned_unit_ids={u['unit_id'] for u in snapshot['get_units']
                                                   if u['owner_id'] == 0})
    before = copy.deepcopy((snapshot, options, directive, reservations))
    result = choose_production(snapshot, player_id=0, city_id='c0:1', options=options or CATALOG,
                               directive=directive, reservations=reservations,
                               growth_policy=growth)
    assert (snapshot, options, directive, reservations) == before
    return result


def barb(**extra):
    return unit('barb', owner=63, coord='1,0', power=20, is_barbarian=True, **extra)


@pytest.mark.parametrize('flag', [None, False, 1, 'true'])
def test_nonbarbarian_or_unknown_contacts_never_imply_war(flag):
    s = state([unit(), unit('contact', owner=1, coord='1,0', is_barbarian=flag)])
    g = policy(s)
    out = choose(g, s)
    assert out['growth']['mode'] == 'grow'
    assert out['growth']['confirmed_local_barbarian_ids'] == []
    assert out['growth']['unclassified_or_nonbarbarian_contact_ids'] == ['contact']
    assert out['item_id'] == 'GRANARY'


def test_confirmed_local_risk_compares_observed_healthy_strength_and_modern_candidate():
    s = state([barb()])
    g = policy(s)
    out = choose(g, s)
    assert out['item_id'] == 'MUSKETMAN'
    assert out['growth']['mode'] == 'defend'
    assert out['growth']['known_threat_strength_sum'] == 20
    assert out['growth']['healthy_local_defense_strength_sum'] == 0
    assert out['growth']['production_defense_needed']


def test_three_consecutive_completed_clear_turns_required_for_grow():
    g = policy(state([barb()]))
    g.complete_turn(1)
    for turn, mode in [(2, 'defend'), (3, 'defend'), (4, 'grow')]:
        assert g.begin_turn(state([unit()]), turn=turn)['mode'] == mode
        g.complete_turn(turn)
    g.begin_turn(state([barb()]), turn=5)
    assert g.assessment['mode'] == 'defend' and g.assessment['quiet_observed_turns'] == 0


def test_quiet_count_does_not_advance_on_extra_economy_reads_or_rejected_turn():
    s = state([barb()])
    g = policy(s)
    for _ in range(3):
        choose(g, state([unit()]))
    assert g.assessment['quiet_observed_turns'] == 0
    with pytest.raises(ValueError, match='consecutive'):
        g.begin_turn(state(), turn=2)


def test_new_threat_during_economy_persists_defense_hysteresis():
    g = policy(state([unit()]))
    assert choose(g, state([barb()]))['growth']['mode'] == 'defend'
    assert g.assessment['mode'] == 'defend'
    g.complete_turn(1)
    assert g.begin_turn(state([unit()]), turn=2)['mode'] == 'defend'


@pytest.mark.parametrize('field,value', [('hp', 35), ('health_valid', False), ('max_hp', None)])
def test_injured_or_unverified_units_are_inventory_but_not_healthy_defense(field, value):
    defender = unit(**{field: value})
    s = state([defender, barb()])
    out = choose(policy(s), s)
    assert out['growth']['military_owned_queued'] == 1
    assert out['growth']['healthy_local_defender_ids'] == []
    assert out['growth']['health_unverified_or_recovering_ids'] == ['u0:1']


def test_aggregate_military_cap_prevents_one_of_each_and_respects_zero():
    s = state([unit(f'u0:{i}', power=10) for i in range(8)] + [barb()])
    out = choose(policy(s), s, options=[option('TANK', power=80), BUILDING])
    assert out['item_id'] == 'GRANARY'
    assert out['growth']['military_owned_queued'] == 8
    s = state([barb()])
    assert choose(policy(s), s, targets={'MUSKETMAN': 0})['item_id'] == 'GRANARY'


def test_remote_defenders_do_not_count_as_current_local_strength():
    s = state([unit(coord='30,0'), barb()])
    assert choose(policy(s), s)['growth']['healthy_local_defense_strength_sum'] == 0


@pytest.mark.parametrize('reservation', [False, True])
def test_other_city_nonoffered_modern_queue_or_reservation_reduces_production_gap(reservation):
    s = state([unit(), barb()],
              cities=[city(), city('c0:2', queue=[] if reservation else ['TANK'])])
    g = policy(s, catalogs={'c0:1': CATALOG, 'c0:2': [option('TANK', power=80)]})
    out = choose(g, s, reservations={'c0:2': 'TANK'} if reservation else None)
    assert out['growth']['military_owned_queued'] == 2
    assert out['growth']['queued_reserved_strength'] == 80
    assert out['growth']['healthy_local_defense_strength_sum'] == 55
    assert not out['growth']['production_defense_needed']
    assert out['item_id'] == 'GRANARY'


def test_observed_queue_and_reservation_count_once():
    s = state([unit(), barb()], cities=[city(), city('c0:2', queue=['MUSKETMAN'])])
    g = policy(s)
    out = choose(g, s, reservations={'c0:2': 'MUSKETMAN'})
    assert out['growth']['military_owned_queued'] == 2


@pytest.mark.parametrize('catalog', [
    [{'item_id': 'TANK', 'kind': 'unit'}, BUILDING],
    [option('SUBMARINE', domain='DOMAIN_SEA'), BUILDING],
    [option('FIGHTER', domain='DOMAIN_AIR'), BUILDING],
])
def test_unknown_or_unsupported_capabilities_never_guessed_by_id(catalog):
    s = state([barb()])
    assert choose(policy(s), s, options=catalog)['item_id'] == 'GRANARY'


def test_settlement_requires_route_spare_healthy_escort_and_city_guard():
    s = state([unit()])
    g = policy(s)
    assert g.mission['status'] == 'awaiting_healthy_spare_escort'
    assert choose(g, s)['item_id'] == 'GRANARY'
    s = state([unit(), unit('u0:2', power=35)])
    g = policy(s)
    assert g.mission['status'] == 'awaiting_settler'
    assert g.mission['escort_id'] == 'u0:2'
    assert choose(g, s)['item_id'] == 'SETTLER'


def test_city_guard_cannot_be_remote_or_recovering():
    s = state([unit(coord='20,0'), unit('u0:2')])
    assert policy(s).mission['status'] == 'awaiting_healthy_spare_escort'


def test_mission_identity_site_persists_and_proposal_is_only_one_observed_step():
    s = state([unit(), unit('u0:2', power=35)])
    g = policy(s)
    prior = g.mission
    g.complete_turn(1)
    s['get_units'].append(unit('u0:3', kind='SETTLER', power=0))
    g.begin_turn(s, turn=2)
    assert g.mission['mission_id'] == prior['mission_id']
    assert g.mission['site'] == prior['site']
    assert g.mission['proposal']['action'] == 'move_unit'
    assert g.mission['proposal']['args']['dest'] in s['get_visible_map']['tiles']
    assert len(g.mission['route']) <= 12
    assert s['get_units'][-1]['coord'] == '0,0'  # no execution occurs
    snapshot = g.mission
    snapshot['site'] = '999,999'
    assert g.mission['site'] == prior['site']


def test_new_threat_suspends_mission_without_replacing_task_destination():
    s = state([unit(), unit('u0:2')])
    g = policy(s)
    prior = g.mission
    g.complete_turn(1)
    g.begin_turn(state([unit(), unit('u0:2'), barb()]), turn=2)
    assert g.mission['status'] == 'suspended_local_threat'
    assert g.mission['site'] == prior['site'] and g.mission['proposal'] is None


def test_fog_owner_unknown_and_mountains_do_not_supply_automatic_settlement_route():
    s = state([unit(), unit('u0:2')])
    for tile in s['get_visible_map']['tiles'].values():
        tile.pop('owner_id')
    assert policy(s).mission is None
    for tile in s['get_visible_map']['tiles'].values():
        tile.update(owner_id=-1, terrain='MOUNTAIN')
    assert policy(s).mission is None


def test_mission_expires_and_does_not_silently_restart():
    s = state([unit(), unit('u0:2')])
    g = policy(s, controls=GrowthControls(mission_ttl=1))
    mid = g.mission['mission_id']
    g.complete_turn(1)
    g.begin_turn(s, turn=2)
    assert g.mission['status'] == 'expired' and g.mission['mission_id'] == mid


@pytest.mark.parametrize('control,value', [('quiet_turns', 0), ('military_cap', True),
                                         ('max_cities', 33), ('max_route', 25)])
def test_controls_are_bounded(control, value):
    with pytest.raises(ValueError):
        GrowthControls(**{control: value})


def test_production_requires_explicit_active_player_policy():
    s = state()
    g = GrowthPolicy(0)
    with pytest.raises(ValueError, match='active matching'):
        choose(g, s)


@pytest.mark.parametrize('suffix,expected', [
    ('', None), ('|55|0|DOMAIN_LAND|false|0', {'combat': 55, 'ranged': 0,
          'domain': 'DOMAIN_LAND', 'found_city': False, 'build_charges': 0}),
    ('|?|?|?|?|?', dict.fromkeys(['combat', 'ranged', 'domain', 'found_city', 'build_charges']))])
def test_native_capabilities_are_optional_closed_and_unknown_preserving(suffix, expected):
    row = parse_available_production(['ITEMROW|unit|MUSKETMAN|100|4' + suffix])[0]
    assert row.get('unit_capabilities') == expected


@pytest.mark.parametrize('suffix', ['|55|0|DOMAIN_LAND|yes|0', '|55|0|SEA|false|0',
                                    '|-1|0|DOMAIN_LAND|false|0', '|55|0|DOMAIN_LAND|false',
                                    '|55.5|0|DOMAIN_LAND|false|0'])
def test_malformed_native_capability_fields_fail(suffix):
    with pytest.raises(ValueError):
        parse_available_production(['ITEMROW|unit|MUSKETMAN|100|4' + suffix])


def test_building_cannot_smuggle_unit_capabilities():
    with pytest.raises(ValueError):
        parse_available_production(['ITEMROW|building|GRANARY|50|2|55|0|DOMAIN_LAND|false|0'])


def test_translated_native_catalog_fields_with_local_lua(tmp_path):
    exe = shutil.which('texlua')
    if exe is None:
        pytest.skip('texlua unavailable')
    source = '''
Game={GetLocalPlayer=function() return 0 end}
CityOperationTypes={PARAM_UNIT_TYPE=1,BUILD=2}
local queue={CanProduce=function() return true end,GetTurnsLeft=function() return 4 end}
local city={GetID=function() return 1 end,GetBuildQueue=function() return queue end}
CityManager={GetCity=function() return city end,CanStartOperation=function() return true end}
GameInfo={Units=function()
 local done=false
 return function() if not done then done=true; return {UnitType='UNIT_MUSKETMAN',
 Hash=1,Cost=100,Combat=55,RangedCombat=0,Domain='DOMAIN_LAND',FoundCity=false,BuildCharges=0}
 end end end,Buildings=function() return function() end end}
'''
    path = tmp_path / 'growth-catalog.lua'
    path.write_text(source + lt.available_production_read('c0:1'))
    result = subprocess.run([exe, str(path)], capture_output=True, text=True, timeout=5, check=True)
    rows = parse_available_production(result.stdout.splitlines())
    assert rows[0]['unit_capabilities'] == option()['unit_capabilities']
    assert rows[0]['item_id'] == 'MUSKETMAN'


@pytest.mark.parametrize('role,item', [('settler', 'COLONIST'), ('builder', 'MOD_BUILDER')])
def test_civilian_targets_count_other_type_and_city_queue(role, item):
    s = state([unit(), unit('u0:2')], cities=[city(), city('c0:2', queue=[item, item])])
    g = policy(s, catalogs={'c0:1': CATALOG, 'c0:2': [option(item, role=role)]})
    out = choose(g, s)
    assert out['growth']['civilian_inventory'][role] == 2
    current = 'SETTLER' if role == 'settler' else 'BUILDER'
    assert not next(row for row in out['candidates'] if row['item_id'] == current)['eligible']


def test_economy_refresh_rechecks_lost_escort_before_settler_production():
    s = state([unit(), unit('u0:2')])
    g = policy(s)
    assert g.mission['status'] == 'awaiting_settler'
    s['get_units'].pop()
    assert choose(g, s)['item_id'] == 'GRANARY'
    assert g.mission['status'] == 'awaiting_healthy_spare_escort'


def test_unknown_queued_capacity_never_counts_as_healthy_defense_or_allows_overbuild():
    s = state([barb()], cities=[city(), city('c0:2', queue=['UNCLASSIFIED'] * 8)])
    out = choose(policy(s), s)
    assert out['growth']['military_owned_queued'] == 0
    assert out['growth']['military_capacity_slots'] == 8
    assert out['growth']['healthy_local_defense_strength_sum'] == 0
    assert out['item_id'] == 'GRANARY'


def test_settler_movement_zero_and_recovery_suspend_proposal():
    s = state([unit(), unit('u0:2'), unit('settler', kind='SETTLER', power=0, movement=0)])
    g = policy(s)
    assert g.mission['proposal'] is None
    assert g.mission['status'] == 'awaiting_observed_movement'
    g.complete_turn(1)
    s['get_units'][-1].update(movement=2, hp=35)
    g.begin_turn(s, turn=2)
    assert g.mission['proposal'] is None
    assert g.mission['status'] == 'awaiting_verified_settler_health'


def test_failed_or_found_mission_is_not_misreported_as_causal_success():
    s = state([unit(), unit('u0:2')])
    g = policy(s)
    site = g.mission['site']
    g.complete_turn(1)
    s['get_cities'].append(city('c0:2', coord=site))
    g.begin_turn(s, turn=2)
    assert g.mission['status'] == 'city_observed_at_site'
    assert g.mission['completion_basis'] == 'observed_owned_city_not_causal_receipt'


def test_configuration_and_observation_copies_do_not_imply_foreign_catalog_authority():
    with pytest.raises(ValueError, match='observed owned city'):
        policy(state(), catalogs={'c1:1': CATALOG})
    g = policy(state())
    record = g.assessment
    record['mode'] = 'tampered'
    assert g.assessment['mode'] == 'grow'


def test_modern_capability_efficiency_can_outrank_old_opening_preference():
    s = state([barb()])
    weak = option('WARRIOR', power=20)
    weak['turns'] = 10
    modern = option('MUSKETMAN', power=55)
    modern['turns'] = 2
    out = choose(policy(s), s, options=[weak, modern, BUILDING])
    assert out['item_id'] == 'MUSKETMAN'
    row = next(row for row in out['candidates'] if row['item_id'] == 'MUSKETMAN')
    assert row['bounded_strength_per_reported_turn'] == 12.5


def test_route_probe_bound_does_not_spend_candidates_on_known_invalid_spacing():
    s = state([unit(), unit('u0:2')])
    for q in range(8, 13):
        for r in range(-2, 3):
            s['get_visible_map']['tiles'][f'{q},{r}'] = {
                'terrain': 'GRASSLAND', 'owner_id': -1, 'city_id': ''}
    g = policy(s, controls=GrowthControls(min_city_spacing=8))
    assert g.mission is not None


@pytest.mark.parametrize('field,value', [('combat', True), ('ranged', -1),
                                        ('domain', 'SPACE'), ('found_city', 1),
                                        ('build_charges', 10001)])
def test_capability_schema_refuses_fabricated_fields(field, value):
    row = option()
    row['unit_capabilities'][field] = value
    with pytest.raises(ValueError):
        policy(state(), catalogs={'c0:1': [row]})


def test_overmaximum_health_never_counts_as_verified_healthy():
    s = state([unit(hp=101), barb()])
    out = choose(policy(s), s)
    assert out['growth']['healthy_local_defender_ids'] == []
