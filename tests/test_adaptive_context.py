"""Adaptive briefings use exact bound counter receipts, never byte-estimated hard gates."""
import asyncio
import copy
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from civ_arena.agents.llm.client import MiniMaxMessagesClient, ModelUnavailable
from civ_arena.agents.llm.context_curator import CONTEXT_MARKER, ContextCurator, distance
from civ_arena.agents.llm.request_budget import (
    GenerationAdmission,
    TokenCount,
    encoded,
    input_payload,
    payload_hash,
    task_for,
)
from civ_arena.agents.llm.runtime import LLMAgentRuntime
from civ_arena.agents.llm.strategic_controller import StrategicController
from civ_arena.agents.llm.terrain_context import entity_rows, terrain_rows
from civ_arena.agents.runtime import AgentProfile
from civ_arena.arena.referee import MatchAborted
from civ_arena.config import AdaptiveContextSpec, ConfigError, LLMSpec, parse_config
from civ_arena.game.sim.state import tiles_within
from fakes import FakeModel, use
from test_compact_terrain_context import RetainedProjection
from test_config_llm import VALID, llm_doc
from test_strategic_controller import Facade


def harness(monkeypatch, *, tokens=100, window=1000000, cap=2000, replies=None, target=None):
    monkeypatch.setenv('ADAPTIVE_TEST_KEY', 'fixture-not-a-secret')
    policy = AdaptiveContextSpec(window, target, target, target)
    spec = LLMSpec('https://unused.test/anthropic/v1', 'ADAPTIVE_TEST_KEY', 'MiniMax-M3',
                   adaptive_context=policy, max_requests_per_match=cap)
    requests, ledger, kinds = [], [], []
    scripts = list(replies or [[use('submit_directive', {'version': 1})]])
    def handler(request):
        body = json.loads(request.content)
        kind = 'count_tokens' if request.url.path.endswith('count_tokens') else 'generation'
        requests.append((kind, body))
        if kind == 'count_tokens':
            return httpx.Response(200, json={'input_tokens': tokens})
        blocks = scripts.pop(0) if len(scripts) > 1 else scripts[0]
        return httpx.Response(200, json={'content': blocks, 'model': spec.model_id,
            'stop_reason': 'tool_use', 'usage': {'input_tokens': tokens, 'output_tokens': 20}})
    client = MiniMaxMessagesClient(spec, on_post=lambda: ledger.append(client.posts_sent),
                                  on_request_post=kinds.append)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    runtime = LLMAgentRuntime.build(AgentProfile('a', 1, 'llm', 4, llm=spec), client=client)
    runtime.begin_turn(28)
    records = []
    return (runtime, StrategicController('adaptive-fixture', audit=records.append),
            requests, records, ledger, kinds)


@pytest.mark.parametrize('task', ['strategy', 'economy', 'contact'])
async def test_large_roster_retains_every_owned_actor_and_mandatory_tile_past_soft_target(task):
    facade = RetainedProjection()
    sample = facade.state['get_units'][0]
    units, tiles = [], {}
    for i in range(64):
        units.append({**sample, 'unit_id': f'u1:{100000+i}', 'coord': f'{i*5},0'})
        for q, r in tiles_within((i*5, 0), 2):
            tiles[f'{q},{r}'] = {'terrain': 'PLAINS', 'owner_id': -1, 'city_id': ''}
    facade.state['get_units'] = units
    facade.state['get_visible_map']['tiles'] = tiles
    curator = ContextCurator(facade, 1, 8000)
    await curator.refresh()
    before = copy.deepcopy(curator.state)
    rendered = curator.render(adaptive_task=task, target_chars=1)
    doc = json.loads(rendered.removeprefix(CONTEXT_MARKER))
    assert len(rendered) > 8000
    assert len(entity_rows(doc, 'own_units')) == 64
    expected = {key for key in tiles if any(distance(key, unit['coord']) <= 1 for unit in units)}
    assert expected <= {row['coord'] for row in terrain_rows(doc)}
    assert curator.last_render_audit['expanded']
    unlimited = json.loads(curator.render(adaptive_task=task).removeprefix(CONTEXT_MARKER))
    assert {row['coord'] for row in terrain_rows(unlimited)} == set(tiles)
    assert curator.state == before


