"""Per-agent telemetry attribution; token counters stubbed for the spike."""

from __future__ import annotations

from civ_arena.arena.telemetry import TelemetryRegistry


def test_per_agent_attribution():
    reg = TelemetryRegistry()
    reg.note_call("roman", "move_unit", 3, ok=True)
    reg.note_call("roman", "move_unit", 5, ok=True)
    reg.note_call("roman", "attack", 7, ok=False)
    reg.note_call("korea", "fortify", 2, ok=True)

    snap = reg.snapshot()
    assert set(snap) == {"korea", "roman"}
    roman = snap["roman"]
    assert roman["total_calls"] == 3
    assert roman["total_errors"] == 1
    assert roman["total_ms"] == 15
    assert roman["tool_calls"] == {"move_unit": 2, "attack": 1}
    assert roman["tool_errors"] == {"attack": 1}
    korea = snap["korea"]
    assert korea["total_calls"] == 1 and korea["total_ms"] == 2


def test_token_counters_stubbed_and_model_none():
    reg = TelemetryRegistry()
    reg.note_call("roman", "move_unit", 1, ok=True)
    doc = reg.snapshot()["roman"]
    assert doc["input_tokens"] == 0
    assert doc["output_tokens"] == 0
    assert doc["model"] is None


def test_snapshot_cross_checks_against_event_log_recount(tmp_path):
    """Counters must equal a recount of TOOL_RESULT records in the log."""
    from civ_arena.arena.events import EventLog

    log = EventLog(tmp_path / "events.jsonl")
    reg = TelemetryRegistry()
    calls = [
        ("roman", "move_unit", 4, True),
        ("roman", "attack", 6, False),
        ("korea", "fortify", 2, True),
    ]
    for agent, tool, ms, ok in calls:
        log.write(
            "TOOL_RESULT",
            match_id="m", game_instance_id="g", turn=1, phase_player_id=0,
            player_id=0, agent_id=agent, visibility_scope="private_player",
            tool=tool, status="accepted" if ok else "rejected", duration_ms=ms,
        )
        reg.note_call(agent, tool, ms, ok)

    recount: dict[str, dict] = {}
    for rec in log.records():
        if rec["kind"] != "TOOL_RESULT":
            continue
        agent = rec["agent_id"]
        bucket = recount.setdefault(agent, {"calls": 0, "errors": 0, "ms": 0})
        bucket["calls"] += 1
        bucket["ms"] += rec.get("duration_ms") or 0
        if rec["status"] != "accepted":
            bucket["errors"] += 1

    for agent, doc in reg.snapshot().items():
        assert doc["total_calls"] == recount[agent]["calls"]
        assert doc["total_errors"] == recount[agent]["errors"]
        assert doc["total_ms"] == recount[agent]["ms"]
