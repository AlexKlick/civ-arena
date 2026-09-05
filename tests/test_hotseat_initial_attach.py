import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from civ_arena.game.civ6 import live_driver as ld

PARKED = dict(TURN=1, TURN_ACTIVE=True, PUPPET_ACTIVE=False,
              LEASE_PLAYER=-1, LEASE_TURN=-1)
ENGAGED = dict(TURN=1, TURN_ACTIVE=True, PUPPET_ACTIVE=True,
               LEASE_PLAYER=0, LEASE_TURN=1)


def adapter_fixture():
    conn = SimpleNamespace(
        _lock=asyncio.Lock(), gamecore_index=2, lua_states={2: "GameCore_Tuner"},
        is_connected=True,
        _locked_execute=AsyncMock(return_value=["ATTACH_CURRENT|accepted|0|1|ok"]),
    )
    return SimpleNamespace(_simulate=None, _conn=conn,
                           poll_status=AsyncMock(side_effect=[PARKED, ENGAGED]))


async def test_initial_attach_uses_current_turn_and_requires_observed_lease():
    adapter = adapter_fixture()
    events = []
    await ld._attach_initial_hotseat_turn(adapter, 0, lambda *a, **kw: events.append((a, kw)))
    adapter._conn._locked_execute.assert_awaited_once()
    args = adapter._conn._locked_execute.await_args.args
    assert "AttachCurrentTurn(0, 1)" in args[1]
    assert "RequestAction" not in args[1] and "Simulate" not in args[1]
    assert adapter.poll_status.await_count == 2
    assert [e[0][0] for e in events] == ["initial_turn_attach", "initial_turn_attach_observed"]
    ledger = ld.CompletedTurns([0, 1])
    ledger.append(dict(turn=1, player=0), SimpleNamespace(released=True))
    ledger.append(dict(turn=1, player=1), SimpleNamespace(released=True))
    assert ledger.rounds == 1


@pytest.mark.parametrize("receipt", [
    [], ["ATTACH_CURRENT|accepted|0|1|ok"] * 2,
    ["ATTACH_CURRENT|rejected|0|1|partially_used"],
    ["ATTACH_CURRENT|accepted|1|1|ok"],
    ["ATTACH_CURRENT|accepted|0|2|ok"],
    ["ATTACH_CURRENT|accepted|0|1"],
])
async def test_bad_initial_attach_receipt_never_retries(receipt):
    adapter = adapter_fixture()
    adapter._conn._locked_execute.return_value = receipt
    with pytest.raises(RuntimeError, match="attachment"):
        await ld._attach_initial_hotseat_turn(adapter, 0, lambda *a, **kw: None)
    adapter._conn._locked_execute.assert_awaited_once()


@pytest.mark.parametrize("after", [PARKED, {**ENGAGED, "LEASE_PLAYER": 1},
                                     {**ENGAGED, "LEASE_TURN": 2},
                                     {**ENGAGED, "TURN": 2}])
async def test_sent_attach_is_not_success_without_exact_engine_progress(after):
    adapter = adapter_fixture()
    adapter.poll_status.side_effect = [PARKED, after]
    with pytest.raises(RuntimeError, match="did not establish"):
        await ld._attach_initial_hotseat_turn(adapter, 0, lambda *a, **kw: None)
    adapter._conn._locked_execute.assert_awaited_once()


@pytest.mark.parametrize("error", [TimeoutError(), ConnectionError("lost response"),
                                    asyncio.CancelledError()])
async def test_initial_attach_transport_failure_and_cancellation_do_not_retry(error):
    adapter = adapter_fixture()
    adapter._conn._locked_execute.side_effect = error
    with pytest.raises(type(error)):
        await ld._attach_initial_hotseat_turn(adapter, 0, lambda *a, **kw: None)
    adapter._conn._locked_execute.assert_awaited_once()


async def test_fake_initial_attach_sends_no_input_or_wire():
    adapter = adapter_fixture()
    adapter._simulate = object()
    await ld._attach_initial_hotseat_turn(adapter, 0, lambda *a, **kw: None)
    adapter.poll_status.assert_not_awaited()
    adapter._conn._locked_execute.assert_not_awaited()


@pytest.mark.parametrize("before", [ENGAGED, {**PARKED, "TURN_ACTIVE": False}])
async def test_existing_lease_or_future_hook_does_not_attach_again(before):
    adapter = adapter_fixture()
    adapter.poll_status.side_effect = [before]
    await ld._attach_initial_hotseat_turn(adapter, 0, lambda *a, **kw: None)
    adapter._conn._locked_execute.assert_not_awaited()


@pytest.mark.parametrize("failure", ["later_turn", "connection", "context"])
async def test_initial_attach_refuses_invalid_state_before_mutation(failure):
    adapter = adapter_fixture()
    if failure == "later_turn":
        adapter.poll_status.side_effect = [{**PARKED, "TURN": 2}]
    elif failure == "connection":
        adapter._conn.is_connected = False
    else:
        adapter._conn.lua_states = {2: "OtherContext"}
    with pytest.raises(RuntimeError, match="initial hotseat attachment"):
        await ld._attach_initial_hotseat_turn(adapter, 0, lambda *a, **kw: None)
    adapter._conn._locked_execute.assert_not_awaited()