async def test_contact_focus_adds_only_already_observed_contact_neighborhood():
    facade = RetainedProjection()
    facade.state['get_units'].append({'unit_id':'u63:1', 'owner_id':63, 'type':'WARRIOR',
                                     'coord':'900,900', 'is_barbarian':True})
    facade.state['get_visible_map']['tiles']['900,900'] = {'terrain':'HILL'}
    curator = ContextCurator(facade, 1, 8000)
    await curator.refresh()
    normal = json.loads(curator.render(adaptive_task='strategy').removeprefix(CONTEXT_MARKER))
    contact = json.loads(curator.render(adaptive_task='contact', target_chars=1)
                         .removeprefix(CONTEXT_MARKER))
    assert '900,900' not in {row['coord'] for row in terrain_rows(normal)}
    assert '900,900' in {row['coord'] for row in terrain_rows(contact)}
    assert len(entity_rows(contact, 'visible_foreign_units')) == 1
    assert '901,900' not in {row['coord'] for row in terrain_rows(contact)}


async def test_complete_payload_receipt_and_output_reserve_override_byte_estimate(monkeypatch):
    runtime, controller, requests, records, ledger, kinds = harness(
        monkeypatch, tokens=10, window=4106, target=1)
    curator = ContextCurator(RetainedProjection(), 1, 40)
    await curator.refresh()
    runtime.llm = replace(runtime.llm, max_result_chars=40)
    try:
        await controller._decide(runtime, curator, ['initial_strategy'])
    finally:
        await runtime.aclose()
    assert [kind for kind, _ in requests] == ['count_tokens', 'generation']
    counted, generated = requests[0][1], requests[1][1]
    assert {key:value for key,value in generated.items() if key != 'max_tokens'} == counted
    assert 'max_tokens' not in counted and generated['max_tokens'] == 4096
    assert set(counted) == {'model','system','messages','tools','tool_choice'}
    assert len(counted['messages'][0]['content']) > 40
    result = next(row for row in records if row['audit'] == 'strategy_token_count_result')
    assert result['total_reserved_tokens'] == 4106 and result['outcome'] == 'admitted'
    assert result['canonical_input_bytes'] > result['provider_context_tokens']
    assert result['local_estimate']['hard_admission_authority'] is False
    assert result['input_payload_sha256'] == payload_hash(counted)
    assert kinds == ['count_tokens','generation'] and ledger == [1,2]
    assert [row['request_kind'] for row in records
            if row['audit'] == 'strategy_provider_post'] == kinds
    assert controller._turn_strategy_requests == 1


async def test_provider_counted_window_exhaustion_sends_no_generation(monkeypatch):
    runtime, controller, requests, records, _, _ = harness(monkeypatch, tokens=11, window=4106)
    curator = ContextCurator(RetainedProjection(), 1, 8000)
    await curator.refresh()
    try:
        with pytest.raises(ModelUnavailable, match='exceeds declared window'):
            await controller._decide(runtime, curator, ['initial_strategy'])
    finally:
        await runtime.aclose()
    assert [kind for kind,_ in requests] == ['count_tokens']
    assert records[-1]['outcome'] == 'provider_window_exceeded'


async def test_economy_tactical_rejection_recounts_repair(monkeypatch):
    replies = [[use('submit_directive', {'version':1,'tactical_overrides':[
        {'unit_id':'u1:327681','action':'hold'}]})], [use('submit_directive', {'version':1})]]
    runtime, controller, requests, records, _, _ = harness(monkeypatch, replies=replies)
    curator = ContextCurator(RetainedProjection(), 1, 8000)
    await curator.refresh()
    unit_id = curator.own('get_units')[0]['unit_id']
    replies[0][0]['input']['tactical_overrides'][0]['unit_id'] = unit_id
    try:
        result = await controller._decide(runtime, curator, ['production_targets_satisfied'],
                                          opening_actions_pending=False)
    finally:
        await runtime.aclose()
    assert result['tactical_overrides'] == []
    assert [kind for kind,_ in requests] == ['count_tokens','generation']*2
    assert all(body['tools'][0]['input_schema']['properties']['tactical_overrides']['maxItems']==0
               for _,body in requests)
    assert requests[0][1] != requests[2][1]
    assert 'format_repair' in requests[2][1]['messages'][0]['content']
    response = next(row for row in records if row['audit']=='strategy_response_shape')
    assert response['reason'] == 'tactical_authority_disabled'
    assert controller._turn_strategy_requests == 2


