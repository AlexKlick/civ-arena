"""Event log: schema, namespacing, seq monotonicity, torn-tail recovery, truncate."""

from __future__ import annotations

import json

import pytest

from civ_arena.arena.events import NAMESPACE_FIELDS, EventLog


def make_log(tmp_path, name="events.jsonl") -> EventLog:
    return EventLog(tmp_path / name)


def write_sample(log: EventLog, n: int = 5) -> None:
    for i in range(n):
        log.write(
            "TOOL_CALL",
            match_id="m1",
            game_instance_id="g1",
            turn=1,
            phase_player_id=0,
            player_id=0,
            agent_id="roman",
            visibility_scope="private_player",
            tool="move_unit",
            args={"unit_id": f"u{i}"},
            idempotency_key=f"k{i}",
        )


def test_append_only_schema_and_namespacing(tmp_path):
    log = make_log(tmp_path)
    write_sample(log, 4)
    recs = log.records()
    assert len(recs) == 4
    for i, rec in enumerate(recs):
        for fieldname in NAMESPACE_FIELDS:
            assert fieldname in rec, f"missing namespacing field {fieldname}"
        assert rec["seq"] == i
        assert rec["schema"] == 1
        assert rec["kind"] == "TOOL_CALL"
        assert rec["ts"]  # envelope present but excluded from replay hashes


def test_unknown_kind_rejected(tmp_path):
    log = make_log(tmp_path)
    with pytest.raises(ValueError, match="unknown event kind"):
        log.write("NUKE", match_id="m", game_instance_id="g", turn=1,
                  phase_player_id=0, player_id=None, agent_id=None,
                  visibility_scope="referee")


def test_seq_continues_across_reopen(tmp_path):
    log = make_log(tmp_path)
    write_sample(log, 3)
    log.close()
    reopened = make_log(tmp_path)
    assert len(reopened) == 3
    seq = reopened.write("HEARTBEAT", match_id="m1", game_instance_id="g2", turn=1,
                         phase_player_id=-1, player_id=None, agent_id=None,
                         visibility_scope="referee")
    assert seq == 3
    assert len(reopened.records()) == 4


def test_torn_tail_dropped_and_seq_set_to_survivors(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    write_sample(log, 3)
    log.close()
    # simulate a kill -9 mid-write: append a partial JSON line
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"schema":1,"seq":3,"kind":"TOOL_RE')

    reopened = EventLog(path)
    recs = reopened.records()
    assert len(recs) == 3
    assert len(reopened) == 3  # next write continues from the surviving prefix
    reopened.write("HEARTBEAT", match_id="m1", game_instance_id="g9", turn=3,
                   phase_player_id=-1, player_id=None, agent_id=None,
                   visibility_scope="referee")
    final = reopened.records()
    assert final[-1]["seq"] == 3
    assert final[-1]["game_instance_id"] == "g9"
    reopened.close()


def test_corrupt_midfile_line_raises_at_construction(tmp_path):
    """A torn TRAILING line is recoverable; corruption mid-file is not."""
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    write_sample(log, 2)
    log.close()
    lines = path.read_text().splitlines()
    lines.insert(1, "this is not json")
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="corrupt event log"):
        EventLog(path)


def test_truncate_to_keeps_prefix_and_continues(tmp_path):
    log = make_log(tmp_path)
    write_sample(log, 5)
    log.truncate_to(3)
    recs = log.records()
    assert [r["seq"] for r in recs] == [0, 1, 2]
    log.write("CHECKPOINT", match_id="m1", game_instance_id="g1", turn=2,
              phase_player_id=-1, player_id=None, agent_id=None,
              visibility_scope="referee", checkpoint_path="x", checkpoint_hash="h")
    recs = log.records()
    assert len(recs) == 4 and recs[-1]["seq"] == 3
    with pytest.raises(ValueError):
        log.truncate_to(99)


def test_records_are_single_line_json(tmp_path):
    log = make_log(tmp_path)
    write_sample(log, 2)
    raw = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(raw) == 2
    json.loads(raw[0])
    json.loads(raw[1])
