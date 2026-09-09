"""Behavioral validity boundaries for the bounded diagnostic harness."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "strategy_benchmark", ROOT / "scripts/strategy_benchmark.py")
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


def config():
    return {"schema": 1, "purpose": "legacy-simulator-diagnostic", "turns": 2,
            "seeds": {"development": [700003], "held_out": [800003]},
            "pairs": [["expansionist", "turtler"]]}


def clean(item, diff):
    return {**copy.deepcopy(item), "errors": [], "completed_turns": item["turns"],
            "violations": 0, "rejections": 0, "value_differential_p0": diff}


@pytest.mark.parametrize("mutation", [
    lambda c: c["seeds"]["held_out"].append(700003),
    lambda c: c["pairs"].append(["turtler", "expansionist"]),
    lambda c: c.update(turns=41),
    lambda c: c["seeds"].update(development=list(range(13))),
    lambda c: c["pairs"].append(["llm", "turtler"]),
])
def test_config_rejects_overlap_duplicates_campaign_size_and_provider(mutation):
    cfg = config()
    mutation(cfg)
    with pytest.raises(ValueError):
        bench.schedule(cfg, "all")


def test_holdout_not_scheduled_by_development_and_seed_follows_variant():
    expected = bench.schedule(config(), "development")
    assert len(expected) == 2
    assert {r["seed"] for r in expected} == {700003}
    assert expected[0]["variants"] == expected[1]["variants"][::-1]
    assert bench.VARIANTS["mcts-b4"]["seed"] == bench.VARIANTS["mcts-b8"]["seed"]


@pytest.mark.parametrize("failure", [
    "missing", "duplicate", "dirty", "short", "wrong_seat", "extra"])
def test_invalid_inventory_blocks_all_descriptives(failure):
    expected = bench.schedule(config(), "development")
    rows = [clean(item, 10) for item in expected]
    if failure == "missing":
        rows.pop()
    elif failure == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    elif failure == "dirty":
        rows[1]["rejections"] = 1
    elif failure == "short":
        rows[1]["completed_turns"] = 1
    elif failure == "wrong_seat":
        rows[1]["variants"].reverse()
    else:
        rows.append({**rows[0], "match_id": "unscheduled"})
    report = bench.analyze(expected, rows)
    assert report["valid"] is False
    assert report["descriptives"] is None


def test_seat_bias_not_counted_as_variant_strength_and_ties_retained():
    cfg = config()
    cfg["seeds"]["development"] = [10, 20, 30]
    expected = bench.schedule(cfg, "development")
    rows = [clean(item, diff) for item, diff in zip(expected, [9, 4, 3, -8, 0, -2], strict=True)]
    d = next(iter(bench.analyze(expected, rows)["descriptives"].values()))
    assert d["seed_clusters"] == 3
    assert tuple(d[k] for k in ("a_wins_both", "b_wins_both", "seat_split",
                               "tie_in_cluster")) == (1, 0, 1, 1)
    assert (d["p0_wins"], d["p1_wins"], d["ties"]) == (3, 2, 1)
    assert d["a_diff_count"] == 6
    assert d["a_diff_sum"] == 18


async def test_real_pilot_audits_terminal_order_hashes_and_refuses_reentry(tmp_path):
    out = tmp_path / "pilot"
    cfg = config()
    cfg["pairs"] = [["mcts-b4", "mcts-b8"]]
    doc = await bench.run_pilot(cfg, out, "development")
    assert doc["audit"]["valid"] is True
    assert len(doc["rows"]) == 2
    assert doc["promotion_allowed"] is False
    first = doc["rows"][0]
    run_dir = out / first["match_id"]
    for name, digest in first["artifacts"].items():
        assert bench.sha(run_dir / name) == digest
    with pytest.raises(FileExistsError):
        await bench.run_pilot(config(), out, "development")
    records = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    records = [r for r in records if not (r["kind"] == "LEASE_RELEASE" and r["player_id"] == 1)]
    for seq, rec in enumerate(records):
        rec["seq"] = seq
    (run_dir / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    row = bench.audit_match(run_dir, bench.schedule(cfg, "development")[0])
    assert "incomplete or unordered LEASE_RELEASE" in row["errors"]


async def test_source_alias_output_refused_before_creation(tmp_path):
    link = tmp_path / "alias"
    link.symlink_to(ROOT / "scripts", target_is_directory=True)
    with pytest.raises(ValueError, match="output inside repository"):
        await bench.run_pilot(config(), link / "should-not-exist", "development")
    assert not (ROOT / "scripts/should-not-exist").exists()


async def test_runtime_failure_preserves_incomplete_report(tmp_path, monkeypatch):
    async def fail(*args):
        raise RuntimeError("injected failure")
    monkeypatch.setattr(bench, "run_match", fail)
    doc = await bench.run_pilot(config(), tmp_path / "failed", "development")
    assert doc["audit"]["valid"] is False
    assert doc["audit"]["descriptives"] is None
    assert any("injected failure" in e for e in doc["audit"]["errors"])
    assert (tmp_path / "failed/results.json").exists()
