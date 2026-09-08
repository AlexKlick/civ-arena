"""Exporter v0: class separation, ref integrity, determinism, honest gaps.

The two sample classes never impersonate each other; every reference
resolves into the source events.jsonl; pre-ledger and pre-boundary runs
export with explicit quality flags instead of fabricated values; and the
same run dir exports byte-identical sample bytes twice.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from civ_arena.research.export_dataset import export, write

REPO = Path(__file__).resolve().parents[1]


def _write_run(tmp_path: Path, *, spectate: bool, with_ledger: bool = False,
               with_boundaries: bool = False, match_id: str = "m1",
               instance: str = "m1-i1", turns: int = 2) -> Path:
    run_dir = tmp_path / f"run-{instance}"
    run_dir.mkdir()
    rows: list[dict] = []
    seq = 0

    def add(kind: str, **fields):
        nonlocal seq
        rows.append({"schema": 1, "seq": seq, "kind": kind,
                     "ts": "2026-09-07T00:00:00+00:00",
                     "match_id": match_id, "game_instance_id": instance,
                     "turn": fields.pop("turn", 0), "phase_player_id": 0,
                     "player_id": fields.pop("player_id", None),
                     "agent_id": fields.pop("agent_id", None),
                     "visibility_scope": "spectator" if spectate else "referee",
                     **fields})
        seq += 1

    add("MATCH_START", config={"agents": [] if spectate else
                               [["a0", 0, "llm"]]})
    if spectate:
        for turn in range(1, turns + 1):
            add("HUMAN_TURN_START", turn=turn, operator="alexk",
                window="turn_start")
            add("SPECTATOR_SNAPSHOT", turn=turn, round=turn,
                ambient={"1": [{"kind": "unit.moved", "entity_type": "unit",
                                "entity_id": "u1:3", "attr": "pos",
                                "before": "0,0", "after": "1,0"}]},
                digest={"before": "a", "after": "b", "consistent": True})
            add("HUMAN_TURN_END", turn=turn, operator="alexk",
                duration_s=5.0, human_ambient=[
                    {"kind": "unit.moved", "entity_type": "unit",
                     "entity_id": "u0:2", "attr": "pos",
                     "before": "1,1", "after": "2,2"}],
                digest_after="c", overrun=False)
    else:
        for turn in range(1, turns + 1):
            if with_boundaries:
                add("HEARTBEAT", turn=turn, audit="decision_boundary",
                    decision_id=f"d{turn}", directive_id=f"dir{turn}",
                    provider_requests=1 if turn == 1 else 0,
                    source="model" if turn == 1 else "autopilot")
            add("TOOL_CALL", turn=turn, player_id=0, agent_id="a0",
                tool="get_units", args={}, args_digest="x")
            add("TOOL_RESULT", turn=turn, player_id=0, agent_id="a0",
                tool="get_units", status="accepted", observed={"own_units": 2})
            add("TOOL_CALL", turn=turn, player_id=0, agent_id="a0",
                tool="move_unit", args={"unit_id": "u0:1", "dest": "1,1"},
                args_digest="y")
            add("TOOL_RESULT", turn=turn, player_id=0, agent_id="a0",
                tool="move_unit", status="accepted",
                mutations=[{"kind": "unit.moved", "entity_type": "unit",
                            "entity_id": "u0:1", "attr": "pos",
                            "before": "0,0", "after": "1,1"}],
                receipts=[{"kind": "unit.moved"}])
    add("MATCH_END", turn=turns, summary={
        "match_id": match_id, "game_instance_id": instance,
        "final_turn": turns, "aborted": None,
        "phase": "spectate" if spectate else "dispatch-hotseat"})
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n")
    summary = {"match_id": match_id, "game_instance_id": instance,
               "final_turn": turns, "aborted": None,
               "phase": "spectate" if spectate else "dispatch-hotseat"}
    (run_dir / "summary.json").write_text(json.dumps(summary, sort_keys=True))
    if with_ledger:
        ledger = []
        for turn in range(1, turns + 1):
            if turn == 1:
                ledger.append({"ts": "t", "agent_id": "a0", "player_id": 0,
                               "request_kind": "generation", "attempt": 0,
                               "status_code": 200, "latency_ms": 10,
                               "model": "m", "payload_hash": "h",
                               "input_tokens": 5, "output_tokens": 3,
                               "decision_id": "d1",
                               "logical_request_id": "lr1",
                               "request_set_key": "h:generation"})
        (run_dir / "llm_costs.jsonl").write_text(
            "\n".join(json.dumps(r, sort_keys=True) for r in ledger) + "\n")
    return run_dir


# -- controlled decisions ------------------------------------------------------

def test_controlled_export_joins_refs_and_costs(tmp_path):
    run_dir = _write_run(tmp_path, spectate=False, with_ledger=True,
                         with_boundaries=True)
    samples, manifest = export(run_dir)
    assert len(samples) == 2
    assert all(s["sample_class"] == "controlled_decision" for s in samples)
    first = samples[0]
    assert first["decision_id"] == "d1"
    assert first["directive_id"] == "dir1"
    assert first["request_costs"]["tokens_in"] == 5
    assert first["request_costs"]["attempts"] == 1
    assert first["observation_refs"] and first["requested_action_refs"]
    assert first["execution_receipt_refs"]
    assert first["outcome_horizon"] == 1  # final_turn 2 - turn 1
    assert first["visibility_class"] == "ordinary_player_information"
    quiet = samples[1]
    assert quiet["provider_requests"] == 0
    assert quiet["request_costs"] is None  # no ledger rows for d2
    # ref integrity: every referenced seq exists
    events = [json.loads(line) for line
              in (run_dir / "events.jsonl").read_text().splitlines()]
    for sample in samples:
        for ref in (sample["observation_refs"]
                    + sample["requested_action_refs"]
                    + sample["execution_receipt_refs"]):
            assert events[ref["seq"]]["seq"] == ref["seq"]
    assert manifest["sample_counts"] == {"controlled_decision": 2}


def test_pre_ledger_and_pre_boundary_runs_export_with_flags_not_fabrication(
        tmp_path):
    run_dir = _write_run(tmp_path, spectate=False)  # no ledger, no boundaries
    samples, _ = export(run_dir)
    assert samples
    for s in samples:
        assert s["decision_id"] is None
        assert s["request_costs"] is None
        assert "pre_ledger_run" in s["quality_flags"]
        assert "pre_boundary_run" in s["quality_flags"]
        # the turn's evidence STILL joins (refs present, costs honestly null)
        assert s["requested_action_refs"]


# -- spectator intervals -------------------------------------------------------

def test_spectator_intervals_never_impersonate_actions(tmp_path):
    run_dir = _write_run(tmp_path, spectate=True)
    samples, _ = export(run_dir)
    assert len(samples) == 2
    for s in samples:
        assert s["sample_class"] == "spectator_interval"
        assert s["actor_attribution"] == "owner_only"
        assert s["observed_actor_ids"] == []
        assert s["interval_closed"] is True
        assert s["ambient_row_count"] == 1
        assert s["entity_owner_ids"] == ["u0"]
        assert "exact_action_imitation" in s["ineligible_with_reasons"]
        # class purity: no controlled-decision fields ever appear
        for forbidden in ("decision_id", "directive_id", "request_costs",
                          "provider_requests", "requested_action_refs"):
            assert forbidden not in s


def test_unpaired_final_start_exports_open_interval(tmp_path):
    run_dir = _write_run(tmp_path, spectate=True, turns=2)
    # drop the final HUMAN_TURN_END (interrupted-run shape)
    lines = (run_dir / "events.jsonl").read_text().splitlines()
    kept = [line for line in lines
            if json.loads(line)["kind"] != "HUMAN_TURN_END"
            or json.loads(line)["turn"] != 2]
    # renumber seqs to stay contiguous (the exporter requires contiguity)
    renumbered = []
    for i, line in enumerate(kept):
        row = json.loads(line)
        row["seq"] = i
        renumbered.append(json.dumps(row, sort_keys=True))
    (run_dir / "events.jsonl").write_text("\n".join(renumbered) + "\n")
    samples, _ = export(run_dir)
    assert samples[-1]["interval_closed"] is False
    assert "interval_open_at_export" in samples[-1]["quality_flags"]
    assert samples[-1]["observed_interval_end"] is None


def test_wrap_affected_round_pairs_chronologically_with_mismatch_flag(
        tmp_path):
    """The real corpus shape: a round whose DEACT was lost to a ring wrap
    is closed by the NEXT turn's END — paired chronologically, both turn
    labels kept, mismatch flagged, never silently reconciled."""
    run_dir = _write_run(tmp_path, spectate=True, turns=2)
    lines = (run_dir / "events.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in lines]
    # the real wrap shape: turn-1's END was lost AND the driver never
    # emitted turn-2's START (the round stayed open) — so turn-2's END
    # closes turn-1's round
    rows = [r for r in rows
            if not (r["kind"] == "HUMAN_TURN_END" and r["turn"] == 1)
            and not (r["kind"] == "HUMAN_TURN_START" and r["turn"] == 2)
            and not (r["kind"] == "SPECTATOR_SNAPSHOT" and r["turn"] == 2)]
    renumbered = []
    for i, row in enumerate(rows):
        row["seq"] = i
        renumbered.append(json.dumps(row, sort_keys=True))
    (run_dir / "events.jsonl").write_text("\n".join(renumbered) + "\n")
    samples, _ = export(run_dir)
    by_flag = [s for s in samples if "turn_label_mismatch" in s["quality_flags"]]
    assert len(by_flag) == 1
    s = by_flag[0]
    assert s["interval_turn_start"] == 1 and s["interval_turn_end"] == 2
    assert s["interval_closed"] is True  # chronologically closed
    assert s["ambient_row_count"] == 1   # the closing END's ambient rows


# -- determinism + lineage -----------------------------------------------------

def test_export_is_deterministic(tmp_path):
    run_dir = _write_run(tmp_path, spectate=False, with_ledger=True,
                         with_boundaries=True)
    out1 = tmp_path / "a.jsonl"
    out2 = tmp_path / "b.jsonl"
    s1, m1 = export(run_dir)
    write(s1, m1, out1)
    s2, m2 = export(run_dir)
    write(s2, m2, out2)
    assert out1.read_bytes() == out2.read_bytes()


def test_game_siblings_and_restarts_share_split_group(tmp_path):
    a = _write_run(tmp_path, spectate=False, match_id="match-x",
                   instance="match-x-i1")
    b = _write_run(tmp_path, spectate=False, match_id="match-x",
                   instance="match-x-i2")  # restart of the same match
    sa, _ = export(a)
    sb, _ = export(b)
    assert {s["split_group"] for s in sa} == {"match-x"}
    assert {s["split_group"] for s in sb} == {"match-x"}
    # instances remain distinguishable
    assert sa[0]["game_instance_id"] != sb[0]["game_instance_id"]


def test_actor_features_carry_no_prompts_or_future_payloads(tmp_path):
    run_dir = _write_run(tmp_path, spectate=False, with_ledger=True,
                         with_boundaries=True)
    samples, _ = export(run_dir)
    text = json.dumps(samples)
    for banned in ("\"prompt\"", "\"system\"", "\"messages\"", "\"request\"",
                   "\"response\"", "\"content\""):
        assert banned not in text


def test_cli_roundtrip(tmp_path):
    run_dir = _write_run(tmp_path, spectate=True)
    out = tmp_path / "out.jsonl"
    proc = subprocess.run(
        [sys.executable, "-m", "civ_arena.research.export_dataset",
         str(run_dir), "-o", str(out)],
        cwd=REPO, capture_output=True, text=True, timeout=120.0,
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = out.read_text().splitlines()
    assert len(lines) == 2
    assert (tmp_path / "out.jsonl.manifest.json").exists()


# -- CAP-R1 #2/#3/#11/#12: joins, keys, cursors, manifest ----------------------


def test_production_nested_boundary_audits_join(tmp_path):
    """#2: production decision audits route through strategy_audit_event,
    which nests the payload fields inside strategy_payload_json — the
    exporter must join BOTH that shape and the flat one."""
    run_dir = _write_run(tmp_path, spectate=False, with_ledger=True)
    lines = (run_dir / "events.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in lines]
    rebuilt = []
    for i, row in enumerate(rows):
        if i == 1:  # first turn position becomes the production boundary
            row = {**row, "kind": "HEARTBEAT", "audit": "decision_boundary",
                   "strategy_payload_json": json.dumps({
                       "decision_id": "d1", "directive_id": "dir1",
                       "provider_requests": 1, "source": "model"},
                       sort_keys=True)}
        rebuilt.append(row)
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rebuilt) + "\n")
    samples, _ = export(run_dir)
    by_seg = {s["segment_id"]: s for s in samples}
    first = by_seg["turn:1"]
    assert first["decision_id"] == "d1"
    assert first["directive_id"] == "dir1"
    assert first["provider_requests"] == 1
    assert first["decision_source"] == "model"
    assert first["request_costs"]["tokens_in"] == 5  # the ledger row joined


def test_hotseat_agents_on_one_turn_export_distinct_samples(tmp_path):
    """#3: a hotseat engine turn carries TWO agents' decisions — each
    exports as its OWN sample with only its observations/actions/receipts
    and its OWN boundary; never one merged cross-agent sample."""
    run_dir = tmp_path / "hotseat"
    run_dir.mkdir()
    rows = []
    seq = 0

    def add(kind, **fields):
        nonlocal seq
        rows.append({"schema": 1, "seq": seq, "kind": kind,
                     "ts": "t", "match_id": "hs", "game_instance_id": "hs-i1",
                     "turn": fields.pop("turn", 0), "phase_player_id": -1,
                     **fields})
        seq += 1

    add("MATCH_START", config={"agents": [["a0", 0, "llm"], ["a1", 1, "llm"]]})
    for aid in ("a0", "a1"):
        add("HEARTBEAT", turn=1, audit="decision_boundary", agent_id=aid,
            decision_id=f"d-{aid}", directive_id=f"dir-{aid}",
            provider_requests=1, source="model")
        add("TOOL_CALL", turn=1, player_id=0 if aid == "a0" else 1,
            agent_id=aid, tool="get_units", args={}, args_digest="x")
        add("TOOL_RESULT", turn=1, player_id=0 if aid == "a0" else 1,
            agent_id=aid, tool="get_units", status="accepted",
            observed={"n": 1})
        add("TOOL_CALL", turn=1, player_id=0 if aid == "a0" else 1,
            agent_id=aid, tool="move_unit", args={"unit_id": aid},
            args_digest="y")
        add("TOOL_RESULT", turn=1, player_id=0 if aid == "a0" else 1,
            agent_id=aid, tool="move_unit", status="accepted",
            mutations=[{"entity_id": f"u{aid}"}], receipts=[{"k": 1}])
    add("MATCH_END", turn=1, summary={"match_id": "hs",
                                      "game_instance_id": "hs-i1",
                                      "final_turn": 1, "aborted": None,
                                      "phase": "dispatch-hotseat"})
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n")
    (run_dir / "summary.json").write_text(json.dumps(
        {"match_id": "hs", "game_instance_id": "hs-i1", "final_turn": 1,
         "aborted": None, "phase": "dispatch-hotseat"}, sort_keys=True))
    samples, _ = export(run_dir)
    assert len(samples) == 2, "two agents on one turn = two samples"
    segs = {s["segment_id"] for s in samples}
    assert segs == {"turn:1:a0", "turn:1:a1"}
    by_agent = {s["agent_id"]: s for s in samples}
    for aid in ("a0", "a1"):
        s = by_agent[aid]
        assert s["decision_id"] == f"d-{aid}"
        assert s["directive_id"] == f"dir-{aid}"
        assert len(s["observation_refs"]) == 1
        assert len(s["requested_action_refs"]) == 1
        # only THIS agent's refs — the sibling's must not leak in
        events = rows
        for ref in s["observation_refs"] + s["requested_action_refs"]:
            assert events[ref["seq"]]["agent_id"] == aid


def test_manifest_digests_every_consumed_input(tmp_path):
    """#12: samples depend on summary.json AND llm_costs.jsonl — changing a
    ledger token count must change BOTH the sample bytes and a manifest
    digest (events digest alone cannot cover it)."""
    run_dir = _write_run(tmp_path, spectate=False, with_ledger=True,
                         with_boundaries=True)
    s1, m1 = export(run_dir)
    assert "summary_sha256" in m1["source"]
    assert "llm_costs_sha256" in m1["source"]
    # mutate the ledger's token count
    ledger = (run_dir / "llm_costs.jsonl").read_text().replace(
        '"input_tokens": 5', '"input_tokens": 50')
    (run_dir / "llm_costs.jsonl").write_text(ledger)
    s2, m2 = export(run_dir)
    assert s1 != s2  # the sample carried the cost — it moved
    assert m1["source"]["events_sha256"] == m2["source"]["events_sha256"]
    assert m1["source"]["llm_costs_sha256"] != m2["source"]["llm_costs_sha256"]


def test_intervals_carry_source_cursors_and_gap_channels(tmp_path):
    """#11: post-CAP-01 runs carry provenance cursors and TWO gap channels
    (ring wraps + engine jumps + gap-inferred rounds) — the exporter
    reports the actual values instead of hard-coded nulls/one count."""
    run_dir = _write_run(tmp_path, spectate=True)
    lines = (run_dir / "events.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in lines]
    rebuilt = []
    for row in rows:
        if row["kind"] == "HUMAN_TURN_START":
            row = {**row, "source_cursor": "5|HOOK_ENTER|0",
                   **({"boundary": "gap_inferred"}
                      if row["turn"] == 2 else {})}
        elif row["kind"] == "HUMAN_TURN_END":
            row = {**row, "source_cursor": "9|HOOK_DEACT|0"}
        elif row["kind"] == "MATCH_END":
            gap = {"schema": 1, "seq": 999, "kind": "HEARTBEAT",
                   "ts": "t", "match_id": row["match_id"],
                   "game_instance_id": row["game_instance_id"], "turn": 2,
                   "phase_player_id": 0, "player_id": None, "agent_id": None,
                   "visibility_scope": "spectator", "audit": "capture_gap",
                   "missing_turns": [2]}
            rebuilt.append(gap)
        rebuilt.append(row)
    for i, row in enumerate(rebuilt):
        row["seq"] = i
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rebuilt) + "\n")
    samples, _ = export(run_dir)
    first = next(s for s in samples if s["segment_id"] == "seq:1")
    assert first["source_cursor_start"] == "5|HOOK_ENTER|0"
    assert first["source_cursor_end"] == "9|HOOK_DEACT|0"
    assert first["engine_jump_gaps"] == 1
    assert first["gap_inferred_rounds"] == 1
    second = next(s for s in samples if s["interval_turn_start"] == 2)
    assert "gap_inferred_round" in second["quality_flags"]


def test_turn_with_replacement_decision_exports_per_decision_segments(tmp_path):
    """CAP-R1 #4 + R-02 tail (recheck 2026-09-08): the economy refresh
    emits a SECOND decision_boundary on the same turn. The turn exports
    one sample PER boundary (genuine decision grain): the operative
    (last) decision keeps the stable segment id, the superseded one
    carries an @decision_id suffix, and each sample joins only ITS OWN
    decision's cost rows — the turn-start decision's costs never vanish
    from the export."""
    run_dir = _write_run(tmp_path, spectate=False)
    lines = (run_dir / "events.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in lines]
    rebuilt = []
    for i, row in enumerate(rows):
        if i == 1:
            rebuilt.append({**row, "kind": "HEARTBEAT",
                            "audit": "decision_boundary",
                            "decision_id": "d-start", "directive_id": "dir1",
                            "provider_requests": 1, "source": "model"})
        if i == 2:
            rebuilt.append({**row, "kind": "HEARTBEAT",
                            "audit": "decision_boundary",
                            "decision_id": "d-refresh", "directive_id": "dir2",
                            "provider_requests": 1, "source": "model",
                            "phase": "economy_refresh"})
        rebuilt.append(row)
    for i, row in enumerate(rebuilt):
        row["seq"] = i
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rebuilt) + "\n")
    ledger = [
        {"ts": "t", "agent_id": "a0", "player_id": 0,
         "request_kind": "generation", "attempt": 0, "status_code": 200,
         "latency_ms": 10, "model": "m", "payload_hash": "h1",
         "input_tokens": 5, "output_tokens": 1,
         "decision_id": "d-start", "logical_request_id": "lr1",
         "request_set_key": "h1:generation"},
        {"ts": "t", "agent_id": "a0", "player_id": 0,
         "request_kind": "generation", "attempt": 0, "status_code": 200,
         "latency_ms": 20, "model": "m", "payload_hash": "h2",
         "input_tokens": 7, "output_tokens": 2,
         "decision_id": "d-refresh", "logical_request_id": "lr2",
         "request_set_key": "h2:generation"},
    ]
    (run_dir / "llm_costs.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in ledger) + "\n")
    samples, _ = export(run_dir)
    by_seg = {s["segment_id"]: s for s in samples}
    # R-02 tail (recheck 2026-09-08): GENUINE decision grain — the turn
    # exports one sample PER boundary. The operative (last) decision
    # keeps the stable segment id; the superseded turn-start decision
    # carries an @decision_id suffix. Costs and the request count are
    # per decision (the refresh's second provider call belongs to the
    # refresh, not to the directive it replaced).
    first = by_seg["turn:1"]
    assert first["decision_id"] == "d-refresh"   # operative = last
    assert first["directive_id"] == "dir2"
    assert first["provider_requests"] == 1        # its OWN boundary only
    assert first["decisions_in_turn"] == 2
    assert "boundary_superseded_within_turn" in first["quality_flags"]
    assert first["request_costs"]["attempts"] == 1   # only lr2 joins
    assert first["request_costs"]["tokens_in"] == 7
    superseded = by_seg["turn:1@d-start~1"]  # d-start's boundary seq = 1
    assert superseded["decision_id"] == "d-start"
    assert superseded["directive_id"] == "dir1"
    assert superseded["provider_requests"] == 1
    assert "boundary_superseded_within_turn" in superseded["quality_flags"]
    assert superseded["request_costs"]["attempts"] == 1   # only lr1 joins
    assert superseded["request_costs"]["tokens_in"] == 5
    second = by_seg["turn:2"]
    assert "boundary_superseded_within_turn" not in second["quality_flags"]


# -- recheck 2026-09-08 regressions (R-03/R-04) -------------------------------


def test_unknown_cost_attempts_flagged_never_folded_as_zero(tmp_path):
    """R-04: one recorded attempt (100/10/5ms) plus one UNKNOWN attempt
    must export as a KNOWN subtotal with the unknown counted separately
    and a costs_partial flag — never as an unqualified total. Genuine
    zeros stay zeros (a recorded 0 is data; an absent field is not)."""
    run_dir = _write_run(tmp_path, spectate=False, with_ledger=True,
                         with_boundaries=True)
    ledger = [  # d1 rows: one known, one unknown; d2 row: genuine zeros
        {"ts": "t", "agent_id": "a0", "player_id": 0,
         "request_kind": "generation", "attempt": 0, "status_code": 200,
         "latency_ms": 5, "model": "m", "payload_hash": "h1",
         "input_tokens": 100, "output_tokens": 10,
         "decision_id": "d1", "logical_request_id": "lr-known",
         "request_set_key": "h1:generation"},
        {"ts": "t", "agent_id": "a0", "player_id": 0,
         "request_kind": "generation", "attempt": 0, "status_code": 200,
         "latency_ms": None, "model": "m", "payload_hash": "h2",
         "input_tokens": None, "output_tokens": None,
         "decision_id": "d1", "logical_request_id": "lr-unknown",
         "request_set_key": "h2:generation"},
        {"ts": "t", "agent_id": "a0", "player_id": 0,
         "request_kind": "generation", "attempt": 0, "status_code": 200,
         "latency_ms": 0, "model": "m", "payload_hash": "h3",
         "input_tokens": 0, "output_tokens": 0,
         "decision_id": "d2", "logical_request_id": "lr-zero",
         "request_set_key": "h3:generation"},
    ]
    (run_dir / "llm_costs.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in ledger) + "\n")
    samples, _ = export(run_dir)
    by_turn = {s["segment_id"]: s for s in samples}
    partial = by_turn["turn:1"]["request_costs"]
    assert partial["attempts"] == 2
    assert partial["attempts_with_unknown_usage"] == 1
    assert partial["attempts_with_unknown_latency"] == 1
    assert partial["tokens_in"] == 100 and partial["tokens_out"] == 10
    assert partial["latency_ms_sum"] == 5
    assert partial["usage_complete"] is False
    assert "costs_partial" in by_turn["turn:1"]["quality_flags"]
    # the unknown row's ids ride along — the subtotal is auditable
    assert partial["logical_request_ids"] == ["lr-known", "lr-unknown"]
    genuine = by_turn["turn:2"]["request_costs"]
    assert genuine["tokens_in"] == 0 and genuine["tokens_out"] == 0
    assert genuine["latency_ms_sum"] == 0
    assert genuine["usage_complete"] is True
    assert "costs_partial" not in by_turn["turn:2"]["quality_flags"]


def test_missing_cost_attempts_flagged_against_expected(tmp_path):
    """R-04 tail: when the boundary declares more provider requests than
    the ledger recorded, the shortfall is flagged — a sink failure or a
    cancelled retry must not silently shrink the aggregate."""
    run_dir = _write_run(tmp_path, spectate=False, with_ledger=True,
                         with_boundaries=True)
    # fixture: turn 1 boundary declares provider_requests=1; keep ZERO
    # ledger rows for d1 so recorded < expected. Codex r1 finding 4:
    # the empty-ledger shape EMITS a zero-attempt request_costs with
    # the missing flag (never a silent null that hides the sink failure)
    (run_dir / "llm_costs.jsonl").write_text("")
    samples, _ = export(run_dir)
    first = next(s for s in samples if s["segment_id"] == "turn:1")
    assert first["request_costs"]["attempts"] == 0
    assert "costs_attempts_missing" in first["quality_flags"]
    # costs_attempts_missing fires on the row-level comparison instead
    rows = [{"ts": "t", "agent_id": "a0", "player_id": 0,
             "request_kind": "generation", "attempt": 0, "status_code": 200,
             "latency_ms": 5, "model": "m", "payload_hash": "h1",
             "input_tokens": 8, "output_tokens": 2,
             "decision_id": "d1", "logical_request_id": "lr1",
             "request_set_key": "h1:generation"}]
    (run_dir / "llm_costs.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n")
    # turn 1 expects 1, has 1: clean; turn 2 boundary declares 0 but the
    # fixture's d2 has no rows — expected 0 means nothing missing.
    samples, _ = export(run_dir)
    by_turn = {s["segment_id"]: s for s in samples}
    assert "costs_attempts_missing" not in by_turn["turn:1"]["quality_flags"]
    # now manufacture the shortfall: two declared, one recorded
    events = [json.loads(line) for line
              in (run_dir / "events.jsonl").read_text().splitlines()]
    for e in events:
        if e.get("audit") == "decision_boundary" and e.get("turn") == 1:
            e["provider_requests"] = 2
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e, sort_keys=True) for e in events) + "\n")
    samples, _ = export(run_dir)
    by_turn = {s["segment_id"]: s for s in samples}
    first = by_turn["turn:1"]
    assert first["request_costs"]["expected_attempts"] == 2
    assert first["request_costs"]["attempts"] == 1
    assert "costs_attempts_missing" in first["quality_flags"]


def test_write_refuses_to_overwrite_source_evidence(tmp_path):
    """R-03: the output path (and its manifest sibling) may never land on
    a consumed artifact of the run the manifest names — direct path,
    `..` spelling, or symlink alias all refuse, and the source bytes
    survive untouched."""
    run_dir = _write_run(tmp_path, spectate=False, with_ledger=True,
                         with_boundaries=True)
    samples, manifest = export(run_dir)
    original = (run_dir / "events.jsonl").read_bytes()
    for target in (run_dir / "events.jsonl",
                   run_dir / "summary.json",
                   run_dir / "llm_costs.jsonl",
                   run_dir,  # the run dir itself would shadow the sources
                   # alias spellings resolve to the same file
                   run_dir / ".." / run_dir.name / "summary.json"):
        with pytest.raises(ValueError, match="refusing to overwrite"):
            write(samples, manifest, target)
    # a symlink alias pointing at the source refuses too
    alias = tmp_path / "alias.jsonl"
    alias.symlink_to(run_dir / "events.jsonl")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        write(samples, manifest, alias)
    assert (run_dir / "events.jsonl").read_bytes() == original
    assert (run_dir / "summary.json").exists()
    # a legitimate out-of-run target still writes fine
    write(samples, manifest, tmp_path / "out" / "dataset.jsonl")
    assert (tmp_path / "out" / "dataset.jsonl").exists()
    assert (tmp_path / "out" / "dataset.jsonl.manifest.json").exists()


def test_driven_run_without_summary_refuses_controlled_export(tmp_path):
    """R-03: summary.json carries the outcome record. Without it a driven
    run's horizon/censoring are unknown — spectator data must not fall
    into the controlled class merely because MATCH_START exists."""
    run_dir = _write_run(tmp_path, spectate=False, with_ledger=True,
                         with_boundaries=True)
    (run_dir / "summary.json").unlink()
    with pytest.raises(ValueError, match="no summary.json"):
        export(run_dir)


# -- Codex r1 (capr1-integration) regressions ----------------------------------


def test_write_refuses_hardlinked_source_alias(tmp_path):
    """Codex r1 finding 1: resolution cannot see hardlinks — a target
    that hardlinks events.jsonl shares its inode, and open(...,'w')
    would truncate the source. samefile() must catch it."""
    run_dir = _write_run(tmp_path, spectate=False, with_ledger=True,
                         with_boundaries=True)
    samples, manifest = export(run_dir)
    original = (run_dir / "events.jsonl").read_bytes()
    hard = tmp_path / "hard-alias.jsonl"
    os.link(run_dir / "events.jsonl", hard)
    with pytest.raises(ValueError, match="refusing to overwrite"):
        write(samples, manifest, hard)
    assert (run_dir / "events.jsonl").read_bytes() == original


def test_first_decision_window_keeps_its_input_observations(tmp_path):
    """Codex r1 finding 3: production emits the boundary AFTER refresh
    + decide — the decision's INPUT observations precede it. The first
    decision's window must start at the turn, not at its boundary, or
    the observations that produced the decision are exported nowhere."""
    run_dir = _write_run(tmp_path, spectate=False)  # NO fixture boundary
    lines = (run_dir / "events.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in lines]
    rebuilt = []
    for i, row in enumerate(rows):
        if i == 3:  # AFTER turn 1's get_units pair, before move_unit
            rebuilt.append({**row, "kind": "HEARTBEAT",
                            "audit": "decision_boundary",
                            "decision_id": "d-late", "directive_id": "dir1",
                            "provider_requests": 1, "source": "model"})
        rebuilt.append(row)
    for i, row in enumerate(rebuilt):
        row["seq"] = i
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rebuilt) + "\n")
    samples, _ = export(run_dir)
    first = next(s for s in samples if s["segment_id"] == "turn:1")
    # the get_units TOOL_RESULT (seq 2) precedes the boundary (seq 3)
    # but IS the decision's input — it must ride the sample
    assert any(ref["seq"] == 2 for ref in first["observation_refs"])


def test_zero_recorded_attempts_with_expected_requests_flag_missing(tmp_path):
    """Codex r1 finding 4: a boundary declaring provider_requests with
    an EMPTY ledger is the sink-failure shape — the missing-attempt
    flag must fire even with zero joined rows."""
    run_dir = _write_run(tmp_path, spectate=False, with_ledger=False,
                         with_boundaries=True)
    (run_dir / "llm_costs.jsonl").write_text("")  # exists, but empty
    samples, _ = export(run_dir)
    first = next(s for s in samples if s["segment_id"] == "turn:1")
    assert first["request_costs"] is not None
    assert first["request_costs"]["attempts"] == 0
    assert first["request_costs"]["expected_attempts"] == 1
    assert "costs_attempts_missing" in first["quality_flags"]


def test_empty_decision_window_keeps_boundary_player_identity(tmp_path):
    """Codex r1 finding 5: two consecutive boundaries make the first
    window empty — the player identity must still come from the
    boundary, never degrade to None."""
    run_dir = _write_run(tmp_path, spectate=False, with_boundaries=True)
    lines = (run_dir / "events.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in lines]
    rebuilt = []
    for i, row in enumerate(rows):
        if i == 1:
            rebuilt.append({**row, "kind": "HEARTBEAT", "player_id": 0,
                            "audit": "decision_boundary",
                            "decision_id": "d-first", "directive_id": "dirA",
                            "provider_requests": 1, "source": "model"})
        if i == 2:
            rebuilt.append({**row, "kind": "HEARTBEAT", "player_id": 0,
                            "audit": "decision_boundary",
                            "decision_id": "d-second", "directive_id": "dirB",
                            "provider_requests": 1, "source": "model"})
        rebuilt.append(row)
    for i, row in enumerate(rebuilt):
        row["seq"] = i
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rebuilt) + "\n")
    samples, _ = export(run_dir)
    superseded = next(s for s in samples
                      if s["segment_id"].startswith("turn:1@d-first"))
    assert superseded["player_id"] == 0


def test_reused_decision_id_joins_costs_once_and_flags_ambiguity(tmp_path):
    """Codex r1 finding 6: two boundaries sharing one decision_id cannot
    attribute ledger rows to an occurrence — rows join ONLY the
    operative sample; every sibling carrying the id is flagged."""
    run_dir = _write_run(tmp_path, spectate=False)
    lines = (run_dir / "events.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in lines]
    rebuilt = []
    for i, row in enumerate(rows):
        if i in (1, 2):
            rebuilt.append({**row, "kind": "HEARTBEAT",
                            "audit": "decision_boundary",
                            "decision_id": "d-same", "directive_id": "dirX",
                            "provider_requests": 1, "source": "model"})
        rebuilt.append(row)
    for i, row in enumerate(rebuilt):
        row["seq"] = i
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rebuilt) + "\n")
    ledger = [
        {"ts": "t", "agent_id": "a0", "player_id": 0,
         "request_kind": "generation", "attempt": 0, "status_code": 200,
         "latency_ms": 5, "model": "m", "payload_hash": "h1",
         "input_tokens": 9, "output_tokens": 1,
         "decision_id": "d-same", "logical_request_id": "lr1",
         "request_set_key": "h1:generation"},
    ]
    (run_dir / "llm_costs.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in ledger) + "\n")
    samples, _ = export(run_dir)
    same_id = [s for s in samples if s["decision_id"] == "d-same"]
    assert len(same_id) == 2
    assert len({s["segment_id"] for s in same_id}) == 2
    joined = [s for s in same_id
              if s["request_costs"]
              and s["request_costs"]["attempts"] == 1]
    assert len(joined) == 1  # exactly ONE sample carries the ROWS
    assert joined[0]["segment_id"] == "turn:1"  # the operative one
    # the superseded sibling emits a zero-attempt cost object with the
    # missing flag — never the shared rows
    superseded = next(s for s in same_id
                      if s["segment_id"] != "turn:1")
    assert superseded["request_costs"]["attempts"] == 0
    for s in same_id:
        assert "decision_id_reused_costs_ambiguous" in s["quality_flags"]


# -- Codex r2 (capr1-integration) regressions ----------------------------------


def test_execution_between_boundaries_belongs_to_its_own_decision(tmp_path):
    """Codex r2 finding 1 (regression corrected per r3 finding 2): the
    two boundaries must sit in the SAME (turn, agent) — across turns
    ``_agents()`` already separates them, so a cross-turn fixture passes
    even with the pre-fix wide window and proves nothing.

    Same-turn A -> move/receipt -> B -> move/receipt: each move is the
    EXECUTION of the boundary that commanded it. Observations still span
    the wide window; actions and receipts are bounded by (own boundary,
    next boundary)."""
    run_dir = _write_run(tmp_path, spectate=False)  # no fixture boundary
    lines = (run_dir / "events.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in lines]

    def _boundary(row, did):
        return {**row, "kind": "HEARTBEAT", "audit": "decision_boundary",
                "decision_id": did, "directive_id": "dir",
                "provider_requests": 1, "source": "model"}

    rebuilt = []
    for i, row in enumerate(rows):
        if i == 3:  # before turn-1 move_unit
            rebuilt.append(_boundary(row, "d-a"))
        rebuilt.append(row)
        if i == 4:  # after turn-1 move receipt: replacement + its own pair
            rebuilt.append(_boundary(row, "d-b"))
            rebuilt.append(dict(rows[3]))
            rebuilt.append(dict(rows[4]))
    for i, row in enumerate(rebuilt):
        row["seq"] = i
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rebuilt) + "\n")
    samples, _ = export(run_dir)
    by_id = {s["decision_id"]: s for s in samples}
    # rebuilt: MS(0), TC gu(1), TR gu(2), bA(3), TC mv(4), TR mv(5),
    #          bB(6), TC mv(7), TR mv(8), then turn 2 — ALL of 1..8 are
    # turn 1 / agent a0, so only the window logic can separate them.
    a, b = by_id["d-a"], by_id["d-b"]
    assert a["segment_id"] != b["segment_id"]
    assert [r["seq"] for r in a["requested_action_refs"]] == [4]
    assert [r["seq"] for r in a["execution_receipt_refs"]] == [5]
    assert [r["seq"] for r in b["requested_action_refs"]] == [7]
    assert [r["seq"] for r in b["execution_receipt_refs"]] == [8]
    # exclusive ownership shows up in the derived count too
    assert a["procedural_tool_calls"] == 1
    assert b["procedural_tool_calls"] == 1
    # the wide observation window still feeds A its pre-boundary inputs
    assert 2 in [r["seq"] for r in a["observation_refs"]]


def test_cross_turn_reused_id_joins_its_rows_exactly_once(tmp_path):
    """Codex r3 finding 1: the same decision_id on boundaries in turn 1
    AND turn 2 is one ambiguous identity, not two. Its ledger rows may
    join exactly ONE sample (the run-wide last occurrence) — per-segment
    recipient resolution double-counted them — and every occurrence
    carries the ambiguity flag."""
    run_dir = _write_run(tmp_path, spectate=False)
    rebuilt = _cross_turn_reuse_events(run_dir)
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rebuilt) + "\n")
    (run_dir / "llm_costs.jsonl").write_text(json.dumps({
        "ts": "t", "agent_id": "a0", "player_id": 0,
        "request_kind": "generation", "attempt": 0, "status_code": 200,
        "latency_ms": 5, "model": "m", "payload_hash": "h1",
        "input_tokens": 9, "output_tokens": 1,
        "decision_id": "d-x", "logical_request_id": "lr-x",
        "request_set_key": "h1:generation"}, sort_keys=True) + "\n")
    samples, _ = export(run_dir)
    reused = [s for s in samples if s["decision_id"] == "d-x"]
    assert len(reused) == 2  # one per turn
    assert {s["segment_id"] for s in reused} == {"turn:1", "turn:2"}
    # the row is counted ONCE across the whole run, not once per turn
    assert sum(s["request_costs"]["attempts"] for s in reused) == 1
    assert sum(s["request_costs"]["tokens_in"] or 0 for s in reused) == 9
    joined = [s for s in reused if s["request_costs"]["attempts"] == 1]
    assert len(joined) == 1
    assert joined[0]["segment_id"] == "turn:2"  # run-wide last occurrence
    for s in reused:  # the ambiguity is visible in BOTH turns
        assert "decision_id_reused_costs_ambiguous" in s["quality_flags"]


def test_reused_id_with_following_decision_keeps_costs(tmp_path):
    """Codex r2 finding 2: A, A, B — the reused id's rows join the LAST
    A (its final occurrence), not 'no sample'."""
    run_dir = _write_run(tmp_path, spectate=False)
    lines = (run_dir / "events.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in lines]
    rebuilt = []
    for i, row in enumerate(rows):
        if i == 1:
            rebuilt.append({**row, "kind": "HEARTBEAT",
                            "audit": "decision_boundary",
                            "decision_id": "d-a", "directive_id": "dir1",
                            "provider_requests": 1, "source": "model"})
        if i == 2:
            rebuilt.append({**row, "kind": "HEARTBEAT",
                            "audit": "decision_boundary",
                            "decision_id": "d-a", "directive_id": "dir2",
                            "provider_requests": 0, "source": "model"})
        if i == 3:
            rebuilt.append({**row, "kind": "HEARTBEAT",
                            "audit": "decision_boundary",
                            "decision_id": "d-b", "directive_id": "dir3",
                            "provider_requests": 1, "source": "model"})
        rebuilt.append(row)
    for i, row in enumerate(rebuilt):
        row["seq"] = i
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rebuilt) + "\n")
    ledger = [
        {"ts": "t", "agent_id": "a0", "player_id": 0,
         "request_kind": "generation", "attempt": 0, "status_code": 200,
         "latency_ms": 5, "model": "m", "payload_hash": "h1",
         "input_tokens": 9, "output_tokens": 1,
         "decision_id": "d-a", "logical_request_id": "lr-a",
         "request_set_key": "h1:generation"},
        {"ts": "t", "agent_id": "a0", "player_id": 0,
         "request_kind": "generation", "attempt": 0, "status_code": 200,
         "latency_ms": 4, "model": "m", "payload_hash": "h2",
         "input_tokens": 3, "output_tokens": 1,
         "decision_id": "d-b", "logical_request_id": "lr-b",
         "request_set_key": "h2:generation"},
    ]
    (run_dir / "llm_costs.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in ledger) + "\n")
    samples, _ = export(run_dir)
    a_samples = [s for s in samples if s["decision_id"] == "d-a"]
    assert len(a_samples) == 2
    joined = [s for s in a_samples
              if s["request_costs"] and s["request_costs"]["attempts"] == 1]
    assert len(joined) == 1  # the LAST occurrence carries d-a's rows
    b = next(s for s in samples if s["decision_id"] == "d-b")
    assert b["request_costs"]["attempts"] == 1
    assert b["request_costs"]["tokens_in"] == 3


def _cross_turn_reuse_events(run_dir: Path) -> list[dict]:
    """Shared fixture body: the same decision_id on a boundary in turn 1
    AND turn 2 (rebuilt seqs: MS 0, bX 1, ... bX 6, ...)."""
    rows = [json.loads(line) for line
            in (run_dir / "events.jsonl").read_text().splitlines()]
    rebuilt = []
    for i, row in enumerate(rows):
        if i in (1, 5):  # before each turn's first tool call
            rebuilt.append({**row, "kind": "HEARTBEAT",
                            "audit": "decision_boundary",
                            "decision_id": "d-x", "directive_id": "dir",
                            "provider_requests": 1, "source": "model"})
        rebuilt.append(row)
    for i, row in enumerate(rebuilt):
        row["seq"] = i
    return rebuilt


def test_integral_float_seq_still_ranks_as_its_integer(tmp_path):
    """Codex r4 finding 2: ``seq: 6.0`` satisfies the contiguity check
    (6.0 == 6) but is not an ``int``, so before normalization every
    isinstance-guarded ordering path ranked that boundary as unsequenced
    and the EARLIER occurrence collected the reused id's ledger row."""
    run_dir = _write_run(tmp_path, spectate=False)
    rebuilt = _cross_turn_reuse_events(run_dir)
    assert rebuilt[6]["audit"] == "decision_boundary"  # turn-2 boundary
    rebuilt[6]["seq"] = 6.0  # the integral float, written as "6.0"
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rebuilt) + "\n")
    assert '"seq": 6.0' in (run_dir / "events.jsonl").read_text()
    (run_dir / "llm_costs.jsonl").write_text(json.dumps({
        "ts": "t", "agent_id": "a0", "player_id": 0,
        "request_kind": "generation", "attempt": 0, "status_code": 200,
        "latency_ms": 5, "model": "m", "payload_hash": "h1",
        "input_tokens": 9, "output_tokens": 1,
        "decision_id": "d-x", "logical_request_id": "lr-x",
        "request_set_key": "h1:generation"}, sort_keys=True) + "\n")
    samples, _ = export(run_dir)
    reused = [s for s in samples if s["decision_id"] == "d-x"]
    # exactly-once survived the float spelling even before the fix; what
    # broke was WHICH occurrence collected the row
    assert sum(s["request_costs"]["attempts"] for s in reused) == 1
    joined = [s for s in reused if s["request_costs"]["attempts"] == 1]
    assert joined[0]["segment_id"] == "turn:2"  # the last occurrence
    # and the normalization is what did it: the BOUNDARY's own seq comes
    # back as a real int (Codex r5 — an observation-ref assertion would
    # not have proven this, since the changed row is not a ref)
    from civ_arena.research.export_dataset import _parse_events
    parsed = _parse_events((run_dir / "events.jsonl").read_bytes())
    assert parsed[6]["audit"] == "decision_boundary"
    assert isinstance(parsed[6]["seq"], int)


