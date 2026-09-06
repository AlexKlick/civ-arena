"""Controller request cadence, fresh-only custody, and honest closure; no network."""
import copy
import hashlib
import json
from dataclasses import replace
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
        return [{'item_id': 'SCOUT', 'kind': 'unit'}, {'item_id': 'WARRIOR', 'kind': 'unit'}]

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
    with pytest.raises(MatchAborted, match='invalid_args/schema_or_ownership'):
        await advance(controller, runtime, facade, 3, tactical_requested=True)
    assert scout.await_count == 2
    assert facade.calls.count('end_turn') == 2


@pytest.mark.parametrize('blocks', [[text('done')], [use('get_units')],
                                  [use('submit_directive'), use('submit_directive')],
                                  [use('submit_directive', {'unknown': 1})]])
async def test_invalid_model_response_bounded_no_actions_or_closure(setup, blocks):
    controller, runtime, model, facade, records, scout = setup
    model.script = [blocks]
    with pytest.raises(MatchAborted):
        await advance(controller, runtime, facade, 1)
    assert model.posts_sent == 2
    assert scout.await_count == 0
    assert 'end_turn' not in facade.calls
    assert records[-1]['audit'] == 'strategy_failed'
    with pytest.raises(MatchAborted, match='previously failed'):
        await advance(controller, runtime, facade, 1)
    assert model.posts_sent == 2


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
    with pytest.raises(MatchAborted, match='invalid_shape/truncated'):
        await advance(controller, runtime, facade, 1)
    assert not scout.await_count
    assert model.create.await_count == 2


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


@pytest.mark.parametrize('frozen', [False, True])
async def test_frozen_opening_authority_is_truthful_and_shares_whole_context_cap(setup, frozen):
    from civ_arena.agents.llm.context_curator import CONTEXT_MARKER
    controller, runtime, model, facade, records, scout = setup
    controller.opening_units_frozen = frozen
    facade.units[0]['movement'] = 0
    facade.units.append({'unit_id': 'u1:2', 'owner_id': 1, 'type': 'SCOUT',
                         'coord': '1,0', 'movement': 0})
    await advance(controller, runtime, facade, 1)
    request = model.requests[0]
    content = request['messages'][0]['content']
    metadata_text, context = content.split('\n', 1)
    metadata = json.loads(metadata_text)
    authority = metadata['movement_authority']
    assert authority['opening_units_frozen'] is frozen
    assert authority['untouched_owned_unit_ids'] == (['u0:1'] if frozen else [])
    assert authority['natural_allowance'] == ('unobserved' if frozen else 'use_projected_movement')
    observed = json.loads(context.removeprefix(CONTEXT_MARKER))
    assert observed['own_units'][0]['movement'] == 0
    assert len(content) <= runtime.llm.max_result_chars
    assert 'engine legality still applies' in request['system']
    assert 'ordinary zero remains observed spent movement' in request['system']


@pytest.mark.parametrize('auxiliary', [text('I will execute the strategy now.'),
    {'type': 'thinking', 'thinking': 'private reasoning'},
    {'type': 'redacted_thinking', 'data': 'private'}, {'type': 'text', 'text': None}])
async def test_valid_directive_discards_auxiliary_content_without_repair(setup, auxiliary):
    controller, runtime, model, facade, records, scout = setup
    model.script = [[auxiliary, use('submit_directive')]]
    await advance(controller, runtime, facade, 1)
    assert model.posts_sent == 1
    assert scout.await_count == 1
    assert facade.calls[-1] == 'end_turn'
    shape = next(r for r in records if r['audit'] == 'strategy_response_shape')
    assert shape['category'] == 'valid' and shape['attempt'] == 1


