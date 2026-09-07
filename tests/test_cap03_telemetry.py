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
import pytest

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


# -- decision boundaries (strategic controller) --------------------------------

@pytest.fixture
def controller_env(monkeypatch):
    """Same harness shape as test_strategic_controller.setup, local so the
    decision-boundary assertions own their construction."""
    from unittest.mock import AsyncMock

    from civ_arena.agents.llm.runtime import LLMAgentRuntime
    from civ_arena.agents.llm.strategic_controller import StrategicController
    from civ_arena.agents.runtime import AgentProfile
    from civ_arena.config import LLMSpec
    from fakes import FakeModel, use
    from test_strategic_controller import Facade

    scout = AsyncMock(return_value={'nodes': [], 'seed': 'fixture'})
    monkeypatch.setattr(
        'civ_arena.agents.llm.strategic_controller.run_scouting', scout)
    model = FakeModel([[use('submit_directive', {'version': 1})]])
    llm = LLMSpec('http://unused', 'UNUSED', 'fake')
    runtime = LLMAgentRuntime.build(
        AgentProfile('a', 0, 'llm', 4, llm=llm), client=model)
    records = []
    controller = StrategicController('cap03-test', cadence=5,
                                     audit=records.append)
    return controller, runtime, model, Facade(), records


async def _advance(controller, runtime, facade, turn):
    runtime.begin_turn(turn)
    await controller.take_turn(runtime, facade)


async def test_quiet_directive_turn_records_zero_provider_calls_and_actual_tool_cost(
        controller_env):
    controller, runtime, model, facade, records = controller_env
    # this harness fires model decisions at turns 1 and 6 (initial review +
    # cadence 5); turns 2-5 are quiet procedural turns
    await _advance(controller, runtime, facade, 1)
    for turn in range(2, 6):
        before = model.posts_sent
        tool_calls = len(facade.calls)
        await _advance(controller, runtime, facade, turn)
        boundaries = [r for r in records if r['audit'] == 'decision_boundary'
                      and r['turn'] == turn]
        assert len(boundaries) == 1
        b = boundaries[0]
        assert b['source'] == 'autopilot'
        assert b['provider_requests'] == model.posts_sent - before == 0
        assert b['decision_id']
        # quiet turns still execute procedurally — the facade saw work
        assert len(facade.calls) > tool_calls


async def test_decision_boundary_carries_ids_on_model_turns(controller_env):
    controller, runtime, model, facade, records = controller_env
    for turn in range(1, 7):
        await _advance(controller, runtime, facade, turn)
    model_turns = [r for r in records if r['audit'] == 'decision_boundary'
                   and r['source'] == 'model']
    assert [b['turn'] for b in model_turns] == [1, 6]
    b = model_turns[-1]
    assert b['provider_requests'] >= 1
    assert b['directive_id']  # accepted directive got its own id
    # every boundary turn has a distinct decision id
    ids = [r['decision_id'] for r in records
           if r['audit'] == 'decision_boundary']
    assert len(set(ids)) == len(ids) == 6
    # the client carried the CURRENT decision id during the model call
    assert model.decision_id == b['decision_id']


async def test_legacy_runtime_turns_assign_decision_ids():
    from civ_arena.agents.llm.runtime import LLMAgentRuntime
    from civ_arena.agents.runtime import AgentProfile
    from civ_arena.config import LLMSpec
    from fakes import FakeModel, use
    from test_strategic_controller import Facade

    model = FakeModel([[use('end_turn', {})]])
    llm = LLMSpec('http://unused', 'UNUSED', 'fake')
    runtime = LLMAgentRuntime.build(
        AgentProfile('a', 0, 'llm', 4, llm=llm), client=model)
    runtime.begin_turn(1)
    await runtime.take_turn(Facade())
    assert model.decision_id  # legacy turns are decision units too
