"""M14a live-preflight: mod handshake gate + staged smoke rehearsal."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from civ_arena.game.civ6 import lua_translator
from civ_arena.game.civ6.fake_tuner_server import FakeMod, FakeTunerServer
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from civ_arena.game.civ6.response_parser import (
    parse_handshake,
    parse_kv_lines,
)

REPO = Path(__file__).resolve().parents[1]

# canned answers for setup()'s mirror + digest seeding when no FakeMod rides
STATUS_DIGEST = [
    (0, "Puppeteer.Status",
     ["TURN|1", "PUPPET_ACTIVE|false", "LEASE_PLAYER|-1", "LEASE_TURN|-1"]),
    (0, "Puppeteer.Digest", ["DIGEST|canned-board"]),
]


async def with_mod(mod: FakeMod | None, fn, canned=None):
    server = FakeTunerServer(responses=canned or [], mod=mod)
    port = await server.start()
    try:
        return await fn(FireTunerAdapter("127.0.0.1", port), server)
    finally:
        await server.stop()


async def test_mod_handshake_gate_passes():
    async def check(adapter, _server):
        await adapter.setup({})
        doc = await adapter.require_mod()
        assert doc["present"] is True
        assert doc["mod_version"] == "0.2.0-rehearsal"
        assert doc["supports_freeze"] and doc["supports_ledger"]
        await adapter.teardown()

    await with_mod(FakeMod(), check)


async def test_require_mod_refuses_when_caps_false():
    async def check(adapter, _server):
        await adapter.setup({})
        with pytest.raises(RuntimeError, match="gate failed"):
            await adapter.require_mod()
        await adapter.teardown()

    canned = STATUS_DIGEST + [(0, "Puppeteer.Handshake",
                               ["MOD_PRESENT|true", "MOD_VERSION|0.2.0-x",
                                "SUPPORTS_FREEZE|false",
                                "SUPPORTS_LEDGER|true",
                                "SUPPORTS_DIGEST|true"])]
    await with_mod(None, check, canned=canned)


async def test_require_mod_refuses_when_mod_absent():
    async def check(adapter, _server):
        await adapter.setup({})
        with pytest.raises(RuntimeError, match="gate failed"):
            await adapter.require_mod()
        await adapter.teardown()

    await with_mod(None, check,
                   canned=[(0, "Puppeteer", ["MOD_PRESENT|false"]),
                           (0, "TS|", ["TURN|1", "LOCAL|0",
                                       "PUPPET_ACTIVE|false"])])


async def test_require_mod_refuses_without_digest():
    """Codex P2-8: state_hash cannot operate without the digest — the gate
    requires freeze AND ledger AND digest."""

    async def check(adapter, _server):
        await adapter.setup({})
        with pytest.raises(RuntimeError, match="digest required"):
            await adapter.require_mod()
        await adapter.teardown()

    canned = STATUS_DIGEST + [(0, "Puppeteer.Handshake",
                               ["MOD_PRESENT|true", "MOD_VERSION|0.2.0-x",
                                "SUPPORTS_FREEZE|true",
                                "SUPPORTS_LEDGER|true",
                                "SUPPORTS_DIGEST|false"])]
    await with_mod(None, check, canned=canned)


def test_parse_kv_lines_splits_embedded_newlines():
    """Live-learned 2026-08-30: one print() of a multi-line string arrives
    as ONE payload — the parser must flatten before parsing."""
    parsed = parse_kv_lines(["TURN|1\nPUPPET_ACTIVE|true\nLEASE_PLAYER|0"])
    assert parsed == {"TURN": 1, "PUPPET_ACTIVE": True, "LEASE_PLAYER": 0}


def test_parse_handshake_fail_closed():
    # garbage lines never produce a True capability
    doc = parse_handshake(["random noise", "SUPPORTS_FREEZE|true"])
    assert doc == {"present": False, "mod_version": None,
                   "supports_freeze": False, "supports_ledger": False,
                   "supports_digest": False}
    # MOD_PRESENT true but a capability merely missing => still False
    doc = parse_handshake(["MOD_PRESENT|true", "MOD_VERSION|0.2.0",
                           "SUPPORTS_FREEZE|true"])
    assert doc["present"] is True
    assert doc["supports_freeze"] is True
    assert doc["supports_ledger"] is False  # absent, not defaulted


def test_translator_mod_entries_guarded_and_terminated():
    for builder, marker in (
        (lua_translator.mod_handshake, "MOD_PRESENT"),
        (lua_translator.mod_status, "MOD_STATUS"),
        (lua_translator.mod_digest, "MOD_DIGEST"),
    ):
        lua = builder()
        assert "Puppeteer == nil" in lua, f"{builder.__name__} nil-guard"
        assert marker in lua, f"{builder.__name__} absent-mod marker"
        assert "---END---" in lua, f"{builder.__name__} sentinel"


async def test_mod_status_flips_only_via_poll_after_turn_start():
    """D3 rehearsal: the turn-start hook print is unsolicited (drained);
    the lease surfaces only via the next Status poll."""

    async def check(adapter, _server):
        await adapter.setup({})
        await adapter.read_raw(
            "Puppeteer.SetPuppet(0, true)")
        parsed = parse_kv_lines(
            await adapter.read_raw(lua_translator.mod_status()))
        assert parsed["PUPPET_ACTIVE"] is False  # hook not fired yet

        await adapter.read_raw("Simulate.TurnStart(0)")  # returns no rows
        parsed = parse_kv_lines(
            await adapter.read_raw(lua_translator.mod_status()))
        assert parsed["PUPPET_ACTIVE"] is True
        assert parsed["LEASE_PLAYER"] == 0
        assert parsed["LEASE_TURN"] == 1

        await adapter.read_raw("Simulate.TurnDeactivated(0)")
        parsed = parse_kv_lines(
            await adapter.read_raw(lua_translator.mod_status()))
        assert parsed["PUPPET_ACTIVE"] is False
        await adapter.teardown()

    await with_mod(FakeMod(), check)


async def test_mod_digest_stable_until_state_changes():
    """Zero-drift rehearsal: digest is a pure function of state."""

    async def check(adapter, _server):
        await adapter.setup({})
        await adapter.read_raw("Puppeteer.SetPuppet(0, true)")
        d1 = await adapter.read_raw(lua_translator.mod_digest())
        d2 = await adapter.read_raw(lua_translator.mod_digest())
        assert d1 == d2  # idle => identical

        await adapter.read_raw("Simulate.Mutate()")
        d3 = await adapter.read_raw(lua_translator.mod_digest())
        assert d3 != d1  # engine drift => digest moves
        await adapter.teardown()

    await with_mod(FakeMod(), check)


async def test_ledger_and_ambient_rows_drain():
    async def check(adapter, _server):
        await adapter.setup({})
        await adapter.read_raw("Simulate.Ledger(move, unit, 7, movement, 2, 0)")
        await adapter.read_raw("Simulate.Ambient(tick, unit, 7, hp, 100, 98)")
        ledger = await adapter.read_raw("Puppeteer.DumpLedger()")
        ambient = await adapter.read_raw("Puppeteer.DumpAmbient()")
        assert ledger == ["LEDGER|1|0|u7|move|2|0"]
        assert ambient == ["AMBIENT|tick|unit|7|hp|100|98"]
        # drained: a second dump is empty
        assert await adapter.read_raw("Puppeteer.DumpLedger()") == []
        await adapter.teardown()

    await with_mod(FakeMod(), check)


async def test_status_skip_rehearsal_on_old_mod():
    async def check(adapter, _server):
        await adapter.setup({})
        lines = await adapter.read_raw(lua_translator.mod_status())
        assert lines == ["MOD_STATUS|unavailable"]
        await adapter.teardown()

    await with_mod(FakeMod(version="0.1.0-draft", has_status=False), check)


def test_smoke_fake_mode_all_stages():
    """The zero-dependency smoke run exercises all six stages."""
    proc = subprocess.run(
        [sys.executable, "scripts/firetuner_smoke.py", "--json"],
        cwd=REPO, capture_output=True, text=True, timeout=120.0)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["mode"] == "fake"
    assert [row["stage"] for row in doc["stages"]] == \
        ["S1", "S2", "S3", "S4", "S5", "S6"]
    assert all(row["status"] == "ok" for row in doc["stages"]), doc["stages"]
