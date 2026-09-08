"""Capture primitives over the real adapter + FakeTunerServer socket path.

TurnWatch is pure-Python (ring semantics); SpectatorCensus is exercised
through FireTunerAdapter against FakeMod's spectate timeline so the
read_raw/observe/digest paths rehearse the live wire shapes.
"""

from civ_arena.config import parse_config
from civ_arena.game.civ6.fake_tuner_server import FakeMod, FakeTunerServer
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from civ_arena.game.civ6.spectate_capture import (
    SpectatorCensus,
    TurnWatch,
    parse_ambient_rows,
)

SPECTATE_DOC = {
    "match": {"match_id": "spectate-capture-test", "seed": 1,
              "adapter": "firetuner"},
    "spectate": {"operator": "alexk"},
    "agents": [],
}


def _spectate_mod(**overrides) -> FakeMod:
    cfg = {"human_seat": 0, "ai_seats": [1], "polls_per_human_turn": 50}
    kwargs = {}
    if "fail_spectator" in overrides:
        kwargs["fail_spectator"] = overrides.pop("fail_spectator")
    cfg.update(overrides)
    return FakeMod(spectate=cfg, ambient_diffs=True, **kwargs)


# -- TurnWatch ----------------------------------------------------------------

def test_turnwatch_incremental_entries_and_dedupe() -> None:
    watch = TurnWatch()
    new, gap = watch.new_entries(["1|HOOK_ENTER|0"])
    assert (new, gap) == (["1|HOOK_ENTER|0"], False)
    new, gap = watch.new_entries(["1|HOOK_ENTER|0"])
    assert (new, gap) == ([], False)
    new, gap = watch.new_entries(["1|HOOK_ENTER|0", "1|HOOK_DEACT|0",
                                  "1|HOOK_ENTER|1"])
    assert new == ["1|HOOK_DEACT|0", "1|HOOK_ENTER|1"]
    assert gap is False


def test_turnwatch_survives_ring_wrap_with_gap_flag() -> None:
    watch = TurnWatch()
    watch.new_entries(["1|HOOK_ENTER|0", "1|HOOK_DEACT|0", "1|HOOK_ENTER|1"])
    # the 64-entry ring wraps: the two oldest entries dropped from the front
    new, gap = watch.new_entries(["1|HOOK_DEACT|1", "2|HOOK_ENTER|0"])
    assert gap is True
    assert watch.gaps == 1
    assert "2|HOOK_ENTER|0" in new


def test_turnwatch_parse_shapes() -> None:
    assert TurnWatch.parse("12|HOOK_DEACT|0") == (12, "HOOK_DEACT", 0)
    assert TurnWatch.parse("noise") is None
    assert TurnWatch.parse("a|HOOK_DEACT|0") is None


def test_parse_ambient_rows() -> None:
    rows = parse_ambient_rows([
        "AMBIENT|unit.moved|unit|u1:3|pos|30,30|31,30",
        "garbage line",
        "---END---",
    ])
    # CAP-01: rows additionally carry owner/evidence attribution fields
    assert rows == [{"kind": "unit.moved", "entity_type": "unit",
                     "entity_id": "u1:3", "attr": "pos",
                     "before": "30,30", "after": "31,30",
                     "entity_owner_at_observation": 1,
                     "actor_id": None,
                     "evidence_kind": "state_interval_diff"}]


# -- SpectatorCensus over the socket ------------------------------------------

async def with_fake(mod: FakeMod, fn):
    server = FakeTunerServer(mod=mod)
    port = await server.start()
    adapter = FireTunerAdapter("127.0.0.1", port)
    try:
        await adapter.setup({})
        return await fn(adapter, server, mod)
    finally:
        await adapter.teardown()
        await server.stop()


async def test_census_snapshot_consistent_and_shaped() -> None:
    async def check(adapter, server, mod):
        await adapter.poll_status()  # attach (human turn 1 active)
        spec = parse_config(SPECTATE_DOC).spectate
        census = SpectatorCensus(adapter, spec)
        doc = await census.snapshot()
        assert doc["digest"]["consistent"] is True
        assert doc["digest"]["before"] == doc["digest"]["after"]
        assert doc["digest"]["before"]  # non-empty hash
        assert doc["overview"]["players"]["1"]["gold"] == 100
        ids = {u["unit_id"] for u in doc["units"]}
        assert "u1:3" in ids  # the AI's warrior is in the census
        assert all(set(u) <= {"unit_id", "owner", "type", "q", "r", "hp",
                              "movement", "fortified"} for u in doc["units"])
        assert doc["cities"][0]["name"] == "ARENA"
        assert doc["counts"]["units"]["1"] == 1
        assert doc["counts"]["cities"]["0"] == 1
        assert doc["census_ms"] >= 0
        assert "truncated" not in doc
        assert mod.turn == 1  # observing never advanced the game

    await with_fake(_spectate_mod(), check)


