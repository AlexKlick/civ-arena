"""Wire layer vs FakeTunerServer speaking the real Firaxis Nexus framing."""

from __future__ import annotations

import pytest

from civ_arena.game.adapter import ObserveKind, ObserveRequest
from civ_arena.game.civ6.fake_tuner_server import FakeMod, FakeTunerServer
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from civ_arena.game.civ6.response_parser import parse_kv_lines
from civ_arena.game.civ6.vendor import tuner_client
from civ_arena.game.civ6.vendor.connection import GameConnection, LuaError

TURN_LINES = ["TURN|7", "LOCAL|0", "PUPPET_ACTIVE|false"]

# setup() seeds its mirror + digest cache: canned answers for those polls
STATUS_DIGEST_CANNED = [
    (0, "Puppeteer.Status",
     ["TURN|7", "PUPPET_ACTIVE|false", "LEASE_PLAYER|-1", "LEASE_TURN|-1"]),
    (0, "Puppeteer.Digest", ["DIGEST|canned-board"]),
]


async def with_server(responses, fn, mod=None):
    server = FakeTunerServer(responses, mod=mod)
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


async def test_parser_visible_map_mapping_and_failclosed():
    from civ_arena.game.civ6.response_parser import parse_visible_map

    # engine names -> sim vocabulary (the mover's cost table); the
    # unknown-name fallback is counted, not dropped
    parsed = parse_visible_map([
        "VMAP|2", "TURN|9",
        "TILEROW|1|0|GRASS_HILLS|true|0|c1",
        "TILEROW|2|0|TUNDRA|false|-1|",
        "TILEROW|3|0|WEIRD_MARS|false|-1|",
        "---END---"])
    assert parsed["turn"] == 9
    assert parsed["tiles"]["1,0"] == {"terrain": "HILL", "owner": 0,
                                      "city": "c1"}
    assert parsed["tiles"]["2,0"] == {"terrain": "PLAINS"}
    assert parsed["tiles"]["3,0"] == {"terrain": "PLAINS"}
    assert parsed["unknown_terrain"] == 1
    assert parsed["visible"] == frozenset({"1,0"})
    # fog rows never materialize ownership even when the wire sends one
    parsed = parse_visible_map([
        "VMAP|2", "TURN|2",
        "TILEROW|0|0|DESERT|false|3|c999", "---END---"])
    assert parsed["tiles"]["0,0"] == {"terrain": "DESERT"}
    # fail-closed shapes
    for bad in (["TILEROW|1|0|GRASS|maybe|-1|"],           # non-boolean flag
                ["TILEROW|1|0|GRASS|true|-1"],             # 5 fields
                ["BADEROW|x"],                             # foreign row
                ["TILEROW|1|0|GRASS|true|-1|",             # duplicate
                 "TILEROW|1|0|GRASS|true|-1|"]):
        with pytest.raises(ValueError):
            parse_visible_map(["VMAP|2"] + bad)
    with pytest.raises(ValueError):
        parse_visible_map(["TILEROW|1|0|GRASS|true|-1|"])  # no TURN row


async def test_visible_map_feeds_visibility_cache():
    """observe(VISIBLE_MAP) derives visibility from own entities and is
    the authority visibility_for() reads: empty before the first read
    (fail-safe under-visibility); remembered tiles ACCUMULATE adapter-side
    (the M11 no-expiry epistemics) — moving an own unit away from its old
    ring turns that ring into remembered fog. Consistency invariant for
    the projection: every observable key's tile carries its owner —
    visibility.py indexes tile["owner"] unconditionally for sees."""
    async def check(port: int, server: FakeTunerServer):
        adapter = FireTunerAdapter("127.0.0.1", port)
        await adapter.setup({})
        assert adapter.visibility_for(0) == (frozenset(), frozenset())
        doc = await adapter.observe(
            ObserveRequest(kind=ObserveKind.VISIBLE_MAP, player_id=0))
        # canonical-JSON material only in the returned doc
        assert set(doc) == {"turn", "tiles"}
        assert len(doc["tiles"]) > 5
        observable, remembered = adapter.visibility_for(0)
        assert observable | remembered == frozenset(doc["tiles"])
        assert observable, "own-entity rings produce current sight"
        assert remembered == frozenset(), "nothing forgotten yet"
        for key in observable:
            tile = doc["tiles"][key]
            assert "owner" in tile and "city" in tile

        # march a warrior off its ring: the old sight becomes remembered
        server.mod.units[2]["x"] += 4  # type: ignore[index]
        doc2 = await adapter.observe(
            ObserveRequest(kind=ObserveKind.VISIBLE_MAP, player_id=0))
        obs2, rem2 = adapter.visibility_for(0)
        assert rem2, "the abandoned ring stays in memory"
        assert obs2 & rem2 == frozenset()
        assert obs2 | rem2 == frozenset(doc2["tiles"])
        for key in rem2:
            assert set(doc2["tiles"][key]) == {"terrain"}
        # the second player has NOT observed: still the safe empty sets
        assert adapter.visibility_for(1) == (frozenset(), frozenset())
        await adapter.teardown()
        return None

    await with_server([], check, mod=FakeMod())


async def test_firetuner_adapter_poll_and_overview():
    async def check(port: int, _server: FakeTunerServer):
        adapter = FireTunerAdapter("127.0.0.1", port)
        await adapter.setup({})
        caps = adapter.capabilities()
        assert caps.acts and caps.rollback is False and caps.turn_events
        assert caps.state_hash, "M14b: the digest hash is live"

        # async by seam contract; body is the local mirror (no wire traffic)
        phase = await adapter.current_phase()
        assert phase["turn"] == 7
        assert phase["phase_player"] == -1

        # M14d: OVERVIEW is the sim shape (turn + players dict) — the
        # projection consumes exactly this
        overview = await adapter.observe(
            ObserveRequest(kind=ObserveKind.OVERVIEW, player_id=0))
        assert overview["turn"] == 7
        assert overview["players"]["0"]["civ_name"] == "CIVILIZATION_ROME"
        assert overview["players"]["1"]["gold"] == 100
        await adapter.teardown()

    responses = STATUS_DIGEST_CANNED + [
        (0, "TS|", TURN_LINES),
        (0, "OVX|", ["OVX|1", "TURN|7",
                     "OVROW|0|CIVILIZATION_ROME|120|-",
                     "OVROW|1|CIVILIZATION_KOREA|100|-"]),
    ]
    await with_server(responses, check)


async def test_adapter_gaps_point_at_live_validation_doc():
    """The NOT-YET-implemented surface stays pinned to the doc; landed
    surface (phases, digest hash, mutation journal, observes, acts) must
    NOT raise."""
    async def check(port: int, _server: FakeTunerServer):
        adapter = FireTunerAdapter("127.0.0.1", port)
        await adapter.setup({})
        for call in (
            adapter.snapshot,
            adapter.export_state,
            lambda: adapter.restore(None),
            lambda: adapter.import_state({}),
        ):
            with pytest.raises(NotImplementedError, match="live-validation"):
                call()
        # M14d declaration: visibility_for returns EMPTY sets (the safe
        # side of no-leak) instead of raising — pinned so the declaration
        # cannot silently become an error path again
        observable, remembered = adapter.visibility_for(0)
        assert observable == frozenset() and remembered == frozenset()
        await adapter.teardown()

    await with_server(STATUS_DIGEST_CANNED, check)