@pytest.mark.parametrize('bad,stop,category,reason', [
    ([text('I am done')], 'end_turn', 'invalid_shape', 'tool_count'),
    ([use('submit_directive'), use('submit_directive')], 'tool_use', 'invalid_shape', 'tool_count'),
    ([use('move_unit', {'unit_id': 'u0:1', 'dest': '1,0'})], 'tool_use',
     'invalid_shape', 'wrong_tool'),
    ([use('submit_directive')], 'max_tokens', 'invalid_shape', 'truncated'),
    ([use('submit_directive', {'wrong_key': 'private argument'})], 'tool_use',
     'invalid_args', 'schema_or_ownership'),
    ([use('submit_directive', {'tactical_overrides': [{'unit_id': 'u1:9', 'action': 'hold'}]})],
     'tool_use', 'invalid_args', 'schema_or_ownership'),
])
async def test_one_fresh_format_repair_before_any_game_action(setup, bad, stop, category, reason):
    controller, runtime, model, facade, records, scout = setup
    model.script = [bad, [use('submit_directive', {'version': 1})]]
    create = model.create
    async def checked_create(**kwargs):
        assert not scout.await_count
        assert 'end_turn' not in facade.calls
        assert not any(isinstance(c, tuple) for c in facade.calls)
        reply = await create(**kwargs)
        return replace(reply, stop_reason=stop) if model.posts_sent == 1 else reply
    model.create = checked_create
    await advance(controller, runtime, facade, 1)
    assert model.posts_sent == 2 and scout.await_count == 1
    assert all(req['tool_choice'] == {'type': 'tool', 'name': 'submit_directive'}
               for req in model.requests)
    assert all(len(req['messages']) == 1 and req['messages'][0]['role'] == 'user'
               for req in model.requests)
    shapes = [r for r in records if r['audit'] == 'strategy_response_shape']
    assert [(r['attempt'], r['category']) for r in shapes] == [(1, category), (2, 'valid')]
    assert shapes[0]['reason'] == reason and shapes[0]['repair_available']
    metadata = json.loads(model.requests[1]['messages'][0]['content'].split('\n', 1)[0])
    assert metadata['format_repair']['previous_category'] == category
    assert metadata['format_repair']['attempt'] == 2
    assert 'private argument' not in json.dumps(model.requests[1])
    decision = next(r for r in records if r['audit'] == 'strategy_decision')
    assert decision['format_attempts'] == decision['posts_attempted'] == 2
    assert decision['input_tokens'] == 20 and decision['output_tokens'] == 40


@pytest.mark.parametrize('cap', ['turn', 'posts'])
async def test_repair_respects_turn_and_global_post_limits(setup, cap):
    controller, runtime, model, facade, records, scout = setup
    model.script = [[text('no directive')]]
    runtime.llm = replace(runtime.llm, **({'max_tool_rounds': 1} if cap == 'turn'
                                        else {'max_requests_per_match': 1}))
    with pytest.raises(MatchAborted, match='no format repair budget remains'):
        await advance(controller, runtime, facade, 1)
    assert model.posts_sent == 1 and not scout.await_count
    shape = next(r for r in records if r['audit'] == 'strategy_response_shape')
    assert not shape['repair_available']


async def test_retry_posts_are_spent_before_repair_budget_check(setup):
    controller, runtime, model, facade, records, scout = setup
    runtime.llm = replace(runtime.llm, max_requests_per_match=3)
    async def client_retried(**kwargs):
        model.posts_sent += 3
        return ModelReply([text('no directive')], 'end_turn', 'fake', 10, 10)
    model.create = AsyncMock(side_effect=client_retried)
    with pytest.raises(MatchAborted, match='no format repair budget remains'):
        await advance(controller, runtime, facade, 1)
    assert model.create.await_count == 1 and model.posts_sent == 3
    shape = next(r for r in records if r['audit'] == 'strategy_response_shape')
    assert shape['posts_attempted'] == 3 and not scout.await_count


