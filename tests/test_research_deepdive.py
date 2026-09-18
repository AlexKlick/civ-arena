"""Per-bot deep-dive audit: facts, prompt, verdict validation (no network)."""

from __future__ import annotations

import json
from pathlib import Path

from civ_arena.research.deepdive import (
    audit_facts,
    audit_prompt,
    doctrine_docs_for,
    validate_verdict,
)

ROSTER = ["hyperwide_flood", "turtler",
          {"spec_id": "flood_pivot", "initial": "hyperwide_flood",
           "interval": 5,
           "triggers": [{"when": {"min_foreign_units_seen": 3},
                         "switch_to": "turtler"}]}]


def _row(match_id: str, scalars: list[int]) -> dict:
    labels = ["hyperwide_flood", "turtler", "flood_pivot", "turtler"]
    return {
        "match_id": match_id, "seed": 7, "rotation": 0, "turns": 30,
        "violations": 0, "aborted": None, "dirty": False,
        "seats": [{"player_id": pid, "agent_id": f"{labels[pid]}-{pid}",
                   "doctrine": labels[pid], "civ_name": f"C{pid}",
                   "scalar": scalars[pid]} for pid in range(4)],
    }


def _write_traj(root: Path, match_id: str, flood_turn: int | None,
                final: int = 30) -> None:
    run = root / match_id
    run.mkdir(parents=True, exist_ok=True)
    ticks = []
    for turn in range(1, final + 1):
        doctrine = None
        if turn >= 5:  # seat 2 is the pivot; switch persists from flood_turn
            doctrine = ("turtler" if flood_turn is not None
                        and turn >= flood_turn else "hyperwide_flood")
        ticks.append({"turn": turn, "scores": {
            "C0": {"cities": 3, "population": 9, "techs": 4, "units": 5,
                   "gold": 40, "scalar": 500},
            "C1": {"cities": 2, "population": 12, "techs": 3, "units": 6,
                   "gold": 90, "scalar": 300},
            "C2": {"cities": 2, "population": 8, "techs": 2, "units": 4,
                   "gold": 60, "scalar": 350},
            "C3": {"cities": 2, "population": 7, "techs": 2, "units": 3,
                   "gold": 30, "scalar": 200},
        }, "doctrines": {"0": None, "1": None, "2": doctrine, "3": None}})
    (run / "trajectory.json").write_text(
        json.dumps({"match_id": match_id, "trajectory": ticks}))


def test_audit_facts_curves_switches_and_ranks(tmp_path: Path) -> None:
    _write_traj(tmp_path, "m1", flood_turn=20)
    rows = [_row("m1", [500, 300, 350, 200]),
            _row("m2", [400, 450, 300, 250])]
    facts = audit_facts("flood_pivot", rows, tmp_path)
    assert facts["label"] == "flood_pivot"
    assert facts["games"] == 2 and facts["mean_rank"] == 2.5
    game = facts["per_game"][0]
    assert game["rank"] == 2 and game["seat"] == 2
    assert game["doctrines_played"] == ["hyperwide_flood", "turtler"]
    assert game["switches"] == [{"turn": 20, "from": "hyperwide_flood",
                                 "to": "turtler"}]
    # curve samples every 10 turns + the final turn
    assert [p["t"] for p in game["curve"]] == [10, 20, 30]
    # a label absent from a row is simply not counted for that game
    assert audit_facts("turtler", rows, tmp_path)["games"] == 4


def test_doctrine_docs_static_and_pivot() -> None:
    static = doctrine_docs_for("hyperwide_flood", ROSTER)
    assert static["doctrine"]["max_cities"] == 6
    assert "thesis" in static["doctrine"]
    pivot = doctrine_docs_for("flood_pivot", ROSTER)
    assert pivot["pivot_spec"]["initial"] == "hyperwide_flood"
    assert pivot["hyperwide_flood"]["max_cities"] == 6   # parent doctrine
    assert pivot["turtler"]["max_cities"] == 2           # switch target


def test_audit_prompt_names_target_and_contract(tmp_path: Path) -> None:
    _write_traj(tmp_path, "m1", flood_turn=20)
    facts = audit_facts("flood_pivot", [_row("m1", [500, 300, 350, 200])],
                        tmp_path)
    prompt = audit_prompt(facts, doctrine_docs_for("flood_pivot", ROSTER))
    assert "AUDIT TARGET: seat label flood_pivot" in prompt
    assert "pivot_spec" in prompt and "execution_score_0_10" in prompt
    assert '"turn": 20' in prompt  # the switch event is in the facts


def test_validate_verdict_contract() -> None:
    good = {"executing_strategy": True, "execution_score_0_10": 7,
            "evidence": ["a"], "deviations": [], "pivot_assessment": {},
            "recommended_changes": []}
    clean, problem = validate_verdict(good, "x")
    assert clean is good and problem is None
    for mutate, needle in [
            ({"executing_strategy": "yes"}, "boolean"),
            ({"execution_score_0_10": 11}, "0..10"),
            ({"evidence": "not a list"}, "list")]:
        bad = dict(good, **mutate)
        clean, problem = validate_verdict(bad, "x")
        assert clean is None and needle in problem
    assert validate_verdict(["nope"], "x")[1].startswith("x:")
