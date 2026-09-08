"""Exporter v0: class separation, ref integrity, determinism, honest gaps.

The two sample classes never impersonate each other; every reference
resolves into the source events.jsonl; pre-ledger and pre-boundary runs
export with explicit quality flags instead of fabricated values; and the
same run dir exports byte-identical sample bytes twice.
"""

import json
import subprocess
import sys
from pathlib import Path

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
