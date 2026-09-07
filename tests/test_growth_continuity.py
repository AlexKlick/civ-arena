"""Bounded preparation and unsent replanning; no native/provider execution."""
import copy
import json
from pathlib import Path

import pytest

from civ_arena.agents.growth_policy import GrowthControls, GrowthPolicy
from civ_arena.agents.llm.context_curator import ContextCurator
from civ_arena.agents.production_policy import choose_production
from test_growth_integration import GrowthFacade, setup
from test_growth_policy import CATALOG, city, state, unit
from test_growth_policy import choose as choose_simple
from test_strategic_controller import advance


def retained():
    return json.loads((Path(__file__).parent / 'fixtures/growth_t39_projection.json').read_text())


def t39():
    fixture = retained()
    g = GrowthPolicy(0, mission_execution=True)
    g._completed = 38
    g._mission = copy.deepcopy(fixture['mission'])
    g.begin_turn(fixture['state'], turn=39, catalogs={'c0:65536': fixture['options']})
    return g, fixture


def choose(g, f, reservations=None):
    return choose_production(f['state'], player_id=0, city_id='c0:65536',
                             options=f['options'], directive=f['directive'],
                             reservations=reservations, growth_policy=g)


def test_exact_retained_t39_prepares_one_founder_without_claiming_safe_site():
    g, f = t39()
    result = choose(g, f)
    assert len(f['state']['get_visible_map']['tiles']) == 107
    assert 'owner_id' not in f['state']['get_visible_map']['tiles']['5,32']
    assert g.mission['replan_outcome'] == 'no_current_feasible_alternative'
    assert result['item_id'] == 'SETTLER'
    assert result['growth']['preparatory_founder_reserve']['selected']
    assert result['growth']['military_capacity_slots'] == 4
    assert not [r for r in result['candidates'] if r['eligible'] and r['item_id'] != 'SETTLER']
    assert g.mission['proposal'] is None  # production does not authorize unknown route/founding
    scope = result['growth']['production_option_scope']
    assert scope['native_availability_of_unrepresented_choices'] == 'unobserved'


async def test_retained_t39_economy_queues_preparation_without_new_model_request():
    g, fixture = t39()
    ctl, rt, model, _, records = setup()
    rt.begin_turn(39)
    ctl._growth = g
    f = GrowthFacade()
    f.units = fixture['state']['get_units']
    f.cities = fixture['state']['get_cities']
    f.tiles = fixture['state']['get_visible_map']['tiles']
    f.you = fixture['state']['get_overview']['you']
    f.catalog = fixture['options']
    curator = ContextCurator(f, 0, 8000)
    await ctl._economy(rt, curator, fixture['directive'])
    assert f.cities[0]['production_queue'] == 'SETTLER'
    assert model.posts_sent == 0
    assert g.mission is None  # infeasible unsent intent retired for surveying
    preparation = g.summary()['preparatory_training']
    assert preparation['status'] == 'awaiting_site_survey'
    assert preparation['retired_unsent_intent']['site'] == '5,32'
    audit = next(r for r in records if r['audit'] == 'strategy_growth_preparation')
    assert audit['preparation']['retired_unsent_intent']['site'] == '5,32'
    assert audit['mission'] is None
    roles = g.reserved_roles(curator.state)
    assert roles[preparation['escort_id']] == 'escort'
    assert 'u0:262146' not in roles  # old uncommitted scout escort can survey


@pytest.mark.parametrize('inventory', ['owned', 'queued', 'reserved', 'accepted_unobserved'])
def test_one_founder_limit_counts_every_preparation_inventory_source(inventory):
    g, f = t39()
    reservation = None
    if inventory == 'owned':
        f['state']['get_units'].append(unit('founder', kind='SETTLER', power=0))
    elif inventory in {'queued', 'reserved'}:
        f['state']['get_cities'].append(city('c0:2', coord='8,29',
                                           queue=['SETTLER'] if inventory == 'queued' else []))
        reservation = {'c0:2': 'SETTLER'} if inventory == 'reserved' else None
    else:
        g.reserve_founder_production(f['state'], 'c0:65536', 'SETTLER', preparatory=True)
    result = choose(g, f, reservation)
    assert not next(r for r in result['candidates'] if r['item_id'] == 'SETTLER')['eligible']