async def test_census_scope_ambient_drops_detail() -> None:
    async def check(adapter, server, mod):
        await adapter.poll_status()
        doc_cfg = dict(SPECTATE_DOC)
        doc_cfg["spectate"] = {"operator": "alexk", "snapshot_scope": "ambient"}
        spec = parse_config(doc_cfg).spectate
        census = SpectatorCensus(adapter, spec)
        doc = await census.snapshot()
        assert "units" not in doc and "cities" not in doc
        assert "world" not in doc, "the world block is full-scope only"
        assert doc["digest"]["consistent"] is True
        assert doc["overview"]  # counts still carried

    await with_fake(_spectate_mod(), check)


# -- M4: the additive world block (contract §4b) --------------------------------


async def test_census_world_block_read_transport_only_with_bracket() -> None:
    async def check(adapter, server, mod):
        await adapter.poll_status()  # attach (human turn 1 active)
        spec = parse_config(SPECTATE_DOC).spectate
        census = SpectatorCensus(adapter, spec)
        doc = await census.snapshot()
        world = doc["world"]
        assert world["schema"] == 1 and world["after_seat"] == -1
        # palette is EXCLUDED from the spectate carrier (write transport)
        assert world["contexts"]["palette"] == "absent"
        assert "palette" not in world
        assert {r["kind"] for r in world["roster"]} == {"major"}
        assert set(world["owned_tiles_columns"]) == {"0"}
        assert world["digest_consistent"] is True
        assert doc["digest"]["consistent"] is True
        assert "truncated" not in doc
        assert mod.turn == 1  # the world read never advanced the game

    await with_fake(_spectate_mod(), check)


async def test_census_world_failure_degrades_to_error_doc() -> None:
    async def check(adapter, server, mod):
        await adapter.poll_status()
        spec = parse_config(SPECTATE_DOC).spectate
        census = SpectatorCensus(adapter, spec)
        doc = await census.snapshot()
        assert doc["world"]["error"] == "LuaError"
        assert "digest_consistent" in doc["world"]
        assert doc["digest"]["consistent"] is True  # the census itself held

    await with_fake(_spectate_mod(fail_spectator=True), check)


def test_world_drops_first_under_the_snapshot_cap(monkeypatch) -> None:
    import civ_arena.game.civ6.spectate_capture as sc

    monkeypatch.setattr(sc, "MAX_SNAPSHOT_BYTES", 512)
    census = SpectatorCensus(object(), None)
    doc = {"digest": {"before": "a", "after": "a"}, "counts": {},
           "world": {"schema": 1, "roster": ["r" * 64] * 8},
           "units": ["u" * 64] * 4, "cities": [], "overview": {"o": 1}}
    capped = census._capped(doc)  # noqa: SLF001
    assert "world" not in capped
    assert capped["truncated"] == {"world": True}
    # a doc that fits is returned untouched
    small = {"digest": {"before": "a", "after": "a"}, "counts": {},
             "world": {"schema": 1}}
    assert census._capped(small) is small  # noqa: SLF001


def test_transport_allows_exactly_the_world_reads() -> None:
    from civ_arena.game.civ6 import world_capture
    from civ_arena.game.civ6.spectate_capture import (
        RecorderCapabilityError,
        SpectateTransport,
    )

    class _Inner:
        def __init__(self):
            self.sent: list[str] = []

        async def read_raw(self, lua: str) -> list[str]:
            self.sent.append(lua)
            return []

    import asyncio as _asyncio

    inner = _Inner()
    transport = SpectateTransport(inner)
    roster = world_capture.roster_read()
    tiles = world_capture.owned_tiles_read()

    async def attempts():
        # the exact full-command reads flow (EXACT match, not prefix)
        await transport.read_raw(roster)
        await transport.read_raw(tiles)
        # a one-character edit re-tightens the allowlist by construction
        for lua in (roster + " ", roster.replace("SPECW|1|roster", "SPECW|1|Roster"),
                    roster[:-1]):
            try:
                await transport.read_raw(lua)
                raise AssertionError("mutated world read was not rejected")
            except RecorderCapabilityError:
                pass

    _asyncio.run(attempts())
    assert inner.sent == [roster, tiles]
    assert transport.census["rejected"] == 3
    assert transport.census["recorder_commands"] == 2


