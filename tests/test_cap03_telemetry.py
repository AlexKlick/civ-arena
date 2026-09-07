"""CAP-03 slice 2: decision-linked telemetry + evidence-qualified export.

F-07 repair: attempt records gain logical identity — one
``logical_request_id`` across a request's retries (uuid per _post_doc
call), a ``request_set_key`` (payload_hash + kind) so joins can tell
same-payload-distinct-decision cases apart, and the ambient
``decision_id`` assigned by the runtime/controller. Absent usage maps to
``null``, never 0 — an unknown is not a zero. Ledger write failures
isolate: a sink fault never fails or retransmits the provider request.
"""


import json

import httpx
import pytest

from civ_arena.agents.llm.client import MiniMaxMessagesClient, ModelUnavailable
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


# -- sink isolation + callback preservation ------------------------------------

def _fake_client_with_counters():
    class _FakeClient:
        def __init__(self):
            self.posts_sent = 0
            self.on_post = None
            self.on_attempt = None
    return _FakeClient()


def _wired(agent_llm, tmp_path, audits):
    from civ_arena.config import AgentSpec
    from civ_arena.game.civ6.live_driver import _wire_client_sinks
    agent = AgentSpec(agent_id="s0", player_id=0, policy="llm", seed=1,
                      llm=agent_llm)
    client = _fake_client_with_counters()
    _wire_client_sinks(client, agent, lambda tag, **f: audits.append(
        {"audit": tag, **f}), tmp_path)
    return client


def test_sink_failure_does_not_resubmit_or_fail_the_request(tmp_path):
    import json as _json

    import pytest as _pytest

    from civ_arena.agents.llm.wire_log import CostLedger
    from civ_arena.config import LLMSpec

    audits: list[dict] = []
    client = _wired(LLMSpec(base_url="https://x.invalid", api_key_env="E",
                            model_id="m", wire_log=True), tmp_path, audits)
    record = {"ts": "t", "request_kind": "generation", "attempt": 0,
              "status_code": 200, "latency_ms": 3, "model": "m",
              "payload_hash": "h", "request": {}, "response": None,
              "error": None, "input_tokens": None, "output_tokens": None,
              "decision_id": "d1", "logical_request_id": "r1",
              "request_set_key": "h:generation"}

    def boom(*a, **k):
        raise OSError("disk full")

    from civ_arena.agents.llm import wire_log as _wl
    from civ_arena.arena.spend import SpendLedger
    # ALL three sinks fail. The request path must not raise.
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(CostLedger, "note", boom)
        mp.setattr(_wl.WireLog, "note", boom)
        mp.setattr(SpendLedger, "note", boom)
        client.posts_sent = 1
        client.on_post()          # spend fails -> audited, on_post survives
        client.on_attempt(record)  # costs+wire fail -> each audited once
        client.on_attempt(record)  # second failure: no duplicate audit
    assert not (tmp_path / "llm_costs.jsonl").exists()
    failures = [a for a in audits if a["audit"] == "ledger_write_failed"]
    assert {a["sink"] for a in failures} == {"spend", "costs", "wire"}
    # each sink audited exactly ONCE
    assert len(failures) == 3
    # the provider_request audit still fired (the request is unaffected)
    assert any(a["audit"] == "provider_request" for a in audits)
    # and a RECOVERED ledger works again for a DIFFERENT sink instance
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    ok = CostLedger(fresh)
    ok.note("s0", 0, record)
    assert "d1" in _json.dumps(
        _json.loads((tmp_path / "fresh" / "llm_costs.jsonl")
                    .read_text().splitlines()[0]))


def test_cost_ledger_lines_carry_logical_identity(tmp_path):
    from civ_arena.agents.llm.wire_log import CostLedger
    costs = CostLedger(tmp_path)
    costs.note("s0", 0, {"ts": "t", "request_kind": "generation",
                         "attempt": 1, "status_code": 200, "latency_ms": 9,
                         "model": "m", "payload_hash": "h",
                         "decision_id": "d1", "logical_request_id": "lr1",
                         "request_set_key": "h:generation"})
    row = json.loads((tmp_path / "llm_costs.jsonl").read_text()
                     .splitlines()[0])
    assert row["decision_id"] == "d1"
    assert row["logical_request_id"] == "lr1"
    assert row["request_set_key"] == "h:generation"


async def test_sink_failure_does_not_resubmit_successful_provider_request(
        monkeypatch, tmp_path):
    """End-to-end: the provider 200s while BOTH ledger writes raise — the
    reply returns normally, no second POST happens."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_reply_usage(
            {"input_tokens": 1, "output_tokens": 1}))

    client = _client(monkeypatch, handler)
    audits: list[dict] = []
    from civ_arena.agents.llm import wire_log as _wl
    from civ_arena.agents.llm.wire_log import CostLedger
    from civ_arena.arena.spend import SpendLedger
    from civ_arena.config import AgentSpec
    from civ_arena.game.civ6.live_driver import _wire_client_sinks
    agent = AgentSpec(agent_id="e2e", player_id=0, policy="llm", seed=1,
                      llm=_spec(wire_log=True))
    _wire_client_sinks(client, agent,
                       lambda tag, **f: audits.append({"audit": tag, **f}),
                       tmp_path)

    def boom(*a, **k):
        raise OSError("ledger io failure")

    with pytest.MonkeyPatch.context() as mp_ctx:
        mp_ctx.setattr(CostLedger, "note", boom)
        mp_ctx.setattr(_wl.WireLog, "note", boom)
        mp_ctx.setattr(SpendLedger, "note", boom)
        reply = await _create(client)  # must NOT raise, must NOT retry
    assert reply.input_tokens == 1
    assert client.posts_sent == 1  # exactly one POST — no retransmission
    assert {a["sink"] for a in audits
            if a["audit"] == "ledger_write_failed"} == {"spend", "costs",
                                                        "wire"}


async def test_wire_redaction_handles_key_used_at_send_time(monkeypatch, tmp_path):
    """Key rotation mid-flight: attempt 1 sends the OLD key (a terminal
    error echoes it back), then the env rotates and attempt 2 sends the
    NEW key (also echoed). Each record must redact the key ACTUALLY SENT
    on that attempt — the old key's echo must not survive via the new
    sweep and vice versa."""
    old_key = SECRET
    new_key = "sk-cap03-rotated-key"
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        echoed = old_key if calls["n"] == 1 else new_key
        return httpx.Response(400, text=f"bad key {echoed}")

    monkeypatch.setenv("CAP03_TEST_KEY", old_key)
    transport = httpx.MockTransport(handler)
    from civ_arena.agents.llm.wire_log import WireLog
    client = MiniMaxMessagesClient(_spec(wire_log=True))
    client._http = httpx.AsyncClient(transport=transport)  # noqa: SLF001
    wire = WireLog(tmp_path, "rot", 0, _spec(wire_log=True))
    client.on_attempt = wire.note
    with pytest.raises(ModelUnavailable):
        await _create(client)
    # rotate AFTER attempt 1 booked; attempt 2 sends the new key
    monkeypatch.setenv("CAP03_TEST_KEY", new_key)
    with pytest.raises(ModelUnavailable):
        await _create(client)
    text = (tmp_path / "llm-wire" / "rot.jsonl").read_text()
    assert old_key not in text
    assert new_key not in text
    assert text.count("<redacted>") == 2  # one per echoed attempt