async def test_both_fresh_contexts_cap_includes_repair_metadata(setup):
    controller, runtime, model, facade, records, scout = setup
    model.script = [[text('x' * 12000)], [use('submit_directive')]]
    await advance(controller, runtime, facade, 1)
    assert model.posts_sent == 2
    for req in model.requests:
        content = req['messages'][0]['content']
        assert len(content) <= runtime.llm.max_result_chars
        metadata, state = content.split('\n', 1)
        assert json.loads(metadata)['turn'] == 1
        assert '"own_units"' in state
        assert 'x' * 100 not in content
    assert 'format_repair' not in model.requests[0]['messages'][0]['content']
    assert 'format_repair' in model.requests[1]['messages'][0]['content']


async def test_shape_audit_is_durable_canonical_and_never_copies_model_secrets(setup, tmp_path):
    from civ_arena.agents.runtime import strategy_audit_event
    from civ_arena.arena.events import EventLog
    from civ_arena.canonical import log_prefix_hash
    from civ_arena.game.civ6.live_driver import LiveDriver
    controller, runtime, model, facade, records, scout = setup
    secret = 'private-response-secret'
    model.script = [[text(secret), {'type': 'thinking', 'thinking': secret},
                     use(secret, {'password': secret})]]
    original = model.create
    async def secret_stop(**kwargs):
        return replace(await original(**kwargs), stop_reason=secret)
    model.create = secret_stop
    log = EventLog(tmp_path / 'events.jsonl')
    driver = object.__new__(LiveDriver)
    driver.spec = type('Spec', (), {'match_id': 'm'})()
    driver.game_instance_id = 'i'
    driver.log = log
    # Use the same durable canonical-safe wrapper as both coordinator lanes.
    from civ_arena.game.civ6.live_driver import _strategic_audit
    def audit(payload):
        records.append(payload)
        _strategic_audit(driver, payload)
    controller.audit = audit
    with pytest.raises(MatchAborted, match='invalid_shape/wrong_tool'):
        await advance(controller, runtime, facade, 1)
    log.close()
    rows = log.records()
    assert len(log_prefix_hash(rows)) == 64
    assert secret not in (tmp_path / 'events.jsonl').read_text()
    assert secret not in json.dumps(model.requests[1])
    shapes = [r for r in records if r['audit'] == 'strategy_response_shape']
    assert len(shapes) == 2
    assert shapes[0]['shape']['tool_names'] == ['other']
    assert shapes[0]['shape']['stop_reason'] == 'other'
    assert shapes[0]['shape']['text_chars'] == len(secret)
    assert shapes[0]['shape']['block_types'] == {'text': 1, 'thinking': 1, 'tool_use': 1}
    assert 'strategy_payload_json' in strategy_audit_event(shapes[0])
    assert not scout.await_count and 'end_turn' not in facade.calls


async def test_oversized_invalid_args_are_categorized_without_copying(setup):
    controller, runtime, model, facade, records, scout = setup
    model.script = [[use('submit_directive', {'unexpected': 'x' * 9000})]]
    with pytest.raises(MatchAborted, match='invalid_args/arguments_oversized'):
        await advance(controller, runtime, facade, 1)
    assert model.posts_sent == 2
    assert all(r['reason'] == 'arguments_oversized' for r in records
               if r['audit'] == 'strategy_response_shape')
    assert 'x' * 100 not in json.dumps(records)


@pytest.mark.parametrize('repair', [False, True])
async def test_exact_projected_request_is_audited_before_each_model_call(setup, repair):
    controller, runtime, model, facade, records, scout = setup
    model.script = ([[text('repair needed')]] if repair else []) + [[use('submit_directive')]]
    original = model.create
    async def checked_create(**kwargs):
        request = records[-1]
        assert request['audit'] == 'strategy_request'
        assert request['attempt'] == model.posts_sent + 1
        context = kwargs['messages'][0]['content']
        assert request['user_context'] == context
        assert request['context_chars'] == len(context) <= runtime.llm.max_result_chars
        assert request['context_sha256'] == hashlib.sha256(context.encode('utf-8')).hexdigest()
        assert request['named_tool'] == kwargs['tool_choice']['name'] == 'submit_directive'
        assert '"own_units"' in context and '"unit_id":"u0:1"' in context
        return await original(**kwargs)
    model.create = checked_create
    await advance(controller, runtime, facade, 1)
    assert len([r for r in records if r['audit'] == 'strategy_request']) == (2 if repair else 1)


