"""Lost-read retries for the verify RPC — hundred-20260920's death.

The 100-round attempt died at turn 1: activate + reflag receipts landed
(~330ms band) but verify's read returned [] at 2304ms — the same
empty-response jitter band firetuner.act() already retries. Verify is the
one PURE OBSERVATION (asserts + fresh-token receipt), so a lost read
retries with a fresh token; mutating operations stay single-shot
(the ambiguity doctrine), and a non-empty WRONG receipt fails everywhere.
"""

from __future__ import annotations

import asyncio

import pytest

from civ_arena.game.civ6 import human_handoff as hh
from civ_arena.game.civ6.fake_tuner_server import FakeMod


class Connection:
    """Per-call scripted results; healthy calls go to the FakeMod."""

    def __init__(self, script: list | None = None) -> None:
        self._reader, self._writer = object(), object()
        self._lock = asyncio.Lock()
        self.is_connected = True
        self.lua_states = {2: "GameCore_Tuner", 94: "InGame"}
        self.mod = FakeMod(hotseat=[0, 1])
        self.mod.puppets = {0: True, 1: True}
        self.mod.lease = {"player": 0, "turn": 1}
        self.mod.turn_active = True
        self.script = script or []
        self.calls = 0

    async def _locked_execute(self, state, code, timeout):
        self.calls += 1
        if self.calls <= len(self.script):
            result = self.script[self.calls - 1]
            if isinstance(result, BaseException):
                raise result
            return result
        return self.mod.respond(code)


async def test_verify_lost_read_retries_with_fresh_token() -> None:
    conn = Connection(script=[[]])  # first read lost, second healthy
    result = await hh._rpc(conn, "verify", 0, 1, (0, 1))
    assert result["status"] == "observed"
    assert conn.calls == 2


async def test_verify_all_reads_lost_aborts_bounded() -> None:
    conn = Connection(script=[[], [], []])
    with pytest.raises(RuntimeError, match="receipt missing or mismatched"):
        await hh._rpc(conn, "verify", 0, 1, (0, 1))
    assert conn.calls == 3  # bounded, not unbounded


async def test_mutating_operation_never_replays_a_lost_read() -> None:
    conn = Connection(script=[[]])
    with pytest.raises(RuntimeError, match="receipt missing or mismatched"):
        await hh._rpc(conn, "activate", 0, 1, (0, 1))
    assert conn.calls == 1  # the ambiguity doctrine, pinned


async def test_wrong_receipt_fails_immediately_even_for_verify() -> None:
    wrong = ["HUMAN_HANDOFF|deadbeefdeadbeefdeadbeefdeadbeef|verify|0|1|observed"]
    conn = Connection(script=[wrong])
    with pytest.raises(RuntimeError, match="receipt missing or mismatched"):
        await hh._rpc(conn, "verify", 0, 1, (0, 1))
    assert conn.calls == 1  # a real mismatch is never retried


async def test_healthy_verify_is_single_dispatch() -> None:
    conn = Connection()
    result = await hh._rpc(conn, "verify", 0, 1, (0, 1))
    assert result["status"] == "observed"
    assert conn.calls == 1