def test_usage_complete_requires_covering_every_expected_attempt(tmp_path):
    """Codex r4 finding 1: usage_complete asserts the known subtotal
    covers the WHOLE decision. A decision that expected two attempts and
    recorded one — or recorded NONE — is not complete, however
    well-formed the rows present are."""
    run_dir = _write_run(tmp_path, spectate=False, with_boundaries=True)
    rows = [json.loads(line) for line
            in (run_dir / "events.jsonl").read_text().splitlines()]
    for row in rows:  # turn 1 expects TWO provider requests
        if row.get("audit") == "decision_boundary" and row["turn"] == 1:
            row["provider_requests"] = 2
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n")
    (run_dir / "llm_costs.jsonl").write_text(json.dumps({
        "ts": "t", "agent_id": "a0", "player_id": 0,
        "request_kind": "generation", "attempt": 0, "status_code": 200,
        "latency_ms": 5, "model": "m", "payload_hash": "h1",
        "input_tokens": 9, "output_tokens": 1,
        "decision_id": "d1", "logical_request_id": "lr1",
        "request_set_key": "h1:generation"}, sort_keys=True) + "\n")
    samples, _ = export(run_dir)
    by_turn = {s["segment_id"]: s for s in samples}
    short = by_turn["turn:1"]
    # one fully-recorded row, but the decision expected two attempts
    assert short["request_costs"]["attempts"] == 1
    assert short["request_costs"]["attempts_with_unknown_usage"] == 0
    assert short["request_costs"]["usage_complete"] is False
    assert "costs_attempts_missing" in short["quality_flags"]