@pytest.mark.parametrize("queue_observed", [False, True])
async def test_production_targets_cover_two_cities_even_before_queue_is_observed(setup,
                                                                               queue_observed):
    controller, runtime, model, facade, records, scout = setup
    facade.cities = [{"city_id": cid, "owner": 0, "coord": "0,0", "production_queue": []}
                     for cid in ("c0:2", "c0:1")]
    facade.get_available_production = AsyncMock(return_value=[
        {"item_id": "SCOUT", "kind": "unit"}, {"item_id": "MONUMENT", "kind": "building"}])
    original = facade.set_city_production

    async def produce(city_id, item_id):
        if queue_observed:
            return await original(city_id, item_id)
        facade.calls.append(('set_city_production', city_id, item_id))
        return {"status": "accepted"}

    facade.set_city_production = produce
    model.script = [[use('submit_directive', {"production_preferences": ["SCOUT"]})]]
    await advance(controller, runtime, facade, 1)
    actions = [call for call in facade.calls if isinstance(call, tuple)
               and call[0] == "set_city_production"]
    assert actions == [("set_city_production", "c0:1", "SCOUT"),
                       ("set_city_production", "c0:2", "MONUMENT")]
    policies = [r["production_policy"] for r in records if r.get("production_policy")]
    last_scout = next(row for row in policies[-1]["candidates"] if row["item_id"] == "SCOUT")
    assert last_scout["owned"] == 1
    assert last_scout["queued"] == int(queue_observed)
    assert last_scout["reserved"] == int(not queue_observed)
    assert last_scout["effective"] == 2
    assert model.posts_sent == 1


async def test_rejected_production_does_not_reserve_inventory_or_retry_same_city(setup):
    controller, runtime, model, facade, records, scout = setup
    facade.cities = [{"city_id": cid, "owner": 0, "coord": "0,0", "production_queue": []}
                     for cid in ("c0:1", "c0:2")]
    facade.set_city_production = AsyncMock(return_value={"status": "rejected"})
    model.script = [[use('submit_directive', {"production_preferences": ["SCOUT"]})]]
    await advance(controller, runtime, facade, 1)
    assert [call.kwargs for call in facade.set_city_production.await_args_list] == [
        {"city_id": "c0:1", "item_id": "SCOUT"}, {"city_id": "c0:2", "item_id": "SCOUT"}]
    policies = [r["production_policy"] for r in records if r.get("production_policy")]
    assert all(row["reserved"] == 0 for policy in policies for row in policy["candidates"])


async def test_exhausted_production_refresh_updates_targets_and_discards_late_tactics(setup):
    controller, runtime, model, facade, records, scout = setup
    facade.cities = [{"city_id": "c0:1", "owner": 0, "coord": "0,0", "production_queue": []}]
    facade.get_available_production = AsyncMock(return_value=[{"item_id": "SCOUT", "kind": "unit"}])
    model.script = [[use('submit_directive', {"unit_targets": {"SCOUT": 0}})],
                    [use('submit_directive', {"unit_targets": {"SCOUT": 3},
                                             "tactical_overrides": [{"unit_id": "u0:1",
                                                                      "action": "hold"}]})]]
    await advance(controller, runtime, facade, 1)
    assert model.posts_sent == 2
    assert controller.directive["unit_targets"] == {"SCOUT": 3}
    assert controller.directive["tactical_overrides"] == []
    assert controller._last_decision == 1
    assert scout.await_count == 1
    assert ("set_city_production", "c0:1", "SCOUT") in facade.calls
    late = next(row for row in records if row.get("phase") == "economy")
    assert late["reasons"] == ["production_targets_satisfied"]
    assert late["discarded_late_tactical_override_ids"] == ["u0:1"]
    await advance(controller, runtime, facade, 2)
    assert model.posts_sent == 2
    assert scout.await_args.kwargs["directive"]["tactical_overrides"] == []