@pytest.mark.parametrize('condition', ['zero_target', 'unhealthy', 'remote_spares', 'barbarian',
                                      'city_cap', 'attempted', 'expired', 'training_committed'])
def test_preparation_never_bypasses_explicit_caps_health_threat_or_committed_plan(condition):
    g, f = t39()
    if condition == 'zero_target':
        f['directive']['unit_targets']['SETTLER'] = 0
    elif condition == 'unhealthy':
        for u in f['state']['get_units']:
            if u['owner_id'] == 0:
                u['hp'] = 35
    elif condition == 'remote_spares':
        for u in f['state']['get_units']:
            if u['owner_id'] == 0 and u['unit_id'] != 'u0:131073':
                u['coord'] = '30,30'
    elif condition == 'barbarian':
        f['state']['get_units'].append(unit('barb', owner=63, coord='8,29', is_barbarian=True))
    elif condition == 'city_cap':
        g.controls = GrowthControls(max_cities=1)
    elif condition == 'attempted':
        g.record_mission_attempt(g.mission['mission_id'], 39)
    elif condition == 'expired':
        g._mission['status'] = 'expired'
    else:
        g._mission['training_accepted_turn'] = 38
    result = choose(g, f)
    assert not next(r for r in result['candidates'] if r['item_id'] == 'SETTLER')['eligible']
    assert g.mission['site'] == '5,32'


def replannable(limit=3):
    s = state([unit(), unit('escort', coord='1,0')])
    g = GrowthPolicy(0, mission_execution=True, controls=GrowthControls(unstarted_replans=limit))
    g.begin_turn(s, turn=1, catalogs={'c0:1': CATALOG})
    return g, s


def test_unstarted_infeasible_site_retargets_with_bounded_history_and_current_route():
    g, s = replannable(limit=2)
    first = g.mission['site']
    for turn in (1, 2):
        old = g.mission['site']
        s['get_visible_map']['tiles'][old].pop('owner_id')
        g.refresh(s)
        assert g.mission['site'] != old
        assert g.mission['replans'][-1]['from_site'] == old
        assert all(s['get_visible_map']['tiles'][step]['owner_id'] in (-1, 0)
                   for step in g.mission['route'])
        g.complete_turn(turn)
        g.begin_turn(s, turn=turn + 1)
    current = g.mission['site']
    s['get_visible_map']['tiles'][current].pop('owner_id')
    for _ in range(5):
        g.refresh(s)
    assert g.mission['site'] == current
    assert len(g.mission['replans']) == 2
    assert g.mission['replans'][0]['from_site'] == first
    assert g.mission['replan_outcome'] == 'unstarted_replan_limit_reached'


@pytest.mark.parametrize('commitment', ['owned', 'queued', 'reserved', 'sent', 'travel'])
def test_committed_or_sent_mission_never_automatically_retargets(commitment):
    g, s = replannable()
    old = g.mission['site']
    if commitment == 'owned':
        s['get_units'].append(unit('settler', kind='SETTLER', power=0))
    elif commitment == 'queued':
        s['get_cities'][0]['production_queue'] = ['SETTLER']
    elif commitment == 'reserved':
        g.reserve_founder_production(s, 'c0:1', 'SETTLER')
    elif commitment == 'sent':
        g.record_mission_attempt(g.mission['mission_id'], 1)
    else:
        g._mission['travel_started_turn'] = 1
    s['get_visible_map']['tiles'][old].pop('owner_id')
    g.refresh(s)
    assert g.mission['site'] == old and not g.mission.get('replans')


