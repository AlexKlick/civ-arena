"""Wire layer vs FakeTunerServer speaking the real Firaxis Nexus framing."""

from __future__ import annotations

import pytest

from civ_arena.game.adapter import ObserveKind, ObserveRequest
from civ_arena.game.civ6.fake_tuner_server import FakeTunerServer
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from civ_arena.game.civ6.response_parser import parse_kv_lines
from civ_arena.game.civ6.vendor import tuner_client
from civ_arena.game.civ6.vendor.connection import GameConnection, LuaError

TURN_LINES = ["TURN|7", "LOCAL|0", "PUPPET_ACTIVE|false"]


async def with_server(responses, fn):
    server = FakeTunerServer(responses)
    port = await server.start()
    try:
        return await fn(port, server)
    finally:
        await server.stop()


async def test_roundtrip_framing():
    async def check(port: int, server: FakeTunerServer):
        reader, writer = await tuner_client.connect("127.0.0.1", port)
        await tuner_client.send_message(writer, tuner_client.TAG_HANDSHAKE, "APP:")
        msg = await tuner_client.recv_message(reader)
        assert msg.tag == tuner_client.TAG_HANDSHAKE
        assert msg.payload == "FakeSidMeiersCivilizationVI", (
            "framing round-trip must return the identity intact"
        )
        writer.close()

    await with_server([], check)


async def test_handshake_discovers_states():
    async def check(port: int, server: FakeTunerServer):
        conn = GameConnection("127.0.0.1", port)
        await conn.connect()
        assert conn.gamecore_index == 0
        assert conn.ingame_index == 1
        assert conn.lua_states == {0: "GameCore_Tuner", 1: "InGame"}
        await conn.disconnect()

    await with_server([], check)


async def test_sentinel_collection():
    async def check(port: int, server: FakeTunerServer):
        conn = GameConnection("127.0.0.1", port)
        await conn.connect()
        lines = await conn.execute_read('print("TS|1") print("---END---")')
        assert lines == TURN_LINES
        assert server.received_commands  # the CMD:{state}:{code} payload arrived
        payload = server.received_commands[0]
        assert payload.startswith("CMD:0:")
        await conn.disconnect()

    await with_server([(0, "TS|", TURN_LINES)], check)


async def test_lua_error_raises():
    async def check(port: int, _server: FakeTunerServer):
        conn = GameConnection("127.0.0.1", port)
        await conn.connect()
        with pytest.raises(LuaError, match="ERR:"):
            await conn.execute_read("bad code")
        await conn.disconnect()

    await with_server([(0, "bad code", ["ERR:lua exploded"])], check)


async def test_ingame_state_framing_parses():
    """InGame (state 1) responses arrive framed with their own context name;
    the vendored parser must strip any context, not just GameCore_Tuner."""

    async def check(port: int, _server: FakeTunerServer):
        conn = GameConnection("127.0.0.1", port)
        await conn.connect()
        lines = await conn.execute_write('print("UI|1") print("---END---")')
        assert lines == ["UI|1"]
        await conn.disconnect()

    await with_server([(1, "UI|", ["UI|1"])], check)


async def test_parser_kv_and_rows():
    parsed = parse_kv_lines(
        ["TURN|12", "LOCAL|0", "PUPPET_ACTIVE|false", "---END---",
         "PLAYER|0|CIVILIZATION_ROME", "PLAYER|1|CIVILIZATION_KOREA"]
    )
    assert parsed["TURN"] == 12
    assert parsed["LOCAL"] == 0
    assert parsed["PUPPET_ACTIVE"] is False
    assert parsed["PLAYER"] == [["0", "CIVILIZATION_ROME"],
                                ["1", "CIVILIZATION_KOREA"]]


async def test_firetuner_adapter_poll_and_overview():
    async def check(port: int, _server: FakeTunerServer):
        adapter = FireTunerAdapter("127.0.0.1", port)
        await adapter.setup({})
        caps = adapter.capabilities()
        assert caps.acts is False and caps.rollback is False and caps.turn_events

        phase = await adapter.current_phase()
        assert phase["turn"] == 7
        assert phase["raw"]["PUPPET_ACTIVE"] is False

        overview = await adapter.observe(
            ObserveRequest(kind=ObserveKind.OVERVIEW, player_id=0))
        assert overview["TURN"] == 7
        assert overview["ALIVE"] == 2
        await adapter.teardown()

    responses = [
        (0, "TS|", TURN_LINES),
        (0, "OV|", ["OV|1", "TURN|7", "ALIVE|2",
                   "PLAYER|0|CIVILIZATION_ROME", "PLAYER|1|CIVILIZATION_KOREA"]),
    ]
    await with_server(responses, check)


async def test_adapter_gaps_point_at_live_validation_doc():
    async def check(port: int, _server: FakeTunerServer):
        adapter = FireTunerAdapter("127.0.0.1", port)
        await adapter.setup({})
        for call in (
            adapter.snapshot,
            adapter.state_hash,
            adapter.drain_mutations,
            adapter.export_state,
            lambda: adapter.visibility_for(0),
            lambda: adapter.import_state({}),
        ):
            with pytest.raises(NotImplementedError, match="live-validation"):
                call()
        for async_call in (
            lambda: adapter.act(None),
            lambda: adapter.begin_phase(0, 1),
            lambda: adapter.end_phase(0, 1),
            lambda: adapter.observe(ObserveRequest(
                kind=ObserveKind.UNITS, player_id=0)),
        ):
            with pytest.raises(NotImplementedError, match="live-validation"):
                await async_call()
        await adapter.teardown()

    await with_server([], check)