async def test_repeated_target_exhaustion_stops_after_one_refresh_without_closure(setup):
    controller, runtime, model, facade, records, scout = setup
    facade.cities = [{"city_id": "c0:1", "owner": 0, "coord": "0,0", "production_queue": []}]
    facade.get_available_production = AsyncMock(return_value=[{"item_id": "SCOUT", "kind": "unit"}])
    model.script = [[use('submit_directive', {"unit_targets": {"SCOUT": 0}})]]
    with pytest.raises(MatchAborted, match="no eligible production"):
        await advance(controller, runtime, facade, 1)
    assert model.posts_sent == 2
    assert not any(isinstance(call, tuple) and call[0] == "set_city_production"
                   for call in facade.calls)
    assert "end_turn" not in facade.calls
    assert records[-1]["audit"] == "strategy_failed"
    assert not any(row["audit"] == "strategy_turn_closed" for row in records)


async def test_refresh_once_for_entire_economy_pass_not_once_per_city(setup):
    controller, runtime, model, facade, records, scout = setup
    facade.cities = [{"city_id": cid, "owner": 0, "coord": "0,0", "production_queue": []}
                     for cid in ("c0:1", "c0:2")]
    facade.get_available_production = AsyncMock(return_value=[{"item_id": "SCOUT", "kind": "unit"}])
    model.script = [[use('submit_directive', {"unit_targets": {"SCOUT": 0}})],
                    [use('submit_directive', {"unit_targets": {"SCOUT": 2}})]]
    with pytest.raises(MatchAborted, match="no eligible production"):
        await advance(controller, runtime, facade, 1)
    assert model.posts_sent == 2
    assert ("set_city_production", "c0:1", "SCOUT") in facade.calls
    assert "end_turn" not in facade.calls


@pytest.mark.parametrize("cap", ["turn", "match"])
async def test_production_refresh_respects_existing_request_caps(setup, cap):
    controller, runtime, model, facade, records, scout = setup
    runtime.llm = replace(runtime.llm, **({"max_tool_rounds": 1} if cap == "turn"
                                         else {"max_requests_per_match": 1}))
    facade.cities = [{"city_id": "c0:1", "owner": 0, "coord": "0,0", "production_queue": []}]
    facade.get_available_production = AsyncMock(return_value=[{"item_id": "SCOUT", "kind": "unit"}])
    model.script = [[use('submit_directive', {"unit_targets": {"SCOUT": 0}})]]
    with pytest.raises(MatchAborted, match="budget exhausted"):
        await advance(controller, runtime, facade, 1)
    assert model.posts_sent == 1
    assert "end_turn" not in facade.calls


async def test_empty_production_catalog_never_asks_model_to_invent_available_item(setup):
    controller, runtime, model, facade, records, scout = setup
    facade.cities = [{"city_id": "c0:1", "owner": 0, "coord": "0,0", "production_queue": []}]
    facade.get_available_production = AsyncMock(return_value=[])
    with pytest.raises(MatchAborted, match="no eligible production"):
        await advance(controller, runtime, facade, 1)
    assert model.posts_sent == 1
    assert "end_turn" not in facade.calls


