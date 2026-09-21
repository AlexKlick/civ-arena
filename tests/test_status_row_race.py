"""The mod's status prints racing into response reads — both chain-20260920
death shapes, pinned.

- chain match-003: ``AMBIENT_WINDOW|closed|1`` landed in a ledger dump read
  -> ``parse_ledger_lines`` raised "non-ledger row" -> recovery abort.
- chain match-001: a status print landed in the HUMAN_HANDOFF receipt read
  -> the exact-equality check called the receipt "mismatched" -> abort.

Status rows are droppable noise in ANY response read; genuinely unknown
rows and torn LEDGER/AMBIENT rows still fail closed.
"""

from __future__ import annotations

from civ_arena.game.civ6.response_parser import (
    STATUS_ROW_PREFIXES,
    drop_status_rows,
    parse_ledger_lines,
)

LEDGER_ROW = "LEDGER|unit.moves|unit|u131073|moves|2|0"


def test_ledger_dump_survives_interleaved_status_rows() -> None:
    # match-003's exact row, plus the wider vocabulary
    docs = parse_ledger_lines([
        "AMBIENT_WINDOW|closed|1",              # the killing row
        LEDGER_ROW,
        "PUPPET_ACTIVE|true",
        "MOD_VERSION|0.4.0",
        "SUPPORTS_LEDGER|true",
    ])
    assert len(docs) == 1 and docs[0]["entity_id"] == "u131073"


def test_unknown_rows_still_raise() -> None:
    # fail-closed preserved: a row outside the vocabulary is not noise
    try:
        parse_ledger_lines(["GARBAGE|something"])
    except ValueError as exc:
        assert "non-ledger row" in str(exc)
    else:
        raise AssertionError("unknown row was silently accepted")


def test_torn_ledger_row_still_raises() -> None:
    # the watchdog contract: a torn data row must never shrink the multiset
    try:
        parse_ledger_lines(["LEDGER|unit.moves|unit|u131073|moves|2"])
    except ValueError as exc:
        assert "malformed LEDGER row" in str(exc)
    else:
        raise AssertionError("torn LEDGER row was silently accepted")


def test_receipt_read_tolerates_status_noise() -> None:
    # match-001's shape: the receipt IS present, interleaved with prints
    expected = "HUMAN_HANDOFF|tok|activate|0|9|observed"
    assert drop_status_rows([
        "AMBIENT_WINDOW|closed|1",
        expected,
        "FROZEN|true",
    ]) == [expected]


def test_receipt_read_still_fails_on_wrong_content() -> None:
    # noise tolerance must not become shape tolerance
    assert drop_status_rows(["AMBIENT_WINDOW|closed|1"]) == []
    assert drop_status_rows(["HUMAN_HANDOFF|WRONG|activate|0|9|observed"]) == [
        "HUMAN_HANDOFF|WRONG|activate|0|9|observed"]


def test_vocabulary_covers_the_mods_prints() -> None:
    # the load-bearing completeness claim: every mod status family is known
    for prefix in ("AMBIENT_WINDOW", "PUPPET_ACTIVE", "HANDOFF", "DIGEST",
                   "ERR", "ATTACH_CURRENT", "FINISHED_MOVES", "FROZEN"):
        assert prefix in STATUS_ROW_PREFIXES, prefix
    # and the data prefixes are NOT in it
    assert "LEDGER" not in STATUS_ROW_PREFIXES
    assert "AMBIENT" not in STATUS_ROW_PREFIXES
