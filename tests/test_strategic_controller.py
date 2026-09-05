"""Controller request cadence, fresh-only custody, and honest closure; no network."""
import copy
from unittest.mock import AsyncMock

import pytest

from civ_arena.agents.llm.client import ModelReply, ModelUnavailable
from civ_arena.agents.llm.runtime import LLMAgentRuntime
from civ_arena.agents.llm.strategic_controller import StrategicController
from civ_arena.agents.runtime import AgentProfile
from civ_arena.arena.referee import MatchAborted
from civ_arena.config import LLMSpec
from fakes import FakeModel, text, use


class Facade:
    def __init__(self):
        self.units = [{'unit_id': 'u0:1', 'owner_id': 0, 'type': 'SCOUT',
                       'coord': '0,0', 'movement': 2, 'hp': 100}]
        self.cities = []
        self.you = {'researching': 'MINING', 'gold': 0}
        self.calls = []
        self.closures = [{'status': 'accepted'}]

    async def get_visible_map(self):
        self.calls.append('map')
        return {'tiles': {'0,0': {'terrain': 'PLAINS'}, '1,0': {'terrain': 'PLAINS'}}}

    async def get_units(self):
        self.calls.append('units')
        return copy.deepcopy(self.units)

    async def get_cities(self):
        return copy.deepcopy(self.cities)

    async def get_overview(self):
        return {'you': copy.deepcopy(self.you), 'public': {}}

    async def get_available_research(self):
        return [{'tech_id': 'MINING'}, {'tech_id': 'POTTERY'}]

    async def get_available_production(self, city_id):
        return [{'item_id': 'SCOUT'}, {'item_id': 'WARRIOR'}]

    async def set_research(self, tech_id):
        self.calls.append(('set_research', tech_id))
        self.you['researching'] = tech_id
        return {'status': 'accepted'}

    async def set_city_production(self, city_id, item_id):
        self.calls.append(('set_city_production', city_id, item_id))
        next(c for c in self.cities if c['city_id'] == city_id)['production_queue'] = item_id
        return {'status': 'accepted'}

    async def fortify(self, unit_id, **kwargs):
        self.calls.append(('fortify', unit_id))
        return {'status': 'accepted'}

    async def move_unit(self, unit_id, dest, **kwargs):
        self.calls.append(('move_unit', unit_id, dest))
        next(u for u in self.units if u['unit_id'] == unit_id)['coord'] = dest
        return {'status': 'accepted'}

    async def end_turn(self):
        self.calls.append('end_turn')
        return self.closures.pop(0) if len(self.closures) > 1 else self.closures[0]


@pytest.fixture
def setup(monkeypatch):
    scout = AsyncMock(return_value={'nodes': [], 'seed': 'fixture'})
    monkeypatch.setattr('civ_arena.agents.llm.strategic_controller.run_scouting', scout)
    model = FakeModel([[use('submit_directive', {'version': 1})]])
    llm = LLMSpec('http://unused', 'UNUSED', 'fake')
    runtime = LLMAgentRuntime.build(AgentProfile('a', 0, 'llm', 4, llm=llm), client=model)
    records = []
    controller = StrategicController('fresh-match', cadence=5, audit=records.append)
    return controller, runtime, model, Facade(), records, scout


async def advance(controller, runtime, facade, turn, **kwargs):
    runtime.begin_turn(turn)
    await controller.take_turn(runtime, facade, **kwargs)


async def test_quiet_turns_no_provider_request_and_cadence_one_json(setup):
    controller, runtime, model, facade, records, scout = setup
    for turn in range(1, 7):
        before = model.posts_sent
        await advance(controller, runtime, facade, turn)
        assert model.posts_sent - before == int(turn in (1, 6))
    assert scout.await_count == 6
    assert facade.calls.count('end_turn') == 6
    assert all([tool['name'] for tool in req['tools']] == ['submit_directive']
               for req in model.requests)
    assert all(len(req['messages']) == 1 for req in model.requests)
    assert all(len(req['messages'][0]['content']) <= runtime.llm.max_result_chars
               for req in model.requests)
    assert records[-1]['audit'] == 'strategy_turn_closed'
    assert any(r.get('source') == 'autopilot' and r['audit'] == 'strategy_execution'
               for r in records)