async def test_returned_directive_character_cap_is_still_enforced(monkeypatch):
    runtime, controller, requests, records, _, _ = harness(monkeypatch, replies=[
        [use('submit_directive', {'version':1,'research_preferences':['POTTERY']})],
        [use('submit_directive', {'version':1})]])
    runtime.llm = replace(runtime.llm, max_result_chars=20)
    curator = ContextCurator(RetainedProjection(), 1, 20)
    await curator.refresh()
    try:
        await controller._decide(runtime,curator,['initial_strategy'])
    finally:
        await runtime.aclose()
    assert len(requests)==4
    assert any(row.get('reason')=='arguments_oversized' for row in records)


async def test_counter_consumes_shared_request_cap_before_generation(monkeypatch):
    runtime, controller, requests, _, ledger, _ = harness(monkeypatch, cap=1)
    curator = ContextCurator(RetainedProjection(),1,8000)
    await curator.refresh()
    try:
        with pytest.raises(ModelUnavailable, match='budget exhausted after token count'):
            await controller._decide(runtime,curator,['initial_strategy'])
    finally:
        await runtime.aclose()
    assert len(requests)==1 and ledger==[1] and controller._turn_strategy_requests==0


@pytest.mark.parametrize('bad', [None, True, 0, -1, '17', 1.5])
async def test_count_endpoint_rejects_nonpositive_or_inexact_token_values(monkeypatch,bad):
    runtime, _, _, _, _, _ = harness(monkeypatch)
    await runtime.client._http.aclose()
    runtime.client._http=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request:httpx.Response(200,json={'input_tokens':bad})))
    try:
        with pytest.raises(ModelUnavailable,match='invalid input_tokens'):
            await runtime.client.count_tokens(system='s',messages=[],tools=[])
    finally:
        await runtime.aclose()


async def test_counter_retries_are_bounded_counted_typed_and_redacted(monkeypatch):
    runtime, _, _, _, ledger, kinds = harness(monkeypatch, cap=2)
    await runtime.client._http.aclose()
    runtime.client._http=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request:httpx.Response(503,text='fixture-not-a-secret')))
    monkeypatch.setattr('civ_arena.agents.llm.client.asyncio.sleep',AsyncMock())
    try:
        with pytest.raises(ModelUnavailable) as error:
            await runtime.client.count_tokens(system='s',messages=[],tools=[])
    finally:
        await runtime.aclose()
    assert 'fixture-not-a-secret' not in str(error.value)
    assert ledger==[1,2] and kinds==['count_tokens']*2
    assert runtime.client.posts_by_kind=={'generation':0,'count_tokens':2}


async def test_counter_timeout_and_cancellation_stop_before_generation(monkeypatch):
    runtime, _, _, _, _, _ = harness(monkeypatch)
    await runtime.client._http.aclose()
    started=asyncio.Event()
    async def wait(request):
        started.set()
        await asyncio.Event().wait()
    runtime.client._http=httpx.AsyncClient(transport=httpx.MockTransport(wait))
    runtime.client.spec=replace(runtime.client.spec,request_timeout_s=.02)
    try:
        with pytest.raises(ModelUnavailable,match='deadline'):
            await runtime.client.count_tokens(system='s',messages=[],tools=[])
        started.clear()
        task=asyncio.create_task(runtime.client.count_tokens(system='s',messages=[],tools=[]))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await runtime.aclose()
    assert runtime.client.posts_by_kind=={'generation':0,'count_tokens':2}


@pytest.mark.parametrize('mismatch', ['hash','model','tokens'])
async def test_injected_counter_receipt_must_bind_same_full_input(monkeypatch,mismatch):
    runtime,controller,requests,_,_,_=harness(monkeypatch)
    async def count(**kwargs):
        body=input_payload(runtime.llm.model_id,**kwargs)
        return TokenCount(True if mismatch=='tokens' else 10,
                          'wrong' if mismatch=='model' else runtime.llm.model_id,
                          'wrong' if mismatch=='hash' else payload_hash(body),
                          source='injected_token_counter')
    runtime.client.count_tokens=count
    curator=ContextCurator(RetainedProjection(),1,8000)
    await curator.refresh()
    try:
        with pytest.raises(ModelUnavailable,match='identity mismatch'):
            await controller._decide(runtime,curator,['initial_strategy'])
    finally:
        await runtime.aclose()
    assert not requests