def test_uncommitted_escort_free_to_survey_then_reserved_after_training():
    g, s = replannable()
    escort = g.mission['escort_id']
    assert escort not in g.reserved_roles(s)
    g.reserve_founder_production(s, 'c0:1', 'SETTLER')
    assert g.reserved_roles(s)[escort] == 'escort'


def test_no_feasible_productive_choice_is_explicit_supported_subset_not_native_absence():
    g, f = t39()
    f['directive']['unit_targets']['SETTLER'] = 0
    result = choose(g, f)
    assert result['item_id'] is None
    scope = result['growth']['production_option_scope']
    assert scope['unrepresented_native_choices'] == ['district_placement', 'city_projects']
    assert scope['no_eligible_choice_requires']


async def test_completed_preparation_does_not_commit_the_next_expansion_intent():
    ctl, rt, _, f, _ = setup()
    f.units = [u for u in f.units if u['unit_id'] != 'settler']
    f.tiles = {f'{q},0': {'terrain': 'GRASSLAND', 'city_id': '',
                         **({'owner_id': -1} if q < 4 else {})} for q in range(13)}
    await advance(ctl, rt, f, 1)
    assert ctl._growth.summary()['preparatory_training']['status'] == 'awaiting_site_survey'
    assert f.cities[0]['production_queue'] == 'SETTLER'
    for tile in f.tiles.values():
        tile['owner_id'] = -1
    f.units.append(unit('settler', kind='SETTLER', power=0))
    f.cities[0]['production_queue'] = []
    for turn in range(2, 11):
        await advance(ctl, rt, f, turn)
        if len(f.cities) == 2:
            break
    assert len(f.cities) == 2 and ctl._growth.mission is None
    assert ctl._growth.summary()['preparatory_training']['status'] == 'completed'
    assert ctl._growth.summary()['founder_training']['status'] == 'completed'
    assert not ctl._growth._training_pending()
    await advance(ctl, rt, f, turn + 1)
    assert ctl._growth.mission is not None
    assert 'training_accepted_turn' not in ctl._growth.mission
    assert ctl._growth._unstarted(ctl._growth.mission)


@pytest.mark.parametrize('preparatory', [True, False])
@pytest.mark.parametrize('missing_outcome', ['empty', 'other_queue', 'foreign_founder'])
def test_accepted_training_remains_latched_when_queue_disappears(preparatory, missing_outcome):
    if preparatory:
        g, f = t39()
        s, cid, turn = f['state'], 'c0:65536', 39
        def decide():
            return choose(g, f)
    else:
        g, s = replannable()
        cid, turn = 'c0:1', 1
        def decide():
            return choose_simple(g, s)
    producer = next(c for c in s['get_cities'] if c['city_id'] == cid)
    g.reserve_founder_production(s, cid, 'SETTLER', preparatory=preparatory)
    producer['production_queue'] = ['SETTLER']
    g.refresh(s)
    assert g.summary()['founder_training']['status'] == 'queued_observed'
    g.complete_turn(turn)
    producer['production_queue'] = []
    if missing_outcome == 'foreign_founder':
        s['get_units'].append(unit('foreign', owner=1, kind='SETTLER', coord='30,30', power=0))
    elif missing_outcome == 'other_queue':
        producer['production_queue'] = ['BUILDER']
    g.begin_turn(s, turn=turn + 1)
    receipt = g.summary()['founder_training']
    assert receipt['status'] == 'outcome_unavailable' and receipt['review_due']
    assert not receipt['observed_founder_present']
    # A different observed queue is never overwritten either; inspect after it closes.
    producer['production_queue'] = []
    for _ in range(3):
        g.refresh(s)
        result = decide()
        row = next(r for r in result['candidates'] if r['item_id'] == 'SETTLER')
        assert not row['eligible']
        assert row['reason'] == 'accepted_founder_training_unresolved'
    with pytest.raises(ValueError, match='remains unresolved'):
        g.reserve_founder_production(s, cid, 'SETTLER', preparatory=preparatory)
    assert g.summary()['founder_training']['accepted_turn'] == turn


