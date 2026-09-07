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
    cfg.update(overrides)
    return FakeMod(spectate=cfg, ambient_diffs=True)


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
    assert rows == [{"kind": "unit.moved", "entity_type": "unit",
                     "entity_id": "u1:3", "attr": "pos",
                     "before": "30,30", "after": "31,30"}]


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
        assert doc["digest"]["consistent"] is True
        assert doc["overview"]  # counts still carried

    await with_fake(_spectate_mod(), check)


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