def test_usage_complete_false_when_no_attempt_was_recorded(tmp_path):
    """The zero-recorded-attempts shape: a captured but empty ledger
    beside a boundary that declared a request must not publish
    usage_complete: true over an empty subtotal."""
    run_dir = _write_run(tmp_path, spectate=False, with_boundaries=True)
    (run_dir / "llm_costs.jsonl").write_text("")
    samples, _ = export(run_dir)
    first = next(s for s in samples if s["segment_id"] == "turn:1")
    assert first["request_costs"]["attempts"] == 0
    assert first["request_costs"]["usage_complete"] is False
    assert "costs_attempts_missing" in first["quality_flags"]


def _with_sink_failure(run_dir: Path, **audit_fields) -> None:
    """Splice one ledger_write_failed audit into a run's events."""
    rows = [json.loads(line) for line
            in (run_dir / "events.jsonl").read_text().splitlines()]
    rebuilt = []
    for i, row in enumerate(rows):
        if i == 1:
            rebuilt.append({**row, "kind": "HEARTBEAT",
                            "audit": "ledger_write_failed", **audit_fields})
        rebuilt.append(row)
    for i, row in enumerate(rebuilt):
        row["seq"] = i
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rebuilt) + "\n")


@pytest.mark.parametrize("sink", ["costs", "jsonl", None])
def test_usage_complete_false_when_a_cost_ledger_write_was_lost(tmp_path, sink):
    """A COST-ledger write failure means rows may be missing that nothing
    else can reveal, so the sample may not claim coverage. An UNKNOWN or
    absent sink name stays conservative for the same reason."""
    run_dir = _write_run(tmp_path, spectate=False, with_ledger=True,
                         with_boundaries=True)
    fields = {"agent": "a0"} if sink is None else {"agent": "a0", "sink": sink}
    _with_sink_failure(run_dir, **fields)
    samples, _ = export(run_dir)
    first = next(s for s in samples if s["segment_id"] == "turn:1")
    assert "ledger_write_gaps" in first["quality_flags"]
    assert first["request_costs"]["attempts"] == 1
    assert first["request_costs"]["usage_complete"] is False