async def test_scout_exclusion_is_visible_in_real_graph_and_preserves_explicit_role(setup,
                                                                               monkeypatch):
    from civ_arena.agents.scouting import run_scouting
    monkeypatch.setattr('civ_arena.agents.llm.strategic_controller.run_scouting', run_scouting)
    controller, runtime, model, facade, records, scout = setup
    model.script = [[use('submit_directive', {"scouting": {"unit_types": ["WARRIOR"]}})]]
    await advance(controller, runtime, facade, 1)
    assert ("fortify", "u0:1") in facade.calls
    assert not any(isinstance(call, tuple) and call[0] == "move_unit" for call in facade.calls)
    graph = next(row["graph"] for row in records if row["audit"] == "strategy_graph")
    assert graph["unassigned_scout_ids"] == ["u0:1"]
    assert graph["decisions"][0]["assignment_warning"] == "SCOUT omitted from scouting.unit_types"
    assert (graph["execution"][0]["decision"]["assignment_warning"]
            == graph["decisions"][0]["assignment_warning"])


@pytest.mark.parametrize("round_cap,repair_initial,expected_posts,success", [
    (1, False, 1, False), (2, False, 2, True), (2, True, 2, False), (3, True, 3, True)])
async def test_initial_repair_and_economy_refresh_share_logical_turn_cap(setup, round_cap,
                                                                      repair_initial,
                                                                      expected_posts, success):
    controller, runtime, model, facade, records, scout = setup
    runtime.llm = replace(runtime.llm, max_tool_rounds=round_cap)
    facade.cities = [{"city_id": "c0:1", "owner": 0, "coord": "0,0", "production_queue": []}]
    facade.get_available_production = AsyncMock(return_value=[{"item_id": "SCOUT", "kind": "unit"}])
    model.script = ([[text("not a directive")]] if repair_initial else []) + [
        [use('submit_directive', {"unit_targets": {"SCOUT": 0}})],
        [use('submit_directive', {"unit_targets": {"SCOUT": 2}})]]
    if success:
        await advance(controller, runtime, facade, 1)
        assert "end_turn" in facade.calls
        await advance(controller, runtime, facade, 2)
        assert controller._turn_strategy_requests == 0
    else:
        with pytest.raises(MatchAborted, match="budget exhausted"):
            await advance(controller, runtime, facade, 1)
        assert "end_turn" not in facade.calls
    assert model.posts_sent == expected_posts


async def test_late_production_refresh_never_advertises_scouted_units_as_untouched(setup):
    controller, runtime, model, facade, records, scout = setup
    controller.opening_units_frozen = True
    facade.cities = [{"city_id": "c0:1", "owner": 0, "coord": "0,0", "production_queue": []}]
    facade.get_available_production = AsyncMock(return_value=[{"item_id": "SCOUT", "kind": "unit"}])
    model.script = [[use('submit_directive', {"unit_targets": {"SCOUT": 0}})],
                    [use('submit_directive', {"unit_targets": {"SCOUT": 2}})]]
    await advance(controller, runtime, facade, 1)
    authority = [json.loads(request["messages"][0]["content"].split("\n", 1)[0])
                 ["movement_authority"]
                 for request in model.requests]
    assert authority[0]["opening_units_frozen"] is True
    assert authority[0]["untouched_owned_unit_ids"] == ["u0:1"]
    assert authority[1]["opening_units_frozen"] is False
    assert authority[1]["untouched_owned_unit_ids"] == []
    assert authority[1]["natural_allowance"] == "use_projected_movement"


async def test_late_format_repair_cannot_exceed_shared_turn_cap(setup):
    controller, runtime, model, facade, records, scout = setup
    runtime.llm = replace(runtime.llm, max_tool_rounds=2)
    facade.cities = [{"city_id": "c0:1", "owner": 0, "coord": "0,0", "production_queue": []}]
    facade.get_available_production = AsyncMock(return_value=[{"item_id": "SCOUT", "kind": "unit"}])
    model.script = [[use('submit_directive', {"unit_targets": {"SCOUT": 0}})], [text("invalid")],
                    [use('submit_directive', {"unit_targets": {"SCOUT": 2}})]]
    with pytest.raises(MatchAborted, match="no format repair budget"):
        await advance(controller, runtime, facade, 1)
    assert model.posts_sent == 2
    assert "end_turn" not in facade.calls
