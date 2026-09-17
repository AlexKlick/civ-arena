"""Lane D flash client: MockTransport only — no network, no real keys."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from civ_arena.research.flash import (
    BACKOFF_S,
    MAX_TOKENS,
    TEMPERATURE,
    FlashClient,
    FlashSpec,
    first_json_object,
    spec_from_env,
)

KEY_ENV = "LANE_D_FLASH_KEY"
SECRET = "fixture-secret-key-0123456789"


class PeakTransport(httpx.AsyncBaseTransport):
    """Counts peak in-flight requests; yields once so tasks truly overlap."""

    def __init__(self) -> None:
        self.inflight = 0
        self.peak = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        await asyncio.sleep(0)
        self.inflight -= 1
        return openai_reply({"n": 1})


DEFAULT_USAGE = {"prompt_tokens": 11, "completion_tokens": 3}


def openai_reply(payload=None, *, model="glm-4.5-flash", usage=DEFAULT_USAGE,
                 content=None):
    """An OpenAI chat-completions 200: ``payload`` as the JSON content, or
    a raw ``content`` string for parse-path tests. ``usage=None`` omits the
    usage key entirely (the absent-usage path)."""
    body = content if content is not None else (
        payload if isinstance(payload, str) else json.dumps(payload))
    doc = {"model": model,
           "choices": [{"index": 0, "finish_reason": "stop",
                        "message": {"role": "assistant", "content": body}}]}
    if usage is not None:
        doc["usage"] = usage
    return httpx.Response(200, json=doc)


def make_client(monkeypatch, handler, tmp_path, *, key=SECRET, **spec_kw):
    """A FlashClient on an injected transport, spend file under tmp_path."""
    if key is None:
        monkeypatch.delenv(KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(KEY_ENV, key)
    spec = FlashSpec(api_key_env=KEY_ENV, **spec_kw)
    spend = tmp_path / "spend" / "flash.jsonl"
    client = FlashClient(spec, spend_path=spend,
                         transport=httpx.MockTransport(handler))
    return client, spend


def spend_rows(spend):
    return [json.loads(line) for line in spend.read_text().splitlines()]


async def test_ok_path_parses_and_writes_one_spend_row(monkeypatch, tmp_path):
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return openai_reply({"answer": 42, "nested": {"ok": True}})

    client, spend = make_client(monkeypatch, handler, tmp_path)
    try:
        result = await client.call_json("hello", purpose="unit-test")
    finally:
        await client.aclose()

    assert result.status == "ok"
    assert result.data == {"answer": 42, "nested": {"ok": True}}
    assert result.raw == ""
    assert result.attempts == 1 and result.call_id
    assert result.model == "glm-4.5-flash"
    assert (result.input_tokens, result.output_tokens) == (11, 3)
    assert client.calls_sent == 1 and client.attempts_sent == 1
    assert captured == [{"model": "glm-4.5-flash",
                         "messages": [{"role": "user", "content": "hello"}],
                         "response_format": {"type": "json_object"},
                         "max_tokens": MAX_TOKENS,
                         "temperature": TEMPERATURE}]
    rows = spend_rows(spend)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "ok" and row["attempt"] == 1
    assert row["purpose"] == "unit-test" and row["call_id"] == result.call_id
    assert (row["input_tokens"], row["output_tokens"]) == (11, 3)


async def test_wire_shape_system_message_and_bearer_header(monkeypatch, tmp_path):
    captured = []

    def handler(request):
        captured.append((request.headers["authorization"], json.loads(request.content)))
        return openai_reply({"ok": True})

    client, spend = make_client(monkeypatch, handler, tmp_path)
    try:
        await client.call_json("the prompt", purpose="wire", system="Be terse.")
    finally:
        await client.aclose()

    auth, body = captured[0]
    assert auth == f"Bearer {SECRET}"
    assert body["messages"] == [{"role": "system", "content": "Be terse."},
                                {"role": "user", "content": "the prompt"}]
    assert len(spend.read_text().splitlines()) == 1


async def test_bracket_scan_recovers_embedded_object(monkeypatch, tmp_path):
    noisy = ('Sure! Here is the JSON you asked for:\n```json\n'
             '{"answer": 7, "note": "brace } inside", "nested": {"x": 1}}\n'
             "```\nHope that helps.")
    client, _ = make_client(monkeypatch, lambda r: openai_reply(content=noisy),
                            tmp_path)
    try:
        result = await client.call_json("hi", purpose="scan")
    finally:
        await client.aclose()
    assert result.status == "ok"
    assert result.data == {"answer": 7, "note": "brace } inside",
                           "nested": {"x": 1}}


async def test_non_object_json_body_recovers_object(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, lambda r: openai_reply(content='[{"a": 5}]'),
                            tmp_path)
    try:
        result = await client.call_json("hi", purpose="wrapped")
    finally:
        await client.aclose()
    assert result.status == "ok" and result.data == {"a": 5}


@pytest.mark.parametrize("body", ["no braces at all", '{"unclosed": [1, 2'])
async def test_garbage_body_parse_error_keeps_raw_and_writes_row(
        monkeypatch, tmp_path, body):
    client, spend = make_client(monkeypatch, lambda r: openai_reply(content=body),
                                tmp_path)
    try:
        result = await client.call_json("hi", purpose="garbage")
    finally:
        await client.aclose()
    assert result.status == "parse_error"
    assert result.data == {}
    assert result.raw == body
    assert result.attempts == 1
    assert [row["status"] for row in spend_rows(spend)] == ["parse_error"]


async def test_parse_error_raw_is_key_redacted(monkeypatch, tmp_path):
    noisy = f"oops {SECRET} and no json here"
    client, _ = make_client(monkeypatch, lambda r: openai_reply(content=noisy),
                            tmp_path)
    try:
        result = await client.call_json("hi", purpose="redact")
    finally:
        await client.aclose()
    assert result.status == "parse_error"
    assert SECRET not in result.raw and "<redacted>" in result.raw


async def test_429_then_200_retries_with_full_jitter(monkeypatch, tmp_path):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(429) if len(seen) == 1 else openai_reply({"ok": True})

    client, spend = make_client(monkeypatch, handler, tmp_path)
    try:
        result = await client.call_json("hi", purpose="retry")
    finally:
        await client.aclose()

    assert result.status == "ok" and result.attempts == 2
    assert client.calls_sent == 1 and client.attempts_sent == 2
    assert len(sleeps) == 1 and 0 <= sleeps[0] <= BACKOFF_S
    rows = spend_rows(spend)
    assert [(row["status"], row["attempt"]) for row in rows] == [
        ("http_error", 1), ("ok", 2)]


async def test_retry_exhaustion_returns_http_error(monkeypatch, tmp_path):
    async def fake_sleep(delay):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    client, spend = make_client(monkeypatch, lambda r: httpx.Response(503),
                                tmp_path, max_retries=2)
    try:
        result = await client.call_json("hi", purpose="down")
    finally:
        await client.aclose()
    assert result.status == "http_error" and result.data == {}
    assert result.attempts == 3 and client.attempts_sent == 3
    assert [row["attempt"] for row in spend_rows(spend)] == [1, 2, 3]


async def test_non_retryable_status_fails_fast(monkeypatch, tmp_path):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    client, spend = make_client(monkeypatch, lambda r: httpx.Response(401), tmp_path)
    try:
        result = await client.call_json("hi", purpose="auth")
    finally:
        await client.aclose()
    assert result.status == "http_error" and result.attempts == 1
    assert sleeps == []
    assert [row["attempt"] for row in spend_rows(spend)] == [1]


async def test_transport_error_retried_then_ok(monkeypatch, tmp_path):
    async def fake_sleep(delay):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("boom")
        return openai_reply({"ok": True})

    client, spend = make_client(monkeypatch, handler, tmp_path)
    try:
        result = await client.call_json("hi", purpose="transport")
    finally:
        await client.aclose()
    assert result.status == "ok" and result.attempts == 2
    assert [row["status"] for row in spend_rows(spend)] == ["http_error", "ok"]


async def test_budget_exhausted_fail_soft(monkeypatch, tmp_path):
    posts = []

    def handler(request):
        posts.append(request)
        return openai_reply({"n": len(posts)})

    client, spend = make_client(monkeypatch, handler, tmp_path, max_calls=2)
    first = await client.call_json("a", purpose="one")
    second = await client.call_json("b", purpose="two")
    third = await client.call_json("c", purpose="three")
    await client.aclose()
    assert (first.status, second.status) == ("ok", "ok")
    assert third.status == "budget_exhausted"
    assert third.data == {} and third.attempts == 0
    assert len(posts) == 2  # no HTTP call was made for the third
    assert [row["status"] for row in spend_rows(spend)] == ["ok", "ok"]
    assert await client.probe() is False  # the probe burns budget too


async def test_missing_key_makes_no_call_and_no_row(monkeypatch, tmp_path):
    def handler(request):
        raise AssertionError("no HTTP call expected without a key")

    client, spend = make_client(monkeypatch, handler, tmp_path, key=None)
    result = await client.call_json("hi", purpose="nokey")
    assert result.status == "no_key" and result.data == {} and result.attempts == 0
    assert client.calls_sent == 0  # a missing key burns nothing
    assert not spend.exists()
    monkeypatch.setenv(KEY_ENV, "")  # a blank value counts as missing too
    assert (await client.call_json("hi", purpose="nokey")).status == "no_key"
    await client.aclose()


async def test_probe_true_and_request_shape(monkeypatch, tmp_path):
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return openai_reply({"reply": "PONG"})

    client, spend = make_client(monkeypatch, handler, tmp_path)
    try:
        assert await client.probe() is True
    finally:
        await client.aclose()
    assert captured[0]["max_tokens"] == 8
    assert captured[0]["messages"] == [{"role": "user", "content": "Reply PONG"}]
    assert spend_rows(spend)[0]["purpose"] == "probe"


async def test_probe_false_when_endpoint_down(monkeypatch, tmp_path):
    async def fake_sleep(delay):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    client, _ = make_client(monkeypatch, lambda r: httpx.Response(503), tmp_path,
                            max_retries=1)
    try:
        assert await client.probe() is False
    finally:
        await client.aclose()


async def test_probe_false_when_reply_is_not_json(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, lambda r: openai_reply(content="PONG"),
                            tmp_path)
    try:
        assert await client.probe() is False
    finally:
        await client.aclose()


@pytest.mark.parametrize("usage,expected", [
    ({"prompt_tokens": 5, "completion_tokens": 2}, (5, 2)),
    ({"input_tokens": 7, "output_tokens": 3}, (7, 3)),
    ({}, (0, 0)),
    (None, (0, 0)),
    ({"input_tokens": "nope", "output_tokens": None}, (0, 0)),
])
async def test_usage_aliases_tolerated(monkeypatch, tmp_path, usage, expected):
    client, _ = make_client(monkeypatch, lambda r: openai_reply({"a": 1}, usage=usage),
                            tmp_path)
    try:
        result = await client.call_json("hi", purpose="tokens")
    finally:
        await client.aclose()
    assert (result.input_tokens, result.output_tokens) == expected


async def test_concurrency_capped_by_semaphore(monkeypatch, tmp_path):
    transport = PeakTransport()
    monkeypatch.setenv(KEY_ENV, SECRET)
    client = FlashClient(FlashSpec(api_key_env=KEY_ENV, concurrency=2),
                         spend_path=tmp_path / "spend.jsonl", transport=transport)
    results = await asyncio.gather(*(client.call_json(f"p{i}", purpose=f"par{i}")
                                     for i in range(3)))
    await client.aclose()
    assert all(result.status == "ok" for result in results)
    assert transport.peak == 2
    assert client.calls_sent == 3 and client.attempts_sent == 3


async def test_key_and_prompt_never_reach_spend_rows(monkeypatch, tmp_path):
    echoed = json.dumps({"note": SECRET, "prompt": "the prompt", "payload": {"a": 1}})
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(500, text=f"auth failed for {SECRET}")
        return openai_reply(content=echoed)  # a valid object that embeds the key

    client, spend = make_client(monkeypatch, handler, tmp_path)
    try:
        result = await client.call_json("the prompt", purpose="leak")
    finally:
        await client.aclose()
    assert result.status == "ok" and result.attempts == 2
    text = spend.read_text()
    assert SECRET not in text
    assert "the prompt" not in text
    assert "Bearer" not in text


def test_first_json_object_string_aware():
    assert first_json_object('noise {"a": "}{" } tail') == '{"a": "}{" }'
    assert first_json_object("no braces") is None
    assert first_json_object('{"unclosed": [1, 2') is None


def test_default_spec_pins_the_lane():
    spec = FlashSpec()
    assert spec.base_url == "https://api.z.ai/api/coding/paas/v4/chat/completions"
    assert spec.api_key_env == "ZAI_CODING_API_KEY"
    assert spec.model == "glm-4.5-flash"
    assert (spec.concurrency, spec.max_retries, spec.timeout_s,
            spec.max_calls) == (2, 5, 120.0, 12)


def test_spec_from_env_overrides(monkeypatch):
    monkeypatch.setenv("RESEARCH_FLASH_BASE_URL", "https://alt.test/v4/chat/completions")
    monkeypatch.setenv("RESEARCH_FLASH_API_KEY_ENV", "OTHER_KEY_ENV")
    monkeypatch.setenv("RESEARCH_FLASH_MODEL", "glm-4.6")
    spec = spec_from_env()
    assert spec.base_url == "https://alt.test/v4/chat/completions"
    assert spec.api_key_env == "OTHER_KEY_ENV"
    assert spec.model == "glm-4.6"
    assert (spec.concurrency, spec.max_retries, spec.max_calls) == (2, 5, 12)


def test_spec_from_env_defaults_when_unset_or_blank(monkeypatch):
    for var in ("RESEARCH_FLASH_BASE_URL", "RESEARCH_FLASH_API_KEY_ENV",
                "RESEARCH_FLASH_MODEL"):
        monkeypatch.delenv(var, raising=False)
    assert spec_from_env() == FlashSpec()
    monkeypatch.setenv("RESEARCH_FLASH_MODEL", "   ")
    assert spec_from_env().model == FlashSpec().model
