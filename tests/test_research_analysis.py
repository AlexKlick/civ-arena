"""Roster-proposal validator + digest reductions (no network)."""

from __future__ import annotations

import json
from pathlib import Path

from civ_arena.game.sim.state import TECHS
from civ_arena.research.digest import digest_batch, digest_match
from civ_arena.research.roster import proposer_rules, validate_proposal

DOCTRINE = {
    "doctrine_id": "siege_turtler",
    "research": ["MINING", "MASONRY", "BRONZE_WORKING"],
    "march": False,
    "fortify_idle": True,
    "build_order": ["WALLS", "ARCHER"],
    "purchase_pref": ["MONUMENT", "WARRIOR"],
    "max_cities": 3,
    "aggression": 3,
    "expand_ring": 2,
    "thesis": "hold two cities, punish walkers",
    "expected_signature": "flat cities, high units",
    "failure_mode": "out-expanded by four-city rosters",
}


def _roster(mutate=None, count: int = 4) -> dict:
    entries = []
    for i in range(count):
        entry = json.loads(json.dumps(DOCTRINE))
        entry["doctrine_id"] = f"doctrine_{i}"
        if mutate:
            mutate(entry, i)
        entries.append(entry)
    return {"roster": entries}


def test_validate_proposal_accepts_well_formed_roster() -> None:
    accepted, dropped = validate_proposal(_roster())
    assert [e["doctrine_id"] for e in accepted] == [
        "doctrine_0", "doctrine_1", "doctrine_2", "doctrine_3"]
    assert dropped == []


def test_validate_proposal_rejects_wrong_size_and_duplicates() -> None:
    accepted, dropped = validate_proposal(_roster(count=3))
    assert accepted == [] and "expected 4 valid doctrines" in dropped[-1]
    accepted, dropped = validate_proposal(
        _roster(lambda e, i: None, count=4))
    assert len(accepted) == 4
    dup = _roster()
    dup["roster"][3]["doctrine_id"] = "doctrine_2"
    accepted, dropped = validate_proposal(dup)
    assert accepted == []
    assert any("duplicate doctrine_id" in d for d in dropped)


def test_validate_proposal_field_rejections() -> None:
    cases = [
        (lambda e, i: e.update(research=["NO SUCH"]), "unknown tech"),
        (lambda e, i: e.update(research=["MINING", "MINING"]), "duplicate tech"),
        (lambda e, i: e.update(build_order=["SPACESHIP"]), "unknown item"),
        (lambda e, i: e.update(build_order=["SLINGER"]), "not player-producible"),
        (lambda e, i: e.update(doctrine_id="llm"), "reserved"),
        (lambda e, i: e.update(doctrine_id="Bad-Id"), "bad doctrine_id"),
        (lambda e, i: e.update(march="yes"), "boolean"),
        (lambda e, i: e.update(max_cities=9), "outside"),
        (lambda e, i: e.pop("thesis"), "non-empty string"),
        (lambda e, i: e.update(expand_ring=0), "outside"),
    ]
    for mutate, needle in cases:
        _, dropped = validate_proposal(_roster(mutate))
        assert any(needle in d for d in dropped), (needle, dropped)


def test_validate_proposal_accepts_beeline_research_order() -> None:
    # ARCHERY before its prereq POTTERY: legal preference-order semantics —
    # the sim enforces prereqs at set_research time, the validator must not
    accepted, dropped = validate_proposal(_roster(
        lambda e, i: e.update(research=["ARCHERY", "POTTERY", "MINING"])))
    assert accepted and dropped == []
    assert accepted[0]["research"] == ["ARCHERY", "POTTERY", "MINING"]


def test_proposer_rules_name_the_whole_vocabulary() -> None:
    rules = proposer_rules()
    for tech in TECHS:
        assert tech in rules
    for item in ("MONUMENT", "GRANARY", "WALLS", "SETTLER", "ARCHER"):
        assert item in rules
    for reserved in ("llm", "planner", "adaptive"):
        assert reserved in rules


def _write_run(tmp_path: Path, match_id: str, turns: int) -> Path:
    run_dir = tmp_path / match_id
    run_dir.mkdir(parents=True)
    trajectory = []
    for turn in range(1, turns + 1):
        trajectory.append({
            "turn": turn,
            "scores": {
                f"C{pid}": {"cities": 1 + pid, "population": 2,
                            "techs": 1, "units": 3, "gold": 10,
                            "scalar": 100 * (1 + pid) + 20 * 2 + 30 + 30 + 10}
                for pid in range(4)
            },
            "doctrines": {str(pid): None for pid in range(4)},
        })
    (run_dir / "trajectory.json").write_text(
        json.dumps({"match_id": match_id, "trajectory": trajectory}))
    return run_dir


def test_digest_match_samples_and_budget(tmp_path) -> None:
    run_dir = _write_run(tmp_path, "m1", 12)
    row = {
        "match_id": "m1", "seed": 7, "rotation": 0, "turns": 12,
        "winner_pid": 2, "margin": 100, "dirty": False,
        "seats": [
            {"player_id": pid, "agent_id": f"d{pid}", "doctrine": f"d{pid}",
             "civ_name": f"C{pid}", "scalar": 200 + pid}
            for pid in range(4)
        ],
    }
    digest = digest_match(row, run_dir)
    assert digest.startswith("MATCH m1 seed=7")
    assert "seat0=d0(C0)" in digest and "t12" in digest
    assert "t11" not in digest  # only sampled turns + the final turn
    assert len(digest) <= 1200


def test_digest_batch_joins_matches(tmp_path: Path) -> None:
    for match_id in ("m1", "m2"):
        _write_run(tmp_path, match_id, 5)
    rows = [
        {"match_id": mid, "seed": 7, "rotation": 0, "turns": 5,
         "winner_pid": None, "margin": 0, "dirty": False,
         "seats": [{"player_id": 0, "agent_id": "a", "doctrine": "d",
                    "civ_name": "C0", "scalar": 1}] + [
             {"player_id": pid, "agent_id": "a", "doctrine": "d",
              "civ_name": f"C{pid}", "scalar": 1} for pid in range(1, 4)]}
        for mid in ("m1", "m2")
    ]
    joined = digest_batch(rows, tmp_path)
    assert "MATCH m1" in joined and "MATCH m2" in joined
    assert joined.index("MATCH m1") < joined.index("MATCH m2")
