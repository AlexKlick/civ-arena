"""Controller-side demand, freshness and bounded deltas; no provider or tuner I/O."""
import copy
import json
from collections import Counter
from dataclasses import replace

import pytest

from civ_arena.agents.llm.context_curator import CONTEXT_MARKER, ContextCurator
from civ_arena.agents.llm.decision_packet import decision_packet, decision_snapshot
from civ_arena.arena.referee import MatchAborted
from fakes import text, use
from test_context_curator import World
from test_strategic_controller import advance
from test_strategic_controller import setup as setup  # noqa: F401


class ChoicesWorld(World):
    def __init__(self):
        super().__init__()
        self.cities = [{'city_id': 'c0:65536', 'owner': 0, 'coord': '2,20',
                        'production_queue': []}]
        self.items = [{'item_id': 'SCOUT'}]
        self.techs = [{'tech_id': 'POTTERY'}]

    async def get_available_research(self):
        self.calls.append(('get_available_research', {}))
        return copy.deepcopy(self.techs)

    async def get_available_production(self, city_id):
        assert city_id == 'c0:65536'
        self.calls.append(('get_available_production', {'city_id': city_id}))
        return copy.deepcopy(self.items)


def counts(world):
    return Counter(name for name, _ in world.calls)


@pytest.mark.parametrize('rejected', [False, True])
async def test_deferred_catalogs_refresh_once_after_actions_with_current_choices(rejected):
    world = ChoicesWorld()
    curator = ContextCurator(world, 0, 8000)
    await curator.refresh()
    assert counts(world)['get_available_production'] == 1
    assert counts(world)['get_available_research'] == 1

    async def move(unit_id, dest):
        world.units[0]['movement'] = 0
        world.items = [{'item_id': 'MONUMENT'}]
        world.techs = [{'tech_id': 'MINING'}]
        world.gold = 140
        return {'status': 'rejected' if rejected else 'accepted'}
    world.move_unit = move
    await curator.execute('move_unit', {'unit_id': 'u0:131073', 'dest': '3,22'})
    with pytest.raises(MatchAborted, match='post-action refresh'):
        curator.render()
    await curator.refresh(include_options=False)
    await curator.refresh(include_options=False)
    assert counts(world)['get_available_production'] == 1
    assert counts(world)['get_available_research'] == 1
    doc = json.loads(curator.render()[len(CONTEXT_MARKER):])
    assert doc['you']['gold'] == 140 and doc['own_units'][0]['movement'] == 0
    assert doc['production_options'] == {} and doc['research_options'] == []
    assert doc['option_sources'] == {'research': 'deferred',
                                     'production': {'c0:65536': 'deferred'}}
    await curator.refresh()
    await curator.refresh()
    assert counts(world)['get_available_production'] == 2
    assert counts(world)['get_available_research'] == 2
    assert curator.production['c0:65536'] == [{'item_id': 'MONUMENT'}]
    assert curator.state['get_available_research'] == [{'tech_id': 'MINING'}]


async def test_quiet_scouting_preloads_state_but_no_catalog_until_economy_needs_it():
    world = ChoicesWorld()
    curator = ContextCurator(world, 0, 8000)
    await curator.refresh(include_options=False)
    for dest in ('3,22', '4,22'):
        await curator.execute('move_unit', {'unit_id': 'u0:131073', 'dest': dest})
        await curator.refresh(include_options=False, include_overview=False)
    assert counts(world)['get_overview'] == 1
    with pytest.raises(MatchAborted, match='post-action refresh'):
        curator.render()
    assert counts(world)['get_available_production'] == 0
    assert counts(world)['get_available_research'] == 0
    await curator.refresh()
    assert counts(world)['get_overview'] == 2
    assert counts(world)['get_available_production'] == 1
    assert counts(world)['get_available_research'] == 1


async def test_successful_research_selection_does_not_repeat_unneeded_catalog():
    world = ChoicesWorld()
    curator = ContextCurator(world, 0, 8000)
    await curator.refresh()
    await curator.execute('set_research', {'tech_id': 'POTTERY'})
    await curator.refresh()
    assert counts(world)['get_available_research'] == 1  # baseline queried twice
    assert curator.state['get_overview']['you']['researching'] == 'POTTERY'
    assert curator.choice_status()['research'] == 'not_requested_active_choice'
    assert curator.state['get_available_research'] == []


async def test_rejected_research_keeps_observed_empty_distinct_from_unqueried():
    world = ChoicesWorld()
    world.researching = 'POTTERY'
    curator = ContextCurator(world, 0, 8000)
    await curator.refresh()
    assert curator.choice_status()['research'] == 'not_requested_active_choice'
    async def reject(tech_id):
        world.techs = []
        return {'status': 'rejected'}
    world.set_research = reject
    await curator.execute('set_research', {'tech_id': 'MINING'})
    await curator.refresh()
    assert counts(world)['get_available_research'] == 1
    assert curator.choice_status()['research'] == 'observed'
    assert curator.state['get_available_research'] == []


