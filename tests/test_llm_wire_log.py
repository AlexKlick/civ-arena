"""Wire ledgers: per-attempt costs (always on) + opt-in wire transcripts.

The client's on_attempt fires after EVERY counted attempt (success,
retryable, terminal) with the full request/response record; on_post
semantics are untouched (budget/spend parity). Redaction: headers are
never recorded, error strings are the client's already-redacted forms,
and the WireLog sweeps the serialized record with the live env key —
pinned by a marker the test source never spells out (the
directive-diagnostics discipline).
"""

import json
import stat

import httpx
import pytest

from civ_arena.agents.llm.client import MiniMaxMessagesClient, ModelUnavailable
from civ_arena.agents.llm.wire_log import CostLedger, WireLog
from civ_arena.config import LLMSpec

# never spelled out in test source (asserted absent from nothing — it must
# SURVIVE the round trip verbatim in the wire transcript)
MARKER = "PRIVATE_" + "MODEL_CONTENT_MUST_NOT_BE_COPIED"

SPEC = LLMSpec(base_url="https://api.example.invalid",
               api_key_env="WIRE_LOG_TEST_KEY", model_id="test-model")
SECRET = "sk-wire-log-test-secret"


def _spec(wire_log: bool = False) -> LLMSpec:
    return LLMSpec(base_url="https://api.example.invalid",
                   api_key_env="WIRE_LOG_TEST_KEY", model_id="test-model",
                   wire_log=wire_log)


def _reply(mark: str = "") -> dict:
    return {"content": [{"type": "text", "text": "ok" + mark}],
            "model": "test-model", "usage": {"input_tokens": 11,
                                             "output_tokens": 7}}


def _client(monkeypatch, handler, **client_kwargs) -> MiniMaxMessagesClient:
    monkeypatch.setenv("WIRE_LOG_TEST_KEY", SECRET)
    transport = httpx.MockTransport(handler)
    client = MiniMaxMessagesClient(_spec(), **client_kwargs)
    client._http = httpx.AsyncClient(transport=transport)  # noqa: SLF001
    return client


async def _create(client: MiniMaxMessagesClient, system: str = "sys"):
    return await client.create(system=system, messages=[
        {"role": "user", "content": "hi"}], tools=[])


async def test_attempt_records_retry_then_success(monkeypatch, tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="try again")
        return httpx.Response(200, json=_reply())

    records: list[dict] = []
    posts = []
    client = _client(monkeypatch, handler, on_attempt=records.append,
                     on_post=lambda: posts.append(None))
    reply = await _create(client, system="s")
    assert reply.input_tokens == 11
    assert [r["status_code"] for r in records] == [503, 200]
    assert records[0]["error"] == "HTTP 503 (retryable)"
    assert records[0]["response"] is None
    assert records[1]["response"]["usage"]["input_tokens"] == 11
    assert records[1]["input_tokens"] == 11 and records[1]["output_tokens"] == 7
    assert records[1]["latency_ms"] >= 0
    assert records[1]["payload_hash"]
    assert "request" in records[1]
    # on_post parity: exactly one per counted attempt
    assert len(posts) == 2 == client.posts_sent


