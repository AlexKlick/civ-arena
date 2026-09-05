"""Curated controller state replaces model discovery calls; no network or engine I/O."""
import copy
import json

import pytest

from civ_arena.agents.llm.context_curator import BASIC_READS, CONTEXT_MARKER, ContextCurator
from civ_arena.arena.referee import MatchAborted
from fakes import FakeModel, use
from test_llm_turn_pacing import SPEC, Facade, runtime


class World(Facade):
    def __init__(self):
        super().__init__()
        self.units = [{'unit_id': 'u0:131073', 'owner_id': 0, 'type': 'WARRIOR',
                       'coord': '3,21', 'movement': 2, 'max_movement': 2},
                      {'unit_id': 'u0:65536', 'owner_id': 0, 'type': 'SETTLER',
                       'coord': '2,20', 'movement': 2, 'max_movement': 2}]
        self.cities = []
        self.gold = 100
        self.researching = ''
        self.sight_fresh = False

    async def get_visible_map(self):
        self.calls.append(('get_visible_map', {}))
        self.sight_fresh = True
        return {'tiles': {f'{q},{r}': {'terrain': 'PLAINS'}
                          for q in range(1, 7) for r in range(19, 24)}}

    async def get_units(self):
        assert self.sight_fresh
        self.calls.append(('get_units', {}))
        return copy.deepcopy(self.units)

    async def get_cities(self):
        assert self.sight_fresh
        self.calls.append(('get_cities', {}))
        return copy.deepcopy(self.cities)

    async def get_overview(self):
        self.calls.append(('get_overview', {}))
        return {'you': {'gold': self.gold, 'researching': self.researching}}

    async def get_available_production(self, city_id):
        assert city_id in {city['city_id'] for city in self.cities}
        self.calls.append(('get_available_production', {'city_id': city_id}))
        return [{'item_id': 'SCOUT', 'cost': 30}]

    async def found_city(self, unit_id):
        self.calls.append(('found_city', {'unit_id': unit_id}))
        self.sight_fresh = False
        self.units = [u for u in self.units if u['unit_id'] != unit_id]
        self.cities.append({'city_id': 'c0:65536', 'owner': 0, 'coord': '2,20',
                            'production_queue': []})
        return {'status': 'accepted', 'result': {'detail': '12,20'}}

    async def move_unit(self, unit_id, dest):
        self.calls.append(('move_unit', {'unit_id': unit_id, 'dest': dest}))
        self.sight_fresh = False
        for unit in self.units:
            if unit['unit_id'] == unit_id:
                unit.update(coord='3,22', movement=0)
        return {'status': 'accepted', 'result': {'detail': '13,21'},
                'mutations': [{'attr': 'pos', 'after': '14,22'}]}

    async def set_city_production(self, city_id, item_id):
        self.calls.append(('set_city_production', {'city_id': city_id, 'item_id': item_id}))
        self.cities[0]['production_queue'] = [item_id]
        return {'status': 'accepted'}

    async def purchase(self, city_id, item_id):
        self.calls.append(('purchase', {'city_id': city_id, 'item_id': item_id}))
        self.gold = 40
        self.sight_fresh = False
        self.units.append({'unit_id': 'u0:200000', 'owner_id': 0, 'type': 'SCOUT',
                           'coord': '2,20', 'movement': 2})
        return {'status': 'accepted'}

    async def set_research(self, tech_id):
        self.calls.append(('set_research', {'tech_id': tech_id}))
        self.researching = tech_id
        return {'status': 'accepted'}


def snapshot(request):
    texts = [block['text'] for message in request['messages']
             if message['role'] == 'user' and isinstance(message['content'], list)
             for block in message['content'] if block.get('type') == 'text'
             and block['text'].startswith(CONTEXT_MARKER)]
    assert len(texts) == 1, 'Only latest controller state belongs in each request'
    assert len(texts[0]) <= SPEC.max_result_chars
    return json.loads(texts[0][len(CONTEXT_MARKER):])


async def test_found_city_and_move_get_post_action_state_without_discovery_request():
    world = World()
    model = FakeModel([
        [use('found_city', {'unit_id': 'u0:65536'}),
         use('move_unit', {'unit_id': 'u0:131073', 'dest': '4,22'})],
        [use('set_city_production', {'city_id': 'c0:65536', 'item_id': 'SCOUT'})],
        [use('end_turn')]])
    await runtime(model).take_turn(world)
    assert model.posts_sent == 3  # every request makes an actual decision, never discovery
    post = snapshot(model.requests[1])
    assert post['own_units'] == [{'unit_id': 'u0:131073', 'type': 'WARRIOR',
                                 'coord': '3,22', 'movement': 0, 'max_movement': 2}]
    assert post['own_cities'][0]['city_id'] == 'c0:65536'
    assert post['production_options']['c0:65536'][0]['item_id'] == 'SCOUT'
    assert snapshot(model.requests[2])['own_cities'][0]['production_queue'] == ['SCOUT']
    # One map/entity refresh for the two independent actions, not one per action.
    assert sum(name == 'get_visible_map' for name, _ in world.calls) == 2
    assert sum(name == 'get_units' for name, _ in world.calls) == 2
    assert '13,21' not in json.dumps(model.requests[1])
    assert '14,22' not in json.dumps(model.requests[1])
    for request in model.requests:
        assert not BASIC_READS & {tool['name'] for tool in request['tools']}


