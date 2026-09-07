"""FakeMod's spectate engine model: a POLL-DRIVEN timeline.

The spectate driver only polls (Status/Trace/Digest/observes) plus the
lease-free ambient-window recorder commands — so the fake advances the
game on Status polls: the first poll establishes attach-mid-turn (the
human's HOOK_ENTER already in the ring as history), and every
polls_per_human_turn-th poll completes the human turn, runs the AI seats
(with a REAL mini-engine mutation), and starts the next human turn.
``ambient_diffs=True`` gives the Begin/End windows real _snapshot/_diff
semantics; the default (False) preserves every pre-existing fixture.
"""

import re

from civ_arena.game.civ6 import lua_translator
from civ_arena.game.civ6.fake_tuner_server import FakeMod


def _status(mod: FakeMod) -> dict[str, str]:
    lines = mod.respond(lua_translator.mod_status())
    assert lines is not None
    rows: dict[str, str] = {}
    for line in lines:
        for part in line.split("\n"):
            if "|" in part:
                k, v = part.split("|", 1)
                rows[k] = v
    return rows


def _spectate_mod(**overrides) -> FakeMod:
    cfg = {"human_seat": 0, "ai_seats": [1], "polls_per_human_turn": 2}
    cfg.update(overrides)
    return FakeMod(spectate=cfg, ambient_diffs=True)


def test_first_poll_establishes_attach_mid_turn() -> None:
    mod = _spectate_mod()
    assert mod.trace == []
    status = _status(mod)
    assert status["TURN_ACTIVE"] == "true"
    assert mod.trace == ["1|HOOK_ENTER|0"]
    assert mod.local_player == 0
    # the turn has NOT advanced yet (polls_per_human_turn not reached)
    _status(mod)
    assert mod.turn == 1


def test_poll_count_advances_round_with_ai_effects() -> None:
    mod = _spectate_mod()
    _status(mod)  # attach
    _status(mod)  # poll 1
    _status(mod)  # poll 2 -> advance
    assert mod.trace == [
        "1|HOOK_ENTER|0",
        "1|HOOK_DEACT|0",
        "1|HOOK_ENTER|1",
        "1|HOOK_DEACT|1",
        "2|HOOK_ENTER|0",
    ]
    assert mod.turn == 2
    assert _status(mod)["TURN_ACTIVE"] == "true"
    # the AI's warrior (unit 3, owner 1) really moved
    assert mod.units[3]["x"] == 31
    # engine turn-end effects landed (per-owner-city gold income)
    assert mod.players[0]["gold"] == 105


def test_default_fake_is_unchanged_by_status_polls() -> None:
    mod = FakeMod()
    for _ in range(5):
        _status(mod)
    assert mod.turn == 1
    assert mod.trace == []
    assert mod.turn_active is False


def test_ambient_window_captures_ai_delta() -> None:
    mod = _spectate_mod()
    _status(mod)  # attach (human turn 1 active)
    # the driver's turn-start close: end AI window from last round,
    # reopen it — here we exercise the diff directly
    assert mod.respond(lua_translator.begin_ambient_window(1)) == \
        ["AMBIENT_WINDOW|open|1"]
    # the AI acts (its effect mutates the board for real)
    mod._spectate_ai_effect(1)
    assert mod.respond(lua_translator.end_ambient_window(1)) == \
        ["AMBIENT_WINDOW|closed|1"]
    dumped = mod.respond(lua_translator.dump_ambient())
    assert dumped is not None
    rows = dumped[0].split("\n") if dumped != ["---END---"] else []
    moved = [r for r in rows if "unit.moved" in r and "u1:3" in r]
    assert moved, f"expected AI unit move row, got {rows}"
    assert re.search(r"pos\|30,30\|31,30", moved[0])


def test_ambient_window_captures_human_board_edits() -> None:
    mod = _spectate_mod()
    _status(mod)  # attach
    assert mod.respond(lua_translator.begin_ambient_window(0)) is not None
    # the human "plays": a direct board edit (exactly how a real human's
    # clicks bypass every harness command path)
    mod.units[2]["x"], mod.units[2]["y"] = 7, 7
    assert mod.respond(lua_translator.end_ambient_window(0)) is not None
    dumped = mod.respond(lua_translator.dump_ambient())
    rows = dumped if dumped != ["---END---"] else []
    assert any("unit.moved" in r and "u0:2" in r and "5,5" in r
               for r in rows), f"expected human move row, got {rows}"


def test_mutate_during_census_knob_drifts_digest() -> None:
    mod = _spectate_mod(mutate_during_census=True)
    d1 = mod.respond(lua_translator.mod_digest())
    assert d1 is not None
    units = mod.respond(lua_translator.units_read())
    assert units is not None  # the UNITS observe bumps the nonce
    d2 = mod.respond(lua_translator.mod_digest())
    assert d2 is not None
    assert d1 != d2


def test_quiet_census_is_consistent() -> None:
    mod = _spectate_mod()
    d1 = mod.respond(lua_translator.mod_digest())
    units = mod.respond(lua_translator.units_read())
    d2 = mod.respond(lua_translator.mod_digest())
    assert d1 is not None and d2 is not None and units is not None
    assert d1 == d2


# -- CAP-01 adversarial timeline: the game moves on OTHER poll types too ------

def test_advance_on_trace_poll_rolls_a_round_without_status() -> None:
    mod = _spectate_mod(polls_per_human_turn=2, advance_on=["trace"])
    _status(mod)  # attach (Status does NOT advance in this configuration)
    assert mod.turn == 1
    _trace(mod)
    assert mod.turn == 1  # one trace poll: not yet
    _trace(mod)  # second trace poll advances the whole round
    assert mod.turn == 2
    assert "1|HOOK_DEACT|0" in mod.trace and "2|HOOK_ENTER|0" in mod.trace


def test_advance_on_digest_poll() -> None:
    mod = _spectate_mod(polls_per_human_turn=1, advance_on=["digest"])
    _status(mod)
    assert mod.turn == 1
    mod.respond(lua_translator.mod_digest())
    assert mod.turn == 2


def test_default_timeline_still_advances_on_status_only() -> None:
    mod = _spectate_mod(polls_per_human_turn=1)
    _status(mod)
    for _ in range(4):
        _trace(mod)
        mod.respond(lua_translator.mod_digest())
    assert mod.turn == 1  # nothing but Status advances the default fake
    _status(mod)
    assert mod.turn == 2


def test_attach_between_turns_models_stale_history() -> None:
    mod = _spectate_mod(attach_turn_active=False)
    # pre-seed a STALE ring (an older round the recorder never saw)
    mod.trace = ["1|HOOK_ENTER|0", "1|HOOK_DEACT|0", "1|HOOK_ENTER|1",
                 "1|HOOK_DEACT|1", "2|HOOK_ENTER|0"]
    status = _status(mod)
    assert status["TURN_ACTIVE"] == "false"  # attached between turns
    # attach does NOT append a fresh HOOK_ENTER (the human's next turn
    # has not started); the stale history stays for the drain logic
    assert "3|HOOK_ENTER|0" not in mod.trace


def _trace(mod: FakeMod) -> list[str]:
    lines = mod.respond(lua_translator.mod_trace())
    assert lines is not None
    return lines
