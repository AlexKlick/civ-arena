"""Optional Messages tool selection against HTTP transport; no provider I/O."""
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from civ_arena.agents.llm.client import MiniMaxMessagesClient, ModelUnavailable
from civ_arena.config import LLMSpec

MESSAGES = [{'role': 'user', 'content': 'Choose the strategy.'}]
TOOLS = [{'name': 'submit_directive', 'input_schema': {'type': 'object'}}]
CHOICE = {'type': 'tool', 'name': 'submit_directive'}
REPLY = {'content': [{'type': 'tool_use', 'id': 'choice-1',
                      'name': 'submit_directive', 'input': {}}],
         'model': 'MiniMax-M3', 'stop_reason': 'tool_use',
         'usage': {'input_tokens': 17, 'output_tokens': 3}}


def make_client(monkeypatch, handler, *, cap=2000, retries=2):
    monkeypatch.setenv('TEST_TOOL_CHOICE_KEY', 'fixture-secret')
    spec = LLMSpec('https://unused.test/anthropic/v1', 'TEST_TOOL_CHOICE_KEY',
                   'MiniMax-M3', max_requests_per_match=cap, max_retries=retries,
                   max_tokens=4096)
    ledger = []
    client = MiniMaxMessagesClient(spec, on_post=lambda: ledger.append(client.posts_sent))
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, ledger


@pytest.mark.parametrize('mode', ['omitted', 'none', 'named', 'empty'])
async def test_optional_tool_choice_exact_wire_shape(monkeypatch, mode):
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        assert request.url == 'https://unused.test/anthropic/v1/messages'
        assert request.headers['x-api-key'] == 'fixture-secret'
        return httpx.Response(200, json=REPLY)
    client, ledger = make_client(monkeypatch, handler)
    extra = {} if mode == 'omitted' else {'tool_choice': {
        'none': None, 'named': CHOICE, 'empty': {}}[mode]}
    try:
        reply = await client.create(system='System', messages=MESSAGES, tools=TOOLS, **extra)
    finally:
        await client.aclose()
    expected = {'model': 'MiniMax-M3', 'max_tokens': 4096, 'system': 'System',
                'messages': MESSAGES, 'tools': TOOLS}
    if mode in {'named', 'empty'}:
        expected['tool_choice'] = CHOICE if mode == 'named' else {}
    assert captured == [expected]
    assert client.posts_sent == 1 and ledger == [1]
    assert reply.content == REPLY['content']
    assert (reply.input_tokens, reply.output_tokens) == (17, 3)


async def test_named_choice_retried_unchanged_and_every_post_counted(monkeypatch):
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(503) if len(captured) == 1 else httpx.Response(200, json=REPLY)
    monkeypatch.setattr('civ_arena.agents.llm.client.asyncio.sleep', AsyncMock())
    client, ledger = make_client(monkeypatch, handler)
    try:
        await client.create(system='System', messages=MESSAGES, tools=TOOLS, tool_choice=CHOICE)
    finally:
        await client.aclose()
    assert captured[0] == captured[1]
    assert captured[0]['tool_choice'] == CHOICE
    assert client.posts_sent == 2 and ledger == [1, 2]


async def test_named_choice_retry_does_not_bypass_match_budget(monkeypatch):
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(503)
    monkeypatch.setattr('civ_arena.agents.llm.client.asyncio.sleep', AsyncMock())
    client, ledger = make_client(monkeypatch, handler, cap=1)
    try:
        with pytest.raises(ModelUnavailable, match='request budget exhausted'):
            await client.create(system='System', messages=MESSAGES, tools=TOOLS,
                                tool_choice=CHOICE)
    finally:
        await client.aclose()
    assert len(captured) == 1 and captured[0]['tool_choice'] == CHOICE
    assert client.posts_sent == 1 and ledger == [1]


async def test_named_choice_keeps_credential_redaction(monkeypatch):
    client, ledger = make_client(monkeypatch, lambda _: httpx.Response(
        400, text='bad tool request fixture-secret'))
    try:
        with pytest.raises(ModelUnavailable) as exc:
            await client.create(system='System', messages=MESSAGES, tools=TOOLS,
                                tool_choice=CHOICE)
    finally:
        await client.aclose()
    assert 'fixture-secret' not in str(exc.value)
    assert '<redacted>' in str(exc.value)
    assert ledger == [1]
