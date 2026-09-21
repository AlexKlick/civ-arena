"""Read correlation v2 — the marker LEADS the script.

overnight-20260920's lesson: the mod's own functions print ---END--- rows
INSIDE their output (Puppeteer.Handshake, SetPuppet, Status), and the
collector stops at the first sentinel row — an END-placed marker never
arrives on exactly the reads that matter (the S4 handshake died
present:False off a correlation-exhausted []). v2 prints the marker
FIRST; data = rows after it up to the first sentinel.
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


def test_decorate_prefixes_the_marker() -> None:
    lua = lua_translator.units_read()
    out = decorate(lua, TOK)
    assert out is not None
    assert out.index('print("ARENA_READ|' + TOK + '")') < out.index('print("UNITS|1")')
    # the script body is otherwise untouched
    assert out.replace(f'print("ARENA_READ|{TOK}")\n', "", 1).lstrip("\n") == lua.lstrip("\n")


def test_decorate_keeps_leading_comments_above_the_marker() -> None:
    lua = ("-- arena:human_handoff=verify,1,1,deadbeef\n\n"
           "local seats={0,1}\nprint('V|1')\nprint('---END---')\n")
    out = decorate(lua, TOK)
    assert out is not None
    assert out.index("-- arena:human_handoff") < out.index(f'print("ARENA_READ|{TOK}")')


def test_decorate_returns_none_without_a_sentinel() -> None:
    assert decorate("print('NOPE|1')", TOK) is None


def test_extract_takes_rows_after_marker_to_first_sentinel() -> None:
    # the mod's Handshake() shape: its own ---END--- ends the DATA
    rows = [f"ARENA_READ|{TOK}", "MOD_VERSION|0.4.0", "SUPPORTS_FREEZE|true",
            "---END---"]
    assert extract_correlated(rows, TOK) == ["MOD_VERSION|0.4.0", "SUPPORTS_FREEZE|true"]


def test_extract_skips_a_late_foreign_response() -> None:
    # hundred-20260920b's shape: the stale UNITS block (carrying the
    # FOREIGN token it was decorated with) precedes this command's marker
    rows = ["UNITS|1", "UNITROW|u0|x", f"ARENA_READ|{FOREIGN}",
            f"ARENA_READ|{TOK}", "CITIES|2", "CITYROW|c0|x", "---END---"]
    assert extract_correlated(rows, TOK) == ["CITIES|2", "CITYROW|c0|x"]


def test_extract_returns_none_when_my_marker_never_arrived() -> None:
    assert extract_correlated(["UNITS|1", "UNITROW|u0|x"], TOK) is None
    assert extract_correlated([], TOK) is None
    # truncated before my command executed at all (a stale block's END)
    assert extract_correlated(["UNITS|1", "---END---"], TOK) is None


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
    calls = {"n": 0}

    async def scripted(lua, timeout=5.0):
        conn.sent.append(lua)
        calls["n"] += 1
        marker = lua.split('print("ARENA_READ|')[1].split('")')[0]
        if calls["n"] == 1:
            return ["UNITS|1", f"ARENA_READ|{FOREIGN}", "---END---"]
        return [f"ARENA_READ|{marker}", "CITIES|2", "CITYROW|c0|x", "---END---"]

    conn = FakeConn([])
    conn.execute_read = scripted  # type: ignore[assignment]

    proxy = CorrelatedConnection(conn)
    rows = await proxy.execute_read(_lua())
    assert rows == ["CITIES|2", "CITYROW|c0|x"]
    assert len(conn.sent) == 2  # exactly one retry


async def test_proxy_reports_empty_after_exhausted_correlation() -> None:
    async def always_lost(lua, timeout=5.0):
        return ["UNITS|1", f"ARENA_READ|{FOREIGN}", "---END---"]

    conn = FakeConn([])
    conn.execute_read = always_lost  # type: ignore[assignment]
    proxy = CorrelatedConnection(conn)
    assert await proxy.execute_read(_lua()) == []


async def test_undecorated_script_passes_through_untouched() -> None:
    conn = FakeConn([["ANY|rows"]])
    proxy = CorrelatedConnection(conn)
    assert await proxy.execute_read("print('NOPE|1')") == ["ANY|rows"]
    assert conn.sent == ["print('NOPE|1')"]  # no decoration attempted
