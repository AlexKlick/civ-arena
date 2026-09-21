"""Read correlation — hundred-20260920b's desync death, pinned.

The lost UNITS read (empty at 2311ms) was delivered LATE into the CITIES
read; the stream desynced by one and the parser failed closed. Every
decorated read now carries ARENA_READ|<token> before its sentinel: late
foreign responses are skipped by their markers, lost reads retry with a
fresh token, and exhausted correlation reports [] (the "read lost"
signal) — never foreign rows.
"""

from __future__ import annotations

from civ_arena.game.civ6 import lua_translator
from civ_arena.game.civ6.read_correlation import (
    CorrelatedConnection,
    decorate,
    extract_correlated,
)

TOK = "a" * 32
FOREIGN = "b" * 32


def test_decorate_puts_the_marker_before_the_sentinel() -> None:
    lua = lua_translator.units_read()
    out = decorate(lua, TOK)
    assert out is not None
    assert out.index('print("ARENA_READ|' + TOK + '")') < out.rindex('---END---')
    # the script body is otherwise untouched
    assert out.replace(f'print("ARENA_READ|{TOK}")\n', "") == lua


def test_decorate_returns_none_without_a_sentinel() -> None:
    assert decorate("print('NOPE|1')", TOK) is None


def test_extract_skips_the_late_foreign_response() -> None:
    # hundred-20260920b's exact shape: the stale UNITS block (carrying the
    # FOREIGN token it was decorated with) precedes the real CITIES rows.
    rows = ["UNITS|1", "UNITROW|u0|x", f"ARENA_READ|{FOREIGN}",
            "CITIES|2", "CITYROW|c0|x", f"ARENA_READ|{TOK}"]
    assert extract_correlated(rows, TOK) == ["CITIES|2", "CITYROW|c0|x"]


def test_extract_skips_through_the_last_of_several_foreign_markers() -> None:
    rows = ["S|1", f"ARENA_READ|{FOREIGN}", "S|2", f"ARENA_READ|{'c' * 32}",
            "MINE|1", f"ARENA_READ|{TOK}"]
    assert extract_correlated(rows, TOK) == ["MINE|1"]


def test_extract_returns_none_when_my_marker_never_arrived() -> None:
    assert extract_correlated(["UNITS|1", "UNITROW|u0|x"], TOK) is None
    assert extract_correlated([], TOK) is None


def test_extract_plain_response_passes_through() -> None:
    rows = ["CITIES|2", "CITYROW|c0|x", f"ARENA_READ|{TOK}"]
    assert extract_correlated(rows, TOK) == ["CITIES|2", "CITYROW|c0|x"]


class FakeConn:
    """Scripted per-call responses; records the lua it was sent."""

    def __init__(self, responses: list) -> None:
        self.responses = responses
        self.sent: list[str] = []

    async def execute_read(self, lua: str, timeout: float = 5.0) -> list[str]:
        self.sent.append(lua)
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _lua() -> str:
    return lua_translator.cities_read(extended=False)


async def test_proxy_retries_a_lost_read_and_recovers() -> None:
    # call 1 serves the stale late block; the retry's response carries ITS
    # OWN token's marker, built from the lua the proxy actually sent
    calls = {"n": 0}

    async def scripted(lua, timeout=5.0):
        conn.sent.append(lua)
        calls["n"] += 1
        marker = lua.split('print("ARENA_READ|')[1].split('")')[0]
        if calls["n"] == 1:
            return ["UNITS|1", f"ARENA_READ|{FOREIGN}"]
        return ["CITIES|2", "CITYROW|c0|x", f"ARENA_READ|{marker}"]

    conn = FakeConn([])
    conn.execute_read = scripted  # type: ignore[assignment]

    proxy = CorrelatedConnection(conn)
    rows = await proxy.execute_read(_lua())
    assert rows == ["CITIES|2", "CITYROW|c0|x"]
    assert len(conn.sent) == 2  # exactly one retry


async def test_proxy_reports_empty_after_exhausted_correlation() -> None:
    async def always_lost(lua, timeout=5.0):
        return ["UNITS|1", f"ARENA_READ|{FOREIGN}"]  # never MY marker

    conn = FakeConn([])
    conn.execute_read = always_lost  # type: ignore[assignment]
    proxy = CorrelatedConnection(conn)
    assert await proxy.execute_read(_lua()) == []


async def test_undecorated_script_passes_through_untouched() -> None:
    conn = FakeConn([["ANY|rows"]])
    proxy = CorrelatedConnection(conn)
    assert await proxy.execute_read("print('NOPE|1')") == ["ANY|rows"]
    assert conn.sent == ["print('NOPE|1')"]  # no decoration attempted


def test_marker_is_stable_shape_for_the_fake_and_the_mod() -> None:
    # 32 lowercase hex, exactly like human_handoff tokens
    out = decorate(lua_translator.units_read(), "d" * 32)
    assert out is not None and 'print("ARENA_READ|' + "d" * 32 + '")' in out