@pytest.mark.parametrize("sink", ["spend", "wire"])
def test_unrelated_sink_failure_leaves_cost_coverage_intact(tmp_path, sink):
    """Codex r5 finding 1: the driver guards three sinks under ONE audit
    name, but only 'costs' writes llm_costs.jsonl. A failed spend ledger
    or opt-in wire transcript must not make every decision's costs
    incomplete when every expected row, token count and latency was
    recorded."""
    run_dir = _write_run(tmp_path, spectate=False, with_ledger=True,
                         with_boundaries=True)
    _with_sink_failure(run_dir, agent="a0", sink=sink)
    samples, _ = export(run_dir)
    first = next(s for s in samples if s["segment_id"] == "turn:1")
    # the run-level signal survives — something DID fail
    assert "ledger_write_gaps" in first["quality_flags"]
    # ...but this decision's cost accounting is genuinely complete
    assert first["request_costs"]["usage_complete"] is True
    assert "costs_partial" not in first["quality_flags"]


def test_cost_sink_failure_suppresses_only_the_failing_agent(tmp_path):
    """The audit carries the agent, so an attributable cost failure
    scopes to that agent's samples; another agent's accounting stands."""
    run_dir = _write_run(tmp_path, spectate=False, with_ledger=True,
                         with_boundaries=True)
    _with_sink_failure(run_dir, agent="a-other", sink="costs")
    samples, _ = export(run_dir)
    first = next(s for s in samples if s["segment_id"] == "turn:1")
    assert "ledger_write_gaps" in first["quality_flags"]
    assert first["request_costs"]["usage_complete"] is True


