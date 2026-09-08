"""CAP-R1 round-1 remediation tests: the mod/transport/trust plane.

Codex-r1 findings pinned here (docs/codex-r1-findings-20260907.md):

- #1 the mod's ONE shared ``ambient_snapshot`` corrupted every windowed
  ambient read whenever more than one player's window was open (live
  pilot: 0/146 human before-values matched the census; AI manifests 63%
  spawn/despawn storms). The fix is a per-player snapshot table in the
  mod — the fake always modeled per-player semantics, so these tests
  bind BOTH to the same contract: fake behavior tests + mod source pins.
- #8 roster discovery via OVX can never see city-states (the live OVX
  enumerates alive majors only) — the mod gains ``Puppeteer.Roster()``
  enumerating ALL players with major/minor classes.
- #5 the recorder allowlist's ``startswith`` check admitted compound
  Lua (``Puppeteer.DumpAmbient(); Puppeteer.FinishAllMoves(0)``) — the
  transport now exact-matches the three recorder calls.
- #9 a teardown failure used to skip MATCH_END/summary entirely —
  closeout evidence now always writes, with the teardown fault recorded.
"""

import json
import re
from pathlib import Path

import pytest

from civ_arena.config import parse_config
from civ_arena.game.civ6 import live_driver as ld
from civ_arena.game.civ6.fake_tuner_server import FakeMod, FakeTunerServer
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from civ_arena.game.civ6.response_parser import parse_handshake
from civ_arena.game.civ6.spectate_capture import (
    RecorderCapabilityError,
    SpectateLimits,
    SpectateTransport,
)

MOD_LUA = ld.MOD_DEFAULT.read_text(encoding="utf-8")
MOD_PATH = Path(ld.MOD_DEFAULT)

SPECTATE_DOC = {
    "match": {"match_id": "cap-r1-test", "seed": 1, "adapter": "firetuner"},
    "spectate": {"operator": "alexk"},
    "agents": [],
}


# -- mod contract pins (the mod has no Python runtime: source pins ARE the
# executable contract the fake must mirror) ---------------------------------


def test_mod_version_bumped_with_new_capabilities() -> None:
    assert 'Puppeteer.version = "0.4.0"' in MOD_LUA
    # same-version reinjection guard moved with the version
    assert 'Puppeteer.version == "0.4.0"' in MOD_LUA
    assert "supports_ambient_windows = true" in MOD_LUA
    assert "supports_roster = true" in MOD_LUA
    # the handshake ADVERTISES both (fail-closed parsing depends on them)
    assert 'print("SUPPORTS_AMBIENT_WINDOWS|"' in MOD_LUA
    assert 'print("SUPPORTS_ROSTER|"' in MOD_LUA


def test_mod_ambient_windows_are_per_player() -> None:
    """#1: one snapshot TABLE keyed by playerID, never a shared slot."""
    assert "local ambient_snapshots = {}" in MOD_LUA
    # Begin writes the player's own slot; End diffs (and clears) only it
    assert "ambient_snapshots[playerID] = snapshot_player(playerID)" in MOD_LUA
    assert "ambient_snapshots[playerID]" in MOD_LUA
    # the corrupting shared slot must be gone
    assert "local ambient_snapshot = nil" not in MOD_LUA
    assert not re.search(r"^\s*ambient_snapshot = snapshot_player", MOD_LUA,
                         re.MULTILINE)
    # honest receipts: rebase on double-Begin, closed_stale on End-without-Begin
    assert '"rebase"' in MOD_LUA and '"closed_stale"' in MOD_LUA


def test_mod_roster_enumerates_all_players_not_majors_only() -> None:
    """#8: Roster walks pairs(Players) WITHOUT the IsMajor filter that
    hides city-states from every other read."""
    assert "function Puppeteer.Roster()" in MOD_LUA
    roster_body = MOD_LUA.split("function Puppeteer.Roster()", 1)[1]
    roster_body = roster_body.split("endfunction", 1)[0]
    roster_body = roster_body.split("\nend", 1)[0]
    assert "pairs(Players)" in roster_body
    assert "ROSTER|" in roster_body
    # class split inside the roster (minor default, IsMajor upgrade) — and
    # NO hard IsMajor() gate on enumeration
    assert "IsMajor" in roster_body
    assert not re.search(r"if[^%\n]*IsMajor\(\)[^%\n]*then\s*\n\s*"
                         r"table\.insert\(rows", roster_body)