def test_preparatory_receipt_reconciles_founder_observation_but_never_retrains_after_loss():
    g, f = t39()
    s = f['state']
    g.reserve_founder_production(s, 'c0:65536', 'SETTLER', preparatory=True)
    g.complete_turn(39)
    g.begin_turn(s, turn=40)
    assert g.summary()['founder_training']['review_due']
    s['get_units'].append(unit('new_founder', kind='SETTLER', coord='8,28', power=0))
    g.refresh(s)
    receipt = g.summary()['founder_training']
    assert receipt['status'] == 'founder_observed'
    assert receipt['observed_founder_id'] == 'new_founder' and not receipt['review_due']
    g.complete_turn(40)
    s['get_units'] = [u for u in s['get_units'] if u['unit_id'] != 'new_founder']
    g.begin_turn(s, turn=41)
    assert g.summary()['founder_training']['review_due']
    assert not next(r for r in choose(g, f)['candidates'] if r['item_id'] == 'SETTLER')['eligible']


def test_training_receipt_preserves_original_same_turn_reservation_and_missing_city():
    g, f = t39()
    s = f['state']
    g.reserve_founder_production(s, 'c0:65536', 'SETTLER', preparatory=True)
    first = g.summary()['founder_training']
    g.reserve_founder_production(s, 'c0:65536', 'SETTLER')
    assert g.summary()['founder_training'] == first
    g.complete_turn(39)
    s['get_cities'] = [city('other', coord='8,28')]
    g.begin_turn(s, turn=40)
    receipt = g.summary()['founder_training']
    assert receipt['review_due'] and not receipt['observed_producer_owned']
    with pytest.raises(ValueError, match='remains unresolved'):
        g.reserve_founder_production(s, 'other', 'SETTLER', preparatory=True)



def test_ordinary_training_without_site_binds_next_observed_feasible_mission():
    g, f = t39()
    s = f['state']
    g._mission = None
    g.reserve_founder_production(s, 'c0:65536', 'SETTLER')
    g.complete_turn(39)
    g.begin_turn(s, turn=40)
    assert g.mission is None and g.summary()['founder_training']['review_due']
    assert not next(r for r in choose(g, f)['candidates'] if r['item_id'] == 'SETTLER')['eligible']
    for tile in s['get_visible_map']['tiles'].values():
        tile.setdefault('owner_id', -1)  # New current observations, not a runtime inference.
    g.refresh(s)
    assert g.mission['training_accepted_turn'] == 39
    assert g.summary()['founder_training']['mission_id'] == g.mission['mission_id']
    assert not g._unstarted(g.mission)



async def test_missing_preparatory_outcome_requests_one_review_without_repeat_training():
    ctl, rt, model, f, records = setup()
    f.units = [u for u in f.units if u['unit_id'] != 'settler']
    f.tiles = {f'{q},0': {'terrain': 'GRASSLAND', 'city_id': '',
                         **({'owner_id': -1} if q < 4 else {})} for q in range(13)}
    await advance(ctl, rt, f, 1)
    assert f.cities[0]['production_queue'] == 'SETTLER'
    f.cities[0]['production_queue'] = []
    for turn in range(2, 5):
        await advance(ctl, rt, f, turn)
    reviews = [r for r in records if r['audit'] == 'strategy_execution'
               and 'founder_training_outcome_unavailable' in r['reasons']]
    assert len(reviews) == 1 and reviews[0]['turn'] == 2
    assert model.posts_sent == 2  # Initial directive and one receipt review only.
    assert ctl._growth.mission is None
    assert ctl._growth.summary()['founder_training']['review_due']
    assert sum(call[:3] == ('set_city_production', 'c0:1', 'SETTLER')
               for call in f.calls if isinstance(call, tuple)) == 1
