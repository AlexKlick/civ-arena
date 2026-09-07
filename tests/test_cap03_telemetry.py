"""CAP-03 slice 2: decision-linked telemetry + evidence-qualified export.

F-07 repair: attempt records gain logical identity — one
``logical_request_id`` across a request's retries (uuid per _post_doc
call), a ``request_set_key`` (payload_hash + kind) so joins can tell
same-payload-distinct-decision cases apart, and the ambient
``decision_id`` assigned by the runtime/controller. Absent usage maps to
``null``, never 0 — an unknown is not a zero. Ledger write failures
isolate: a sink fault never fails or retransmits the provider request.
"""


import httpx

from civ_arena.agents.llm.client import MiniMaxMessagesClient
from civ_arena.config import LLMSpec

SECRET = "sk-cap03-test-secret"


def _spec(wire_log: bool = False) -> LLMSpec:
    return LLMSpec(base_url="https://api.example.invalid",
                   api_key_env="CAP03_TEST_KEY", model_id="test-model",
                   wire_log=wire_log)


def _reply_usage(usage: dict | None) -> dict:
    doc = {"content": [{"type": "text", "text": "ok"}], "model": "test-model"}
    if usage is not None:
        doc["usage"] = usage
    return doc


def _client(monkeypatch, handler, **client_kwargs) -> MiniMaxMessagesClient:
    monkeypatch.setenv("CAP03_TEST_KEY", SECRET)
    transport = httpx.MockTransport(handler)
    client = MiniMaxMessagesClient(_spec(), **client_kwargs)
    client._http = httpx.AsyncClient(transport=transport)  # noqa: SLF001
    return client


async def _create(client, system: str = "s"):
    return await client.create(system=system, messages=[
        {"role": "user", "content": "hi"}], tools=[])


# -- correlation identity ------------------------------------------------------

async def test_retry_chain_shares_one_logical_request_id(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="again")
        return httpx.Response(200, json=_reply_usage(
            {"input_tokens": 5, "output_tokens": 2}))

    records: list[dict] = []
    client = _client(monkeypatch, handler, on_attempt=records.append)
    await _create(client)
    assert len(records) == 2
    ids = {r["logical_request_id"] for r in records}
    assert len(ids) == 1 and next(iter(ids))  # one id across the retry
    assert records[0]["attempt"] == 0 and records[1]["attempt"] == 1
    assert records[0]["request_set_key"] == records[1]["request_set_key"]
    assert records[0]["request_set_key"].endswith(":generation")


async def test_same_payload_on_two_decisions_has_distinct_request_identity(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_reply_usage(
            {"input_tokens": 5, "output_tokens": 2}))

    records: list[dict] = []
    client = _client(monkeypatch, handler, on_attempt=records.append)
    await _create(client)          # decision 1 (decision_id unset -> null)
    client.decision_id = "dec-alpha"
    await _create(client)          # decision 2: byte-identical payload
    await _create(client)          # decision 3: same decision_id, new request
    # identical payload => identical request_set_key ...
    assert len({r["request_set_key"] for r in records}) == 1
    # ... but every logical request is distinct ...
    assert len({r["logical_request_id"] for r in records}) == 3
    # ... and the decision attribution rides along (null when unassigned)
    assert [r["decision_id"] for r in records] == \
        [None, "dec-alpha", "dec-alpha"]


# -- unknown-usage semantics ---------------------------------------------------

async def test_missing_usage_remains_unknown_not_zero(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_reply_usage(None))  # no usage key

    records: list[dict] = []
    client = _client(monkeypatch, handler, on_attempt=records.append)
    await _create(client)
    assert records[0]["input_tokens"] is None
    assert records[0]["output_tokens"] is None


async def test_explicit_zero_usage_is_zero_not_unknown(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_reply_usage(
            {"input_tokens": 0, "output_tokens": 0}))

    records: list[dict] = []
    client = _client(monkeypatch, handler, on_attempt=records.append)
    await _create(client)
    assert records[0]["input_tokens"] == 0 and records[0]["output_tokens"] == 0


async def test_usage_alias_spellings_resolve(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_reply_usage(
            {"prompt_tokens": 7, "completion_tokens": 3}))

    records: list[dict] = []
    client = _client(monkeypatch, handler, on_attempt=records.append)
    await _create(client)
    assert records[0]["input_tokens"] == 7 and records[0]["output_tokens"] == 3