async def test_census_drift_reports_inconsistent_after_retry() -> None:
    async def check(adapter, server, mod):
        await adapter.poll_status()
        spec = parse_config(SPECTATE_DOC).spectate
        census = SpectatorCensus(adapter, spec)
        doc = await census.snapshot()
        assert doc["digest"]["consistent"] is False
        assert census.retries == 1

    await with_fake(_spectate_mod(mutate_during_census=True), check)


async def test_human_window_captures_board_edits() -> None:
    async def check(adapter, server, mod):
        await adapter.poll_status()  # attach: human turn 1 active
        spec = parse_config(SPECTATE_DOC).spectate
        census = SpectatorCensus(adapter, spec)
        await census.open_window(0)
        mod.units[2]["x"], mod.units[2]["y"] = 7, 7  # the human "plays"
        rows = await census.close_window(0)
        moved = [r for r in rows if r["entity_id"] == "u0:2"]
        assert moved and moved[0]["before"] == "5,5" \
            and moved[0]["after"] == "7,7"

    await with_fake(_spectate_mod(), check)


async def test_ai_window_captures_engine_ai_turn() -> None:
    async def check(adapter, server, mod):
        await adapter.poll_status()  # attach
        spec = parse_config(SPECTATE_DOC).spectate
        census = SpectatorCensus(adapter, spec)
        await census.open_window(1)
        mod._spectate_ai_effect(1)  # the engine AI plays its turn
        rows = await census.close_window(1)
        moved = [r for r in rows if r["entity_id"] == "u1:3"]
        assert moved and moved[0]["attr"] == "pos"
        # gold income lands with the turn advance, not the AI window
        assert not any(r["kind"] == "player.gold" for r in rows)

    await with_fake(_spectate_mod(), check)


# -- CAP-01: interval semantics, actor/owner, generation, transport ----------

def test_ambient_rows_carry_owner_and_evidence_fields() -> None:
    rows = parse_ambient_rows([
        "AMBIENT|unit.moved|unit|u1:3|pos|30,30|31,30",
        "AMBIENT|player.gold|player|p1|gold|100|105",
    ])
    assert rows[0]["entity_owner_at_observation"] == 1
    assert rows[0]["actor_id"] is None  # owner != actor — never invented
    assert rows[0]["evidence_kind"] == "state_interval_diff"
    assert rows[1]["entity_owner_at_observation"] == 1
    assert rows[1]["evidence_kind"] == "state_interval_diff"


async def test_opponent_damage_to_owned_unit_is_not_owner_action() -> None:
    """A damage row inside the HUMAN's window is a net state difference —
    the mover may be an opponent attacking the human's unit. The row must
    not claim the human (the owner) acted."""
    async def check(adapter, server, mod):
        await adapter.poll_status()  # attach: human turn 1 active
        spec = parse_config(SPECTATE_DOC).spectate
        census = SpectatorCensus(adapter, spec)
        await census.open_window(0)
        # an opponent attacks the human's warrior during the human's turn:
        # the unit's damage changes while its OWNER never acted
        mod.units[2]["damage"] = 30
        rows = await census.close_window(0, actor_class="human_seat")
        damage = [r for r in rows if r["entity_id"] == "u0:2"
                  and r["attr"] == "damage"]
        assert damage, f"expected damage row, got {rows}"
        assert damage[0]["entity_owner_at_observation"] == 0
        assert damage[0]["actor_id"] is None  # owner attribution is NOT actor
        assert damage[0]["evidence_kind"] == "state_interval_diff"
        assert damage[0]["actor_class"] == "human_seat"  # window provenance

    await with_fake(_spectate_mod(), check)


async def test_net_diff_does_not_claim_complete_command_sequence() -> None:
    """Two distinct actions within one window (a move AND a moves-refresh)
    collapse into net rows without order or attempt semantics — the
    representation must say interval-diff, not a command sequence."""
    async def check(adapter, server, mod):
        await adapter.poll_status()
        spec = parse_config(SPECTATE_DOC).spectate
        census = SpectatorCensus(adapter, spec)
        await census.open_window(0)
        mod.units[2]["x"] = 6  # action 1
        mod.units[2]["x"] = 7  # action 2 — same attr, net diff only
        rows = await census.close_window(0)
        moved = [r for r in rows if r["entity_id"] == "u0:2"
                 and r["attr"] == "pos"]
        assert len(moved) == 1  # ONE net row for two actions
        assert moved[0]["evidence_kind"] == "state_interval_diff"
        assert "attempt" not in moved[0] and "order" not in moved[0]

    await with_fake(_spectate_mod(), check)