async def test_purchase_refreshes_gold_new_units_and_research_selection():
    world = World()
    world.cities = [{'city_id': 'c0:65536', 'owner': 0, 'coord': '2,20',
                     'production_queue': ['SCOUT']}]
    model = FakeModel([[use('purchase', {'city_id': 'c0:65536', 'item_id': 'SCOUT'}),
                        use('set_research', {'tech_id': 'POTTERY'})], [use('end_turn')]])
    await runtime(model).take_turn(world)
    post = snapshot(model.requests[1])
    assert post['you'] == {'gold': 40, 'researching': 'POTTERY'}
    assert any(u['unit_id'] == 'u0:200000' for u in post['own_units'])
    assert sum(name == 'get_overview' for name, _ in world.calls) == 2


async def test_rejected_attempt_invalidates_and_legacy_reads_make_no_rpc():
    world = World()
    async def rejected_move(unit_id, dest):
        world.units[0]['movement'] = 0  # real rejected live commands can restore/refreeze
        return {'status': 'rejected', 'rejection': 'illegal_move'}
    world.move_unit = rejected_move
    model = FakeModel([[use('move_unit', {'unit_id': 'u0:131073', 'dest': '4,22'}),
                        use('get_units'), use('get_visible_map')],
                       [use('get_units'), use('get_cities')], [use('end_turn')]])
    await runtime(model).take_turn(world)
    assert snapshot(model.requests[1])['own_units'][0]['movement'] == 0
    assert sum(name == 'get_units' for name, _ in world.calls) == 2
    assert sum(name == 'get_visible_map' for name, _ in world.calls) == 2


async def test_error_shaped_facade_read_never_looks_empty():
    world = World()
    async def expired():
        return {'error': 'lease_expired'}
    world.get_visible_map = expired
    model = FakeModel([[use('end_turn')]])
    with pytest.raises(MatchAborted, match='lease_expired'):
        await runtime(model).take_turn(world)
    assert model.posts_sent == 0


async def test_all_owned_entities_kept_or_budget_fails_honestly():
    world = World()
    world.units *= 100
    curator = ContextCurator(world, 0, 1000)
    await curator.refresh()
    with pytest.raises(MatchAborted, match='critical owned state'):
        curator.render()


async def test_focused_inspection_cannot_query_foreign_city_or_unknown_tile():
    world = World()
    curator = ContextCurator(world, 0, 8000)
    await curator.refresh()
    before = len(world.calls)
    for args in ({'city_id': 'c1:65536'}, {'coord': '99,99'}):
        assert (await curator.inspect(args))['status'] == 'rejected'
    assert len(world.calls) == before


async def test_history_has_one_snapshot_and_valid_paired_results_at_round_cap():
    world = World()
    model = FakeModel([[use('get_units', id='read')]])
    await runtime(model).take_turn(world)
    assert model.posts_sent == SPEC.max_tool_rounds
    assert sum(name == 'get_units' for name, _ in world.calls) == 1
    sizes = []
    for request in model.requests:
        snapshot(request)
        sizes.append(len(json.dumps(request['messages'])))
        for msg in request['messages']:
            if msg['role'] == 'user' and isinstance(msg['content'], list):
                for block in msg['content']:
                    if block['type'] == 'tool_result':
                        json.loads(block['content'])
    assert max(sizes) < 17000  # fixture measurement, not an arbitrary model-output bound


async def test_real_facade_post_found_city_reads_are_audited_and_replayable(tmp_path):
    from civ_arena.replay import replay_run
    from test_llm_runtime import make_arena

    model = FakeModel([[use('found_city', {'unit_id': 'u1'})],
                       [use('set_city_production', {'city_id': 'c1', 'item_id': 'SCOUT'})],
                       [use('end_turn')]])
    arena, _ = make_arena(tmp_path, model, max_turns=1)
    arena.runtimes[0].configure_turn_pacing(recall_available=False)
    summary = await arena.run()
    assert summary['aborted'] is None
    view = snapshot(model.requests[1])
    assert view['own_cities'][0]['city_id'] == 'c1'
    assert 'SCOUT' in {x['item_id'] for x in view['production_options']['c1']}
    records = arena.log.records()
    found_seq = next(r['seq'] for r in records if r.get('tool') == 'found_city'
                     and r['kind'] == 'TOOL_RESULT' and r['status'] == 'accepted')
    choice_seq = next(r['seq'] for r in records if r.get('tool') == 'set_city_production'
                      and r['kind'] == 'TOOL_CALL')
    assert any(found_seq < r['seq'] < choice_seq and r.get('tool') == 'get_available_production'
               and r['kind'] == 'TOOL_CALL' for r in records)
    replay = await replay_run(arena.run_dir, arena.spec, tmp_path / 'curated-replay')
    assert replay['identical'], replay