async def test_no_counter_means_explicit_unavailable_not_estimated_fallback():
    spec=LLMSpec('http://unused','UNUSED','fake',adaptive_context=AdaptiveContextSpec(1000000))
    model=FakeModel([[use('submit_directive',{'version':1})]])
    runtime=LLMAgentRuntime.build(AgentProfile('a',1,'llm',4,llm=spec),client=model)
    runtime.begin_turn(28)
    curator=ContextCurator(RetainedProjection(),1,8000)
    await curator.refresh()
    with pytest.raises(ModelUnavailable,match='counter not supported'):
        await StrategicController('fixture')._decide(runtime,curator,['initial_strategy'])
    assert model.posts_sent==0


async def test_quiet_autopilot_has_zero_additional_http_requests(monkeypatch):
    runtime,controller,requests,_,_,_=harness(monkeypatch)
    runtime.profile=replace(runtime.profile,player_id=0)
    monkeypatch.setattr('civ_arena.agents.llm.strategic_controller.run_scouting',
                        AsyncMock(return_value={'nodes':[],'seed':'fixture'}))
    facade=Facade()
    try:
        for turn in (1,2,3):
            runtime.begin_turn(turn)
            await controller.take_turn(runtime,facade)
    finally:
        await runtime.aclose()
    assert [kind for kind,_ in requests]==['count_tokens','generation']


def test_opt_in_configuration_has_no_numeric_soft_defaults_and_validates_fields():
    spec=parse_config(llm_doc({**VALID,'adaptive_context':{'provider_context_tokens':1000000}}))
    policy=spec.agents[0].llm.adaptive_context
    assert policy==AdaptiveContextSpec(1000000)
    assert parse_config(llm_doc(VALID)).agents[0].llm.adaptive_context is None
    for block in ({}, {'provider_context_tokens':True}, {'provider_context_tokens':100,'extra':1},
                  {'provider_context_tokens':100,'economy_target_chars':0}):
        with pytest.raises(ConfigError):
            parse_config(llm_doc({**VALID,'adaptive_context':block}))
    assert task_for(['new_visible_contact'],True)=='contact'
    assert task_for(['research_choice_required'],True)=='strategy'
    assert task_for(['new_visible_contact'],False)=='economy'


async def test_adaptive_runtime_refuses_non_strategic_path():
    spec=LLMSpec('http://unused','UNUSED','fake',adaptive_context=AdaptiveContextSpec(1000000))
    runtime=LLMAgentRuntime.build(AgentProfile('a',0,'llm',4,llm=spec),client=FakeModel([[]]))
    runtime.begin_turn(1)
    with pytest.raises(MatchAborted,match='requires the strategic controller'):
        await runtime.take_turn(Facade())


async def test_generation_output_configuration_must_match_count_reserve(monkeypatch):
    runtime, controller, requests, _, _, _ = harness(monkeypatch)
    runtime.client.spec = replace(runtime.client.spec, max_tokens=8192)
    curator = ContextCurator(RetainedProjection(), 1, 8000)
    await curator.refresh()
    try:
        with pytest.raises(ModelUnavailable, match='generation configuration mismatch'):
            await controller._decide(runtime, curator, ['initial_strategy'])
    finally:
        await runtime.aclose()
    assert not requests


@pytest.mark.parametrize('field,value', [('max_tokens',8192),('model_id','OTHER_MODEL')])
@pytest.mark.parametrize('when', ['after_count','generation_audit'])
async def test_configuration_drift_after_admission_refuses_generation(monkeypatch,field,value,when):
    runtime,controller,requests,records,_,_=harness(monkeypatch,window=4200)
    original=runtime.client.count_tokens
    async def drift(**kwargs):
        receipt=await original(**kwargs)
        runtime.client.spec=replace(runtime.client.spec,**{field:value})
        return receipt
    if when=='after_count':
        runtime.client.count_tokens=drift
    else:
        def audit(record):
            records.append(record)
            if record['audit']=='strategy_request':
                runtime.client.spec=replace(runtime.client.spec,**{field:value})
        controller.audit=audit
    curator=ContextCurator(RetainedProjection(),1,8000)
    await curator.refresh()
    try:
        with pytest.raises(ModelUnavailable,match='generation configuration mismatch'):
            await controller._decide(runtime,curator,['initial_strategy'])
    finally:
        await runtime.aclose()
    assert [kind for kind,_ in requests]==['count_tokens']
    assert runtime.client.posts_by_kind=={'count_tokens':1,'generation':0}