async def test_engine_advances_between_census_reads_is_marked_non_atomic() -> None:
    async def check(adapter, server, mod):
        await adapter.poll_status()
        spec = parse_config(SPECTATE_DOC).spectate
        census = SpectatorCensus(adapter, spec)
        doc = await census.snapshot()
        assert doc["digest"]["consistent"] is False
        assert doc["census_phase"] == "read_time"  # snapshot = read-time state
        assert doc["atomic"] is False  # explicit non-atomicity marker
        assert census.retries == 1

    await with_fake(_spectate_mod(mutate_during_census=True), check)


async def test_quiet_census_declares_read_time_phase() -> None:
    async def check(adapter, server, mod):
        await adapter.poll_status()
        spec = parse_config(SPECTATE_DOC).spectate
        census = SpectatorCensus(adapter, spec)
        doc = await census.snapshot()
        assert doc["census_phase"] == "read_time"
        assert doc["atomic"] is True

    await with_fake(_spectate_mod(), check)


def test_ring_wrap_and_source_reset_preserve_gap_and_generation() -> None:
    watch = TurnWatch()
    watch.new_entries(["1|HOOK_ENTER|0", "1|HOOK_DEACT|0"])
    # wrap: everything we had is gone, a later window arrives
    new, gap = watch.new_entries(["5|HOOK_ENTER|0"])
    assert gap is True and watch.gaps == 1
    assert watch.generation == 0  # turns moved FORWARD — same ring epoch
    # source reset (mod reload): the ring restarts at an EARLIER turn
    new, gap = watch.new_entries(["1|HOOK_ENTER|0"])
    assert gap is True
    assert watch.generation == 1  # a NEW ring epoch, counted separately


async def test_window_dump_duplicate_does_not_duplicate_an_action() -> None:
    async def check(adapter, server, mod):
        await adapter.poll_status()
        spec = parse_config(SPECTATE_DOC).spectate
        census = SpectatorCensus(adapter, spec)
        await census.open_window(0)
        mod.units[2]["x"] = 6
        first = await census.close_window(0)
        assert any(r["entity_id"] == "u0:2" for r in first)
        # re-dump without a new window: NOTHING (drained, not replayed)
        dumped = await adapter.read_raw(
            __import__("civ_arena.game.civ6.lua_translator",
                       fromlist=["lua_translator"]).dump_ambient())
        assert parse_ambient_rows(dumped) == []
        # end-without-begin produces no rows either (no fabricated diff)
        again = await census.close_window(0)
        assert again == []

    await with_fake(_spectate_mod(), check)


def test_spectator_transport_rejects_mutating_operation_before_send() -> None:
    from civ_arena.game.civ6.spectate_capture import (
        RecorderCapabilityError,
        SpectateTransport,
    )

    class _MutatingAdapter:
        def __init__(self):
            self.sent: list[str] = []

        async def read_raw(self, lua: str) -> list[str]:
            self.sent.append(lua)
            return []

        async def act(self, cmd):
            self.sent.append("ACT!")
            return None

        async def write_raw(self, lua: str):
            self.sent.append(lua)

    inner = _MutatingAdapter()
    transport = SpectateTransport(inner)
    import asyncio as _asyncio

    async def attempts():
        for bad in (
            lambda: transport.act(object()),
            lambda: transport.write_raw("UI.RequestAction(ActionTypes.ACTION_ENDTURN)"),
            lambda: transport.read_raw("Puppeteer.SetPuppet(0, true)"),
            lambda: transport.begin_phase(0, 1),
            lambda: transport.set_puppet(0, True),
        ):
            try:
                result = bad()
                if hasattr(result, "__await__"):
                    await result
                raise AssertionError("mutation was not rejected")
            except RecorderCapabilityError:
                pass
        # reads still flow
        rows = await transport.read_raw("Puppeteer.DumpAmbient()")
        assert rows == []

    _asyncio.run(attempts())
    assert inner.sent == ["Puppeteer.DumpAmbient()"]  # only the allowed read
    assert transport.census["rejected"] == 5
    assert transport.census["recorder_commands"] == 1


def test_transport_census_counts_lifecycle_separately() -> None:
    from civ_arena.game.civ6.spectate_capture import SpectateTransport

    class _LifecycleAdapter:
        def __init__(self):
            self.calls = []

        async def setup(self, cfg):
            self.calls.append("setup")
            return {}

        async def inject_mod(self, lua):
            self.calls.append("inject")
            return {"mod_version": "0.3.10"}

        async def teardown(self):
            self.calls.append("teardown")

    inner = _LifecycleAdapter()
    transport = SpectateTransport(inner)
    import asyncio as _asyncio

    async def run():
        await transport.setup({})
        await transport.inject_mod("-- mod")
        await transport.teardown()

    _asyncio.run(run())
    assert transport.census["recorder_lifecycle"] == 3
    assert transport.census["recorder_commands"] == 0  # lifecycle != commands