def test_unattributable_cost_failure_suppresses_the_run(tmp_path):
    """A cost failure with no agent cannot be scoped, so every sample
    loses its coverage claim — the conservative case."""
    run_dir = _write_run(tmp_path, spectate=False, with_ledger=True,
                         with_boundaries=True)
    _with_sink_failure(run_dir, sink="costs")
    samples, _ = export(run_dir)
    first = next(s for s in samples if s["segment_id"] == "turn:1")
    assert first["request_costs"]["usage_complete"] is False


def test_boolean_seq_is_refused(tmp_path):
    """Codex r5: bool is an int subclass, so `true` at index 1 would pass
    the contiguity check (True == 1) and then order as an integer."""
    run_dir = _write_run(tmp_path, spectate=False)
    rows = [json.loads(line) for line
            in (run_dir / "events.jsonl").read_text().splitlines()]
    rows[1]["seq"] = True
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n")
    with pytest.raises(ValueError, match="not an integer"):
        export(run_dir)


def test_captured_ledger_absence_never_resurrects_from_disk(tmp_path):
    """Codex r2 finding 3: a ledger CREATED between export()'s capture
    and segmentation must not join — the manifest never hashed it. The
    captured verdict (ledger_absent=True) wins over disk state."""
    from civ_arena.research.export_dataset import _controlled_decisions
    run_dir = _write_run(tmp_path, spectate=False, with_boundaries=True)
    events = [json.loads(line) for line
              in (run_dir / "events.jsonl").read_text().splitlines()]
    summary = json.loads((run_dir / "summary.json").read_text())
    # the ledger appears AFTER capture (it did not exist at export time)
    (run_dir / "llm_costs.jsonl").write_text(json.dumps({
        "ts": "t", "agent_id": "a0", "player_id": 0,
        "request_kind": "generation", "attempt": 0, "status_code": 200,
        "latency_ms": 5, "model": "m", "payload_hash": "h1",
        "input_tokens": 9, "output_tokens": 1,
        "decision_id": "d1", "logical_request_id": "lr1",
        "request_set_key": "h1:generation"}) + "\n")
    samples = _controlled_decisions(run_dir, events, summary,
                                    cost_rows=[], ledger_absent=True)
    first = next(s for s in samples if s["segment_id"] == "turn:1")
    assert "pre_ledger_run" in first["quality_flags"]
    assert first["request_costs"] is None
