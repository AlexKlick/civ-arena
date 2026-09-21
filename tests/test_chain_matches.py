"""Chain-runner classification and stop conditions (no game, no network)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _load(name: str) -> object:
    spec = importlib.util.spec_from_file_location(
        name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


chain = _load("chain_matches")


def _summary(tmp_path: Path, doc: dict | None) -> Path:
    path = tmp_path / "summary.json"
    if doc is not None:
        path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_no_summary_is_fail(tmp_path) -> None:
    verdict, row = chain.classify(tmp_path / "summary.json", rounds=30)
    assert verdict == "fail"
    assert row["reason"] == "no summary.json"


def test_clean_complete_is_pass(tmp_path) -> None:
    # the exact smoke-006 shape: clean, 30/30
    path = _summary(tmp_path, {"clean": True, "completed_rounds": 30,
                               "final_turn": 30, "elapsed_s": 870.2,
                               "violations_total": 0})
    verdict, row = chain.classify(path, rounds=30)
    assert verdict == "pass"
    assert row["completed_rounds"] == 30 and row["clean"] is True


def test_not_clean_is_partial_even_at_full_rounds(tmp_path) -> None:
    # smoke-005's shape: 30/30 rounds THEN a recovery failure — not a pass
    path = _summary(tmp_path, {"clean": False, "completed_rounds": 30,
                               "failure_stage": "recovery"})
    verdict, _ = chain.classify(path, rounds=30)
    assert verdict == "partial"


def test_partial_progress_is_partial_zero_rounds_is_fail(tmp_path) -> None:
    shape = {"clean": False, "completed_rounds": 23}
    assert chain.classify(_summary(tmp_path, shape), 30)[0] == "partial"
    zero = dict(shape, completed_rounds=0)
    assert chain.classify(_summary(tmp_path, zero), 30)[0] == "fail"


def test_unreadable_summary_is_fail(tmp_path) -> None:
    path = tmp_path / "summary.json"
    path.write_text("{not json", encoding="utf-8")
    verdict, row = chain.classify(path, rounds=30)
    assert verdict == "fail"
    assert "unreadable summary" in row["reason"]


def test_missing_fields_never_crash_the_classifier(tmp_path) -> None:
    path = _summary(tmp_path, {"clean": True})  # no completed_rounds key
    verdict, row = chain.classify(path, rounds=30)
    # zero completed rounds is a fail even if clean claims true — a
    # clean-with-no-rounds summary is contradictory, fail is the honest read
    assert verdict == "fail"
    assert row["completed_rounds"] == 0


def test_stop_conditions() -> None:
    assert chain.should_stop(0, 2, 0.0, 100.0) is None
    assert chain.should_stop(1, 2, 50.0, 100.0) is None
    assert "consecutive failures" in chain.should_stop(2, 2, 50.0, 100.0)
    assert "budget exhausted" in chain.should_stop(0, 2, 100.0, 100.0)
    # a pass resets the streak; budget still guards
    assert chain.should_stop(0, 2, 999.0, 100.0)


def test_python_syntax_and_stdlib_only() -> None:
    # the chain runs under SYSTEM python (no venv import). sys.modules here
    # is unusable as proof (the venv's conftest imports civ_arena); the
    # source scan plus a system-python --help smoke (run in the lane log)
    # are the evidence.
    source = (REPO / "scripts" / "chain_matches.py").read_text()
    assert "civ_arena" not in source