def test_handshake_parses_new_capabilities_fail_closed() -> None:
    base = ["MOD_PRESENT|true", "MOD_VERSION|0.4.0",
            "SUPPORTS_FREEZE|true", "SUPPORTS_LEDGER|true",
            "SUPPORTS_DIGEST|true", "SUPPORTS_COMMAND_DIFF|true",
            "SUPPORTS_GUARDED_HANDOFF|true", "SUPPORTS_REWARD_RECEIPTS|true"]
    with_new = parse_handshake(
        base + ["SUPPORTS_AMBIENT_WINDOWS|true", "SUPPORTS_ROSTER|true"])
    assert with_new["supports_ambient_windows"] is True
    assert with_new["supports_roster"] is True
    without = parse_handshake(base)  # an older mod: absent => False, never True
    assert without["supports_ambient_windows"] is False
    assert without["supports_roster"] is False


# -- fake parity with the mod contract ---------------------------------------


def _fake(spectate: dict | None = None) -> FakeMod:
    cfg = {"human_seat": 0, "ai_seats": [1, 2], "polls_per_human_turn": 3}
    if spectate:
        cfg.update(spectate)
    return FakeMod(spectate=cfg, ambient_diffs=True)


def test_fake_overlapping_windows_diff_own_baselines() -> None:
    """#1 semantics: windows for SEVERAL players open at once; each End
    diffs against ITS OWN Begin baseline. Cross-player composites (the
    live v0.3.10 corruption) must be impossible."""
    from civ_arena.game.civ6.spectate_capture import parse_ambient_rows

    def _ambient_for(mod: FakeMod, pid: int) -> list[dict]:
        mod.ambient_rows.clear()
        mod.respond(f"Puppeteer.EndAmbientWindow({pid})")
        dumped = mod.respond("Puppeteer.DumpAmbient()")  # drains + returns
        return parse_ambient_rows(list(dumped))

    mod = _fake()
    mod.respond("Puppeteer.BeginAmbientWindow(0)")
    mod.respond("Puppeteer.BeginAmbientWindow(1)")
    mod.respond("Puppeteer.BeginAmbientWindow(2)")
    # player 1's unit marches while every window is open
    for _, u in sorted(mod.units.items()):
        if u["owner"] == 1:
            u["x"] += 3
            break
    rows0 = _ambient_for(mod, 0)
    rows1 = _ambient_for(mod, 1)
    rows2 = _ambient_for(mod, 2)
    assert rows1, "the mutated player's window must book its delta"
    assert all(r["entity_id"].startswith("u1:") for r in rows1)
    assert not rows0 and not rows2, (
        "untouched players' windows must be EMPTY — a non-empty diff here "
        "is the shared-snapshot cross-player corruption")


def test_fake_window_receipts_rebase_and_stale() -> None:
    mod = _fake()
    assert "AMBIENT_WINDOW|open|0" in mod.respond(
        "Puppeteer.BeginAmbientWindow(0)")
    assert "AMBIENT_WINDOW|rebase|0" in mod.respond(
        "Puppeteer.BeginAmbientWindow(0)")
    # End without Begin: honest stale receipt, no fabricated diff
    mod.ambient_rows.clear()
    out = mod.respond("Puppeteer.EndAmbientWindow(5)")
    assert "AMBIENT_WINDOW|closed_stale|5" in out
    assert not mod.ambient_rows


def test_fake_roster_returns_all_players_with_classes() -> None:
    mod = _fake({"minor_seats": [63]})
    mod.players[63] = {"gold": 0, "researching": "", "researched": []}
    out = mod.respond("Puppeteer.Roster()")
    rows = [ln for ln in out if ln.startswith("ROSTER|")]
    by_pid = {int(r.split("|")[1]): r.split("|")[2] for r in rows}
    assert by_pid == {0: "major", 1: "major", 2: "major", 63: "minor"}


def test_fake_defaults_advertise_new_capabilities() -> None:
    mod = _fake()
    out = mod.respond("Puppeteer.Handshake()")
    doc = parse_handshake([ln for ln in out if ln != "---END---"])
    assert doc["supports_ambient_windows"] is True
    assert doc["supports_roster"] is True


# -- transport exact-match allowlist (#5) ------------------------------------


class _RecordingAdapter:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def read_raw(self, lua: str) -> list[str]:
        self.sent.append(lua)
        return ["---END---"]


def _transport() -> tuple[SpectateTransport, _RecordingAdapter]:
    adapter = _RecordingAdapter()
    return SpectateTransport(adapter), adapter


async def test_transport_accepts_exact_recorder_calls_only() -> None:
    transport, adapter = _transport()
    for lua in ("Puppeteer.BeginAmbientWindow(0)",
                "Puppeteer.EndAmbientWindow(12)",
                "Puppeteer.DumpAmbient()",
                "Puppeteer.Roster()"):
        await transport.read_raw(lua)
    assert adapter.sent == [
        "Puppeteer.BeginAmbientWindow(0)",
        "Puppeteer.EndAmbientWindow(12)",
        "Puppeteer.DumpAmbient()",
        "Puppeteer.Roster()",
    ]
    assert transport.census["recorder_commands"] == 4


