"""The flash-proposed batch-002 roster: transcription pins (no network).

The roster arrived as JSON (research/iterations/000/proposal.json) and was
transcribed into DOCTRINES by hand — these pins make a typo fail here,
loudly, instead of silently mismatching a 16-game batch.
"""

from __future__ import annotations

import json
from pathlib import Path

from civ_arena.agents.runtime import AgentProfile, ScriptedRuntime, build_runtime
from civ_arena.agents.scripted import DOCTRINES, FLASH_ROSTER_IDS
from civ_arena.config import VALID_POLICIES, parse_config
from civ_arena.game.sim.state import BUILDINGS, TECHS, UNIT_TYPES

PROPOSAL = Path("research/iterations/000/proposal.json")
DECISION_FIELDS = ("research", "march", "fortify_idle", "build_order",
                   "purchase_pref", "max_cities", "aggression", "expand_ring")


def test_flash_roster_ids_transcribed() -> None:
    for doctrine_id in FLASH_ROSTER_IDS:
        assert doctrine_id in DOCTRINES
        assert doctrine_id in VALID_POLICIES


def test_decision_fields_match_the_proposal_verbatim() -> None:
    proposal = json.loads(PROPOSAL.read_text())
    assert proposal["status"] == "ok" and len(proposal["accepted"]) == 4
    proposed = {e["doctrine_id"]: e for e in proposal["accepted"]}
    assert set(proposed) == set(FLASH_ROSTER_IDS)
    for doctrine_id, entry in proposed.items():
        transcribed = DOCTRINES[doctrine_id]
        for field in DECISION_FIELDS:
            assert transcribed[field] == entry[field], (doctrine_id, field)
        for field in ("thesis", "expected_signature", "failure_mode"):
            assert isinstance(transcribed.get(field), str) and transcribed[field]


def test_roster_vocab_is_sim_legal() -> None:
    for doctrine_id in FLASH_ROSTER_IDS:
        doctrine = DOCTRINES[doctrine_id]
        assert doctrine["research"], doctrine_id
        for tech in doctrine["research"]:
            assert tech in TECHS, (doctrine_id, tech)
        for item in doctrine["build_order"] + doctrine["purchase_pref"]:
            assert item in UNIT_TYPES or item in BUILDINGS, (doctrine_id, item)


def test_every_doctrine_builds_a_scripted_runtime() -> None:
    # registry-driven dispatch: any DOCTRINES key, not a name list
    for doctrine_id in DOCTRINES:
        runtime = build_runtime(AgentProfile(
            agent_id=f"a-{doctrine_id}", player_id=0, policy=doctrine_id,
            seed=7))
        assert isinstance(runtime, ScriptedRuntime), doctrine_id


def test_batch002_seating_parses_as_four_seat_config() -> None:
    # one seat per doctrine — the exact shape research_loop's runner writes
    doc = {
        "match": {"match_id": "batch-002-s0-r0", "seed": 947381,
                  "max_turns": 40, "player_count": 4},
        "agents": [
            {"agent_id": f"seat{pid}", "player_id": pid,
             "policy": doctrine_id, "seed": 947381 * 10 + pid}
            for pid, doctrine_id in enumerate(FLASH_ROSTER_IDS)
        ],
    }
    spec = parse_config(doc)
    assert [a.policy for a in spec.agents] == list(FLASH_ROSTER_IDS)
    assert spec.player_count == 4