@pytest.mark.parametrize('field,value', [('max_tokens',8192),('model_id','OTHER_MODEL')])
async def test_post_hook_drift_cannot_change_serialized_admitted_generation(
        monkeypatch,field,value):
    runtime,controller,requests,_,_,_=harness(monkeypatch,window=4200)
    def hook(kind):
        if kind=='generation':
            runtime.client.spec=replace(runtime.client.spec,**{field:value})
    runtime.client.on_request_post=hook
    curator=ContextCurator(RetainedProjection(),1,8000)
    await curator.refresh()
    try:
        await controller._decide(runtime,curator,['initial_strategy'])
    finally:
        await runtime.aclose()
    counted,generated=[body for _,body in requests]
    assert {k:v for k,v in generated.items() if k!='max_tokens'}==counted
    assert generated['model']=='MiniMax-M3' and generated['max_tokens']==4096
    assert runtime.client.posts_by_kind=={'count_tokens':1,'generation':1}


async def test_admitted_body_is_immutable_across_hook_mutation_and_transport_retries(monkeypatch):
    runtime,_,_,_,_,_=harness(monkeypatch)
    client=runtime.client
    await client._http.aclose()
    sent=[]
    def handler(request):
        sent.append(bytes(request.content))
        if len(sent)==1:
            return httpx.Response(503,json={})
        assert request.headers['content-type']=='application/json'
        return httpx.Response(200,json={'content':[]})
    client._http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr('civ_arena.agents.llm.client.asyncio.sleep',AsyncMock())
    kwargs={'system':'original','messages':[{'role':'user','content':'original'}],
            'tools':[{'name':'original'}],'tool_choice':{'type':'tool','name':'original'}}
    body=input_payload(client.spec.model_id,**kwargs)
    receipt=TokenCount(100,client.spec.model_id,payload_hash(body),'injected_token_counter')
    admission=GenerationAdmission(encoded({**body,'max_tokens':4096}),receipt,4200)
    original=client._post_doc
    async def wrapped(body,kind,path,**kw):
        # Simulates caller-owned dictionaries changed immediately before POST.
        def mutate():
            body.update(model='OTHER_MODEL',max_tokens=8192,system='changed',
                        messages=[],tools=[],tool_choice={'type':'none'})
            kwargs['messages'][0]['content']='changed'
        client.on_post=mutate
        return await original(body,kind,path,**kw)
    client._post_doc=wrapped
    try:
        reply=await client.create_admitted(admission=admission)
    finally:
        await runtime.aclose()
    assert sent==[admission.request_json.encode()]*2
    assert reply.model=='MiniMax-M3'
    assert admission.body()['messages'][0]['content']=='original'
    assert client.posts_by_kind=={'generation':2,'count_tokens':0}


@pytest.mark.parametrize('change', ['window','count','input','reserve','extra'])
async def test_invalid_admission_never_posts(monkeypatch,change):
    runtime,_,requests,_,_,_=harness(monkeypatch)
    body=input_payload('MiniMax-M3',system='s',messages=[],tools=[])
    receipt=TokenCount(100,'MiniMax-M3',payload_hash(body),'injected_token_counter')
    body['max_tokens']=4096
    window=4200
    if change=='window':
        window=4195
    elif change=='count':
        receipt=replace(receipt,input_tokens=True)
    elif change=='input':
        body['tools']=[{'name':'uncounted'}]
    elif change=='reserve':
        body['max_tokens']=8192
    elif change=='extra':
        body['uncounted_extra']='not allowed'
    admission=GenerationAdmission(encoded(body),receipt,window)
    try:
        with pytest.raises(ModelUnavailable,match='invalid generation admission'):
            await runtime.client.create_admitted(admission=admission)
    finally:
        await runtime.aclose()
    assert requests==[] and runtime.client.posts_sent==0


async def test_count_and_admitted_generation_use_same_canonical_field_serialization(monkeypatch):
    runtime,controller,_,_,_,_=harness(monkeypatch)
    await runtime.client._http.aclose()
    sent=[]
    def handler(request):
        doc=json.loads(request.content)
        assert request.content==encoded(doc).encode()
        assert request.headers['content-type']=='application/json'
        sent.append(doc)
        if request.url.path.endswith('count_tokens'):
            return httpx.Response(200,json={'input_tokens':100})
        return httpx.Response(200,json={'content':[use('submit_directive',{'version':1})]})
    runtime.client._http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    curator=ContextCurator(RetainedProjection(),1,8000)
    await curator.refresh()
    try:
        await controller._decide(runtime,curator,['initial_strategy'])
    finally:
        await runtime.aclose()
    assert len(sent)==2
    assert {k:v for k,v in sent[1].items() if k!='max_tokens'}==sent[0]