async def test_transport_rejects_compound_lua_after_allowed_prefix() -> None:
    """#5: the startswith hole — an allowed call followed by a mutation
    must be refused BEFORE dispatch (F-03 enforcement stays real)."""
    transport, adapter = _transport()
    for lua in (
        "Puppeteer.DumpAmbient(); Puppeteer.FinishAllMoves(0)",
        "Puppeteer.DumpAmbient();Puppeteer.SetPuppet(0, true)",
        "Puppeteer.BeginAmbientWindow(0) -- sneaky comment",
        "Puppeteer.BeginAmbientWindow(-1)",
        "Puppeteer.BeginAmbientWindow(0, 1)",
        "Puppeteer.BeginAmbientWindow(0) ",
        "Puppeteer.DumpAmbient ();",
        "xPuppeteer.DumpAmbient()",
    ):
        with pytest.raises(RecorderCapabilityError):
            await transport.read_raw(lua)
    assert not adapter.sent, "a rejected call must never reach the wire"
    assert transport.census["rejected"] == 8


async def test_transport_roster_is_counted_and_parsed() -> None:
    class _RosterAdapter(_RecordingAdapter):
        async def read_raw(self, lua: str) -> list[str]:
            await super().read_raw(lua)
            return ["ROSTER|0|major", "ROSTER|1|major", "ROSTER|63|minor",
                    "---END---"]

    transport = SpectateTransport(_RosterAdapter())
    roster = await transport.roster()
    assert roster == {0: "major", 1: "major", 63: "minor"}
    assert transport.census["recorder_commands"] == 1


# -- phase capability gate + guarded teardown (#9) ----------------------------


async def _run(tmp_path: Path, mod: FakeMod, turns: int = 1,
               break_teardown: bool = False):
    server = FakeTunerServer(mod=mod)
    port = await server.start()
    adapter = FireTunerAdapter("127.0.0.1", port)
    run_dir = tmp_path / "run"
    original_teardown = adapter.teardown
    if break_teardown:

        async def _boom() -> None:
            raise RuntimeError("IncompleteReadError: tuner dropped")

        adapter.teardown = _boom  # type: ignore[method-assign]
    try:
        rc = await ld.phase_spectate(
            parse_config(SPECTATE_DOC), adapter, run_dir, turns, MOD_LUA,
            limits=SpectateLimits(poll_s=0.01, heartbeat_s=3600.0,
                                  match_s=60.0))
        events = [json.loads(line) for line
                  in (run_dir / "events.jsonl").read_text().splitlines()]
        return rc, events, json.loads(
            (run_dir / "summary.json").read_text())
    finally:
        adapter.teardown = original_teardown  # restore before cleanup
        await adapter.teardown()
        await server.stop()


async def test_spectate_refuses_mod_without_per_player_windows(
        tmp_path) -> None:
    """The phase fails CLOSED against a mod that would corrupt its data:
    per-player ambient windows + roster are required capabilities."""
    mod = _fake()
    mod.supports_ambient_windows = False
    with pytest.raises(RuntimeError, match="per-player ambient"):
        await _run(tmp_path, mod)


async def test_spectate_refuses_mod_without_roster(tmp_path) -> None:
    mod = _fake()
    mod.supports_roster = False
    with pytest.raises(RuntimeError, match="roster"):
        await _run(tmp_path, mod)


async def test_teardown_failure_still_writes_match_end(tmp_path) -> None:
    """#9: closeout evidence survives a disconnect failure — the teardown
    fault is RECORDED, never allowed to discard the summary/MATCH_END."""
    mod = _fake()
    rc, events, summary = await _run(tmp_path, mod, turns=1,
                                     break_teardown=True)
    assert rc == 2  # not clean: the run ended via the teardown failure path
    assert events[-1]["kind"] == "MATCH_END"
    assert summary["outcome"] is not None
    assert summary["cleanup"]["status"].startswith("teardown_error")
    assert "IncompleteReadError" in summary["cleanup"]["status"]
    # the lifecycle op still counts inside the census
    assert summary["command_census"]["recorder_lifecycle"] == 3


async def test_clean_teardown_status_unchanged(tmp_path) -> None:
    mod = _fake()
    _, _, summary = await _run(tmp_path, mod, turns=1)
    assert summary["cleanup"]["status"] == \
        "disconnect_only_no_game_actions_no_leases"