@pytest.mark.parametrize('trigger',
                         ['city', 'contact', 'research', 'research_auto',
                          'damage', 'tactical', 'settler', 'lost_unit'])
async def test_milestone_exactly_one_new_json_request(setup, trigger):
    controller, runtime, model, facade, records, scout = setup
    await advance(controller, runtime, facade, 1)
    if trigger == 'city':
        facade.cities.append({'city_id': 'c0:1', 'owner': 0, 'coord': '0,0',
                              'production_queue': 'SCOUT'})
    elif trigger == 'contact':
        facade.units.append({'unit_id': 'u1:1', 'owner_id': 1, 'type': 'SCOUT', 'coord': '1,0'})
    elif trigger == 'research':
        facade.you['researching'] = None
    elif trigger == 'research_auto':
        facade.you['researching'] = 'POTTERY'
    elif trigger == 'lost_unit':
        facade.units.clear()
    elif trigger == 'damage':
        facade.units[0]['hp'] = 80
    elif trigger == 'settler':
        facade.units.append({'unit_id': 'u0:2', 'owner_id': 0, 'type': 'SETTLER', 'coord': '1,0'})
    await advance(controller, runtime, facade, 2, tactical_requested=trigger == 'tactical')
    assert model.posts_sent == 2
    assert len([r for r in records if r['audit'] == 'strategy_decision']) == 2


async def test_owned_override_one_turn_only_foreign_override_before_action(setup):
    controller, runtime, model, facade, records, scout = setup
    model.script = [[use('submit_directive', {'tactical_overrides': [
        {'unit_id': 'u0:1', 'action': 'hold'}]})]]
    await advance(controller, runtime, facade, 1)
    assert scout.await_args.kwargs['directive']['tactical_overrides']
    await advance(controller, runtime, facade, 2)
    assert not scout.await_args.kwargs['directive']['tactical_overrides']
    model.script = [[use('submit_directive', {'tactical_overrides': [
        {'unit_id': 'u1:1', 'action': 'hold'}]})]]
    with pytest.raises(MatchAborted, match='currently owned'):
        await advance(controller, runtime, facade, 3, tactical_requested=True)
    assert scout.await_count == 2
    assert facade.calls.count('end_turn') == 2


@pytest.mark.parametrize('blocks', [[text('done')], [use('get_units')],
                                  [use('submit_directive'), use('submit_directive')],
                                  [use('submit_directive', {'unknown': 1})],
                                  [text('narration'), use('submit_directive')]])
async def test_invalid_model_response_bounded_no_actions_or_closure(setup, blocks):
    controller, runtime, model, facade, records, scout = setup
    model.script = [blocks]
    with pytest.raises(MatchAborted):
        await advance(controller, runtime, facade, 1)
    assert model.posts_sent == 1
    assert scout.await_count == 0
    assert 'end_turn' not in facade.calls
    assert records[-1]['audit'] == 'strategy_failed'
    with pytest.raises(MatchAborted, match='previously failed'):
        await advance(controller, runtime, facade, 1)
    assert model.posts_sent == 1


async def test_request_budget_no_post(setup):
    controller, runtime, model, facade, records, scout = setup
    model.posts_sent = runtime.llm.max_requests_per_match
    with pytest.raises(MatchAborted, match='budget exhausted'):
        await advance(controller, runtime, facade, 1)
    assert not model.requests
    assert not scout.await_count


async def test_provider_failure_converted_to_match_abort(setup):
    controller, runtime, model, facade, records, scout = setup
    model.create = AsyncMock(side_effect=ModelUnavailable('offline'))
    with pytest.raises(MatchAborted, match='unavailable'):
        await advance(controller, runtime, facade, 1)
    assert model.create.await_count == 1
    assert not scout.await_count


async def test_truncated_tool_response_rejected(setup):
    controller, runtime, model, facade, records, scout = setup
    model.create = AsyncMock(return_value=ModelReply([use('submit_directive')],
                                                    'max_tokens', 'fake', 10, 10))
    with pytest.raises(MatchAborted, match='complete strategic directive'):
        await advance(controller, runtime, facade, 1)
    assert not scout.await_count