async def test_active_city_options_are_unqueried_until_owned_focus_and_revalidated():
    world = ChoicesWorld()
    world.cities[0]['production_queue'] = ['SCOUT']
    curator = ContextCurator(world, 0, 8000)
    await curator.refresh()
    assert curator.choice_status()['production']['c0:65536'] == 'not_requested_active_choice'
    assert counts(world)['get_available_production'] == 0
    cached = curator.cached_read('get_available_production', {'city_id': 'c0:65536'})
    assert cached['option_source'] == 'not_requested_active_choice' and 'value' not in cached
    foreign = curator.cached_read('get_available_production', {'city_id': 'c1:65536'})
    assert foreign['option_source'] == 'not_an_observed_owned_city' and 'value' not in foreign
    assert (await curator.inspect({'city_id': 'c1:65536'}))['status'] == 'rejected'
    await curator.inspect({'city_id': 'c0:65536'})
    await curator.refresh()
    assert counts(world)['get_available_production'] == 1
    world.items = []
    await curator.execute('move_unit', {'unit_id': 'u0:131073', 'dest': '3,22'})
    await curator.refresh()
    assert counts(world)['get_available_production'] == 2
    assert curator.production['c0:65536'] == []
    assert curator.choice_status()['production']['c0:65536'] == 'observed'


async def test_decision_deltas_are_projected_bounded_and_do_not_infer_contact_death():
    world = ChoicesWorld()
    curator = ContextCurator(world, 0, 8000)
    await curator.refresh()
    old = decision_snapshot(curator, 1)
    old['contacts'] = ['u1:20']
    current = copy.deepcopy(old)
    current['turn'] = 6
    current['contacts'] = ['u1:21']
    current['units']['u0:131073']['coord'] = '3,22'
    current['units'].update({f'u0:{i}': {'coord': '2,20'} for i in range(30)})
    current['known_tiles'].append('7,23')
    packet = decision_packet(current, old)
    changes = packet['changes_since_strategy_request']
    assert changes['from_turn'] == 1 and changes['to_turn'] == 6
    assert changes['contacts']['no_longer_observed']['items'] == ['u1:20']
    assert changes['contacts']['newly_observed']['items'] == ['u1:21']
    assert changes['units']['changed']['items'] == [{'id': 'u0:131073', 'fields': ['coord']}]
    assert len(changes['units']['newly_owned']['items']) == 16
    assert changes['units']['newly_owned']['omitted'] == 14
    assert changes['new_known_tile_count'] == 1
    assert old['units']['u0:131073']['coord'] == '3,21'
    assert packet['outstanding'] == {'research_choice': True, 'idle_city_ids': ['c0:65536']}
    with pytest.raises(ValueError, match='different player'):
        decision_packet(current, {**old, 'player_id': 1})


async def test_next_strategy_packet_compares_last_request_including_quiet_turns(setup):
    controller, runtime, model, facade, records, scout = setup
    for turn in range(1, 7):
        facade.units[0]['coord'] = f'{turn - 1},0'
        facade.you['gold'] = turn * 10
        await advance(controller, runtime, facade, turn)
    assert model.posts_sent == 2
    initial = json.loads(model.requests[0]['messages'][0]['content'].split('\n', 1)[0])
    final = json.loads(model.requests[1]['messages'][0]['content'].split('\n', 1)[0])
    assert initial['decision_packet']['changes_since_strategy_request'] is None
    delta = final['decision_packet']['changes_since_strategy_request']
    assert delta['from_turn'] == 1 and delta['to_turn'] == 6
    assert delta['units']['changed']['items'] == [{'id': 'u0:1', 'fields': ['coord']}]
    assert delta['you_changed_fields'] == ['gold']
    assert all([tool['name'] for tool in req['tools']] == ['submit_directive']
               for req in model.requests)


async def test_packet_and_repair_metadata_remain_in_total_budget(setup):
    controller, runtime, model, facade, records, scout = setup
    runtime.llm = replace(runtime.llm, max_result_chars=2500)
    model.script = [[text('no tool')], [use('submit_directive', {'version': 1})]]
    await advance(controller, runtime, facade, 1)
    assert model.posts_sent == 2
    contexts = [request['messages'][0]['content'] for request in model.requests]
    assert all(len(context) <= 2500 for context in contexts)
    packets = [json.loads(context.split('\n', 1)[0])['decision_packet'] for context in contexts]
    assert packets[0] == packets[1]
    assert packets[0]['changes_since_strategy_request'] is None


async def test_missing_options_fail_before_model_request(setup):
    controller, runtime, model, facade, records, scout = setup
    facade.you['researching'] = None
    async def missing():
        return {'error': 'research_accessor_unavailable'}
    facade.get_available_research = missing
    with pytest.raises(MatchAborted, match='research_accessor_unavailable'):
        await advance(controller, runtime, facade, 1)
    assert model.posts_sent == 0 and scout.await_count == 0