async def test_terminal_4xx_record_carries_redacted_error(monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        # a reflecting proxy echoing the credential back
        return httpx.Response(400, text=f"bad key {SECRET}")

    records: list[dict] = []
    client = _client(monkeypatch, handler, on_attempt=records.append)
    with pytest.raises(ModelUnavailable):
        await _create(client)
    assert len(records) == 1
    assert records[0]["status_code"] == 400
    assert SECRET not in json.dumps(records[0])
    assert "<redacted>" in records[0]["error"]


async def test_wire_log_round_trips_marker_verbatim(monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_reply())

    client = _client(monkeypatch, handler)
    wire = WireLog(tmp_path, "test-agent", 0, _spec(wire_log=True))
    client.on_attempt = wire.note
    await _create(client, system=f"system prompt with {MARKER}")
    path = tmp_path / "llm-wire" / "test-agent.jsonl"
    line = path.read_text()
    assert MARKER in line  # verbatim round trip, marker not split/escaped
    assert SECRET not in line
    # 0600 on the transcript
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_cost_ledger_compact_lines(monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_reply())

    client = _client(monkeypatch, handler)
    costs = CostLedger(tmp_path)
    client.on_attempt = lambda r: costs.note("test-agent", 0, r)
    await _create(client)
    await _create(client)
    lines = (tmp_path / "llm_costs.jsonl").read_text().splitlines()
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert row["agent_id"] == "test-agent" and row["player_id"] == 0
    assert row["input_tokens"] == 11 and row["output_tokens"] == 7
    assert row["status_code"] == 200 and row["latency_ms"] >= 0
    assert "request" not in row and "response" not in row  # no payloads
    # CAR-003 seam: the identity keys are always present; unsupplied == null
    assert row["turn"] is None and row["match_id"] is None and row["run_id"] is None


def test_cost_ledger_rows_carry_join_identity_only_when_supplied(tmp_path):
    """CAR-003 §8a: turn/match_id/run_id ride on the row when the composer
    supplies them; an absent, empty, or ill-typed value stays null (a
    runtime's ``_turn == 0`` is "no turn begun", not turn zero; a bool is
    not a turn) — nothing is defaulted."""
    costs = CostLedger(tmp_path)
    record = {"ts": "t", "request_kind": "generation", "attempt": 0,
              "status_code": 200, "latency_ms": 1, "model": "m",
              "payload_hash": "h", "decision_id": "d1",
              "logical_request_id": "lr1", "request_set_key": "h:generation"}
    costs.note("a", 0, record, turn=7, match_id="match-x", run_id="run-x")
    costs.note("a", 0, record)
    costs.note("a", 0, record, turn=0, match_id="", run_id=None)
    costs.note("a", 0, record, turn=True, match_id=5, run_id=b"x")
    rows = [json.loads(line) for line in
            (tmp_path / "llm_costs.jsonl").read_text().splitlines()]
    assert [(r["turn"], r["match_id"], r["run_id"]) for r in rows] == [
        (7, "match-x", "run-x"), (None, None, None), (None, None, None),
        (None, None, None)]
    # the CAP-03 identity and the cost fields are untouched by the seam
    assert all(r["decision_id"] == "d1" and r["logical_request_id"] == "lr1"
               and r["status_code"] == 200 for r in rows)


def test_wire_client_sinks_thread_join_identity_per_attempt(monkeypatch, tmp_path):
    """CAR-003 §8a: the composer passes match_id/run_id and reads
    ``turn_of()`` at ATTEMPT time (the turn advances between attempts);
    a raising ``turn_of`` yields null and the row still lands, with no
    ledger_write_failed audit — identity is best-effort, cost is not."""
    from civ_arena.config import AgentSpec
    from civ_arena.game.civ6.live_driver import _wire_client_sinks

    monkeypatch.setenv("WIRE_LOG_TEST_KEY", SECRET)
    agent = AgentSpec(agent_id="seat", player_id=1, policy="llm", seed=1,
                      llm=_spec())
    audits = []

    class _Runtime:
        _turn = 0

    runtime = _Runtime()
    calls = {"n": 0}

    def turn_of():
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("turn source broke")
        return runtime._turn

    class _FakeClient:
        def __init__(self):
            self.posts_sent = 0
            self.on_post = None
            self.on_attempt = None

    client = _FakeClient()
    _wire_client_sinks(client, agent, lambda tag, **f: audits.append(
        {"audit": tag, **f}), tmp_path, match_id="m-1", run_id="run-1",
        turn_of=turn_of)
    record = {"ts": "t", "request_kind": "generation", "attempt": 0,
              "status_code": 200, "latency_ms": 5, "model": "m",
              "payload_hash": "h", "request": {}, "response": None,
              "error": None, "input_tokens": 2, "output_tokens": 1,
              "decision_id": "d", "logical_request_id": "l",
              "request_set_key": "h:generation"}
    client.on_attempt(record)          # before any turn: _turn == 0 -> null
    runtime._turn = 4
    client.on_attempt(record)          # turn 4
    client.on_attempt(record)          # turn_of raises -> null, row still lands
    rows = [json.loads(line) for line in
            (tmp_path / "llm_costs.jsonl").read_text().splitlines()]
    assert [r["turn"] for r in rows] == [None, 4, None]
    assert all(r["match_id"] == "m-1" and r["run_id"] == "run-1"
               and r["agent_id"] == "seat" and r["player_id"] == 1
               for r in rows)
    assert not any(a["audit"] == "ledger_write_failed" for a in audits)


def test_wire_client_sinks_compose(monkeypatch, tmp_path):
    from civ_arena.config import AgentSpec
    from civ_arena.game.civ6.live_driver import _wire_client_sinks

    monkeypatch.setenv("WIRE_LOG_TEST_KEY", SECRET)
    agent = AgentSpec(
        agent_id="wired-agent", player_id=2, policy="llm", seed=1,
        llm=_spec(wire_log=True))
    audits = []

    def _audit(tag, **fields):
        audits.append({"audit": tag, **fields})

    class _FakeClient:
        def __init__(self):
            self.posts_sent = 0
            self.on_post = None
            self.on_attempt = None

    client = _FakeClient()
    _wire_client_sinks(client, agent, _audit, tmp_path)
    client.posts_sent = 3
    client.on_post()
    client.on_attempt({"ts": "t", "request_kind": "generation", "attempt": 0,
                       "status_code": 200, "latency_ms": 5, "model": "m",
                       "payload_hash": "h", "request": {"a": 1},
                       "response": {"usage": {"input_tokens": 2}},
                       "error": None, "input_tokens": 2, "output_tokens": 1})
    spend = [json.loads(line) for line in
             (tmp_path / "spend.jsonl").read_text().splitlines()]
    assert spend == [{"agent_id": "wired-agent", "player_id": 2}]
    costs = [json.loads(line) for line in
             (tmp_path / "llm_costs.jsonl").read_text().splitlines()]
    assert len(costs) == 1 and costs[0]["agent_id"] == "wired-agent"
    wire = (tmp_path / "llm-wire" / "wired-agent.jsonl").read_text()
    assert '"a": 1' in wire
    assert any(a.get("agent") == "wired-agent" and a.get("posts_sent") == 3
               for a in audits)


def test_wire_client_sinks_skips_non_clients(tmp_path):
    from civ_arena.config import AgentSpec
    from civ_arena.game.civ6.live_driver import _wire_client_sinks
    agent = AgentSpec(agent_id="t", player_id=0, policy="turtler", seed=1)
    _wire_client_sinks(None, agent, lambda **p: None, tmp_path)  # no error


def test_single_seat_dispatch_sink_writes_cost_and_spend_without_typeerror(
        monkeypatch, tmp_path):
    """F-04 regression: the single-seat dispatch call site passes the
    production (event, **payload) audit closure into _wire_client_sinks.
    The pre-fix lambda (_strategic_audit's one-dict signature) raised
    TypeError on the FIRST provider POST; nothing else exercised this
    path, so the full gate stayed green with the bug live."""

    from civ_arena.config import AgentSpec, parse_config
    from civ_arena.game.civ6 import live_driver as ld
    from civ_arena.game.civ6.firetuner import FireTunerAdapter

    monkeypatch.setenv("WIRE_LOG_TEST_KEY", SECRET)
    spec = parse_config({
        "match": {"match_id": "f04", "seed": 1, "adapter": "firetuner",
                  "watchdog_mode": "flag_and_continue"},
        "agents": [{"agent_id": "a0", "player_id": 0, "policy": "turtler"}],
    })
    driver = ld.LiveDriver(spec, FireTunerAdapter("127.0.0.1", 1),
                           tmp_path, "f04-i1")
    agent = AgentSpec(agent_id="a0", player_id=0, policy="llm", seed=1,
                      llm=_spec(wire_log=True))

    class _FakeClient:
        def __init__(self):
            self.posts_sent = 0
            self.on_post = None
            self.on_attempt = None

    client = _FakeClient()
    # THE production callable from the single-seat call site
    ld._wire_client_sinks(client, agent, ld._provider_request_audit(driver),
                          tmp_path)
    client.posts_sent = 1
    client.on_post()  # pre-fix: TypeError here
    client.on_attempt({"ts": "t", "request_kind": "generation", "attempt": 0,
                       "status_code": 200, "latency_ms": 4, "model": "m",
                       "payload_hash": "h", "request": {}, "response": None,
                       "error": None, "input_tokens": 1, "output_tokens": 1})
    spend = [json.loads(line) for line in
             (tmp_path / "spend.jsonl").read_text().splitlines()]
    assert spend == [{"agent_id": "a0", "player_id": 0}]
    assert (tmp_path / "llm_costs.jsonl").exists()
    events = [json.loads(line) for line in
              (tmp_path / "events.jsonl").read_text().splitlines()]
    provider = [e for e in events if e.get("audit") == "provider_request"]
    assert len(provider) == 1 and provider[0]["posts_sent"] == 1


async def test_wire_log_absent_when_not_opted_in(monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_reply())

    client = _client(monkeypatch, handler)
    costs = CostLedger(tmp_path)
    client.on_attempt = lambda r: costs.note("a", 0, r)
    await _create(client)
    assert (tmp_path / "llm_costs.jsonl").exists()
    assert not (tmp_path / "llm-wire").exists()


async def test_rotated_key_echoed_in_response_never_reaches_any_sink(
        monkeypatch, tmp_path):
    """CAP-R1 #13: the env key rotates WHILE a request is in flight and the
    response echoes the OLD credential. The attempt record is swept at
    FIRE time with the key actually sent, so the old value never reaches
    any sink (wire transcript, cost ledger) — and everything else (the
    marker) stays verbatim."""
    old_secret, new_secret = "sk-old-rotate-me", "sk-new-rotate-me"
    monkeypatch.setenv("WIRE_LOG_TEST_KEY", old_secret)

    def handler_echo(request: httpx.Request) -> httpx.Response:
        # the rotation lands mid-flight, before the response arrives
        monkeypatch.setenv("WIRE_LOG_TEST_KEY", new_secret)
        doc = _reply()
        doc["content"][0]["text"] = f"echo {old_secret}"  # provider echo
        return httpx.Response(200, json=doc)

    client = MiniMaxMessagesClient(_spec(wire_log=True))
    client._http = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler_echo))
    wire = WireLog(tmp_path, "rotate-agent", 0, _spec(wire_log=True))
    client.on_attempt = wire.note
    await _create(client, system=f"system prompt with {MARKER}")
    line = (tmp_path / "llm-wire" / "rotate-agent.jsonl").read_text()
    assert MARKER in line            # everything else verbatim
    assert old_secret not in line    # the OLD key never lands on disk
    assert new_secret not in line