async def test_closure_repair_once_and_repeated_failure_honest(setup):
    controller, runtime, model, facade, records, scout = setup
    facade.closures = [{'status': 'rejected', 'rejection': 'unmoved_units',
                        'unmoved_units': ['u0:1']}, {'status': 'rejected'}]
    with pytest.raises(MatchAborted, match='did not release'):
        await advance(controller, runtime, facade, 1)
    assert facade.calls.count('end_turn') == 2
    assert facade.calls.count(('fortify', 'u0:1')) == 1
    assert not any(r['audit'] == 'strategy_turn_closed' for r in records)
    assert controller._last_turn == 0


async def test_successful_completeness_repair_preserved(setup):
    controller, runtime, model, facade, records, scout = setup
    facade.closures = [{'status': 'rejected', 'rejection': 'unmoved_units',
                        'unmoved_units': ['u0:1']}, {'status': 'accepted'}]
    await advance(controller, runtime, facade, 1)
    assert facade.calls.count('end_turn') == 2
    assert records[-1]['audit'] == 'strategy_turn_closed'


@pytest.mark.parametrize('turn', [0, 2, 10])
async def test_fresh_only_refuses_resume_without_posts_or_facade_reads(setup, turn):
    controller, runtime, model, facade, records, scout = setup
    with pytest.raises(MatchAborted, match='fresh-only'):
        await advance(controller, runtime, facade, turn)
    assert not model.posts_sent
    assert not facade.calls


async def test_preferences_only_fill_idle_economy(setup):
    controller, runtime, model, facade, records, scout = setup
    facade.you['researching'] = None
    facade.cities = [{'city_id': 'c0:1', 'owner': 0, 'coord': '0,0', 'production_queue': []},
                     {'city_id': 'c0:2', 'owner': 0, 'coord': '1,0', 'production_queue': 'SCOUT'}]
    model.script = [[use('submit_directive', {'research_preferences': ['POTTERY'],
                                            'production_preferences': ['WARRIOR']})]]
    await advance(controller, runtime, facade, 1)
    assert ('set_research', 'POTTERY') in facade.calls
    assert ('set_city_production', 'c0:1', 'WARRIOR') in facade.calls
    assert not any(isinstance(c, tuple) and 'c0:2' in c for c in facade.calls)
    await advance(controller, runtime, facade, 2)
    assert model.posts_sent == 1


async def test_quiet_turn_works_even_when_provider_budget_spent(setup):
    controller, runtime, model, facade, records, scout = setup
    await advance(controller, runtime, facade, 1)
    model.posts_sent = runtime.llm.max_requests_per_match
    await advance(controller, runtime, facade, 2)
    assert len(model.requests) == 1
    assert facade.calls.count('end_turn') == 2


async def test_oversized_critical_context_refuses_before_provider(setup):
    controller, runtime, model, facade, records, scout = setup
    facade.units = [dict(facade.units[0], unit_id=f'u0:{i}') for i in range(200)]
    with pytest.raises(MatchAborted, match='context budget'):
        await advance(controller, runtime, facade, 1)
    assert not model.posts_sent
    assert not scout.await_count


@pytest.mark.parametrize('frozen,movement,expected_moves', [(False, 2, 1), (False, 0, 0),
                                                           (True, 0, 1)])
async def test_real_seeded_executor_facade_and_frozen_opening(setup, monkeypatch,
                                                            frozen, movement, expected_moves):
    from civ_arena.agents.scouting import run_scouting
    monkeypatch.setattr('civ_arena.agents.llm.strategic_controller.run_scouting', run_scouting)
    controller, runtime, model, facade, records, scout = setup
    controller.opening_units_frozen = frozen
    facade.units[0]['movement'] = movement
    await advance(controller, runtime, facade, 1)
    moves = [call for call in facade.calls if isinstance(call, tuple) and call[0] == 'move_unit']
    assert len(moves) == expected_moves
    assert model.posts_sent == 1
    assert facade.calls[-1] == 'end_turn'
    graph = next(row['graph'] for row in records if row['audit'] == 'strategy_graph')
    assert graph['identity']['configured_seed'] == 4
    assert len(graph['execution']) == expected_moves
