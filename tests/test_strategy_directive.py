"""Strict JSON strategy validation with no engine/provider traffic."""
import copy
import math

import pytest

from civ_arena.agents.strategy_directive import DEFAULT_DIRECTIVE, validate_directive


def validate(doc):
    return validate_directive(doc, player_id=0, owned_unit_ids={"u0:131073", "u1"})


def test_default_normalization_copies_and_canonicalizes():
    doc = validate({})
    assert doc == DEFAULT_DIRECTIVE
    assert validate(doc) == doc
    doc["scouting"]["weights"]["threat"] = 0
    assert validate({})["scouting"]["weights"]["threat"] == 5
    assert validate({"scouting": {"unit_types": ["WARRIOR", "SCOUT"]}}) == validate({})


@pytest.mark.parametrize("doc", [
    None, [], {"unrecognized": 1}, {"version": True}, {"version": 2},
    {"scouting": []}, {"scouting": {"policy": "omniscient"}},
    {"scouting": {"selection": []}}, {"scouting": {"weights": {"foo": 1}}},
    {"scouting": {"weights": {"threat": math.nan}}},
    {"scouting": {"weights": {"threat": math.inf}}},
    {"scouting": {"weights": {"threat": True}}},
    {"scouting": {"weights": {"threat": -1}}},
    {"scouting": {"weights": {"threat": 11}}},
    {"scouting": {"temperature": 0}}, {"scouting": {"temperature": 3}},
    {"scouting": {"unit_types": ["SETTLER"]}},
    {"scouting": {"unit_types": ["SCOUT", "SCOUT"]}},
    {"research_preferences": ["TECH;INJECT"]},
    {"research_preferences": ["MINING"] * 17},
    {"production_preferences": ["SCOUT", "SCOUT"]},
    {"tactical_overrides": [{}]},
    {"tactical_overrides": [{"unit_id": "u1:131073", "action": "hold"}]},
    {"tactical_overrides": [{"unit_id": "u0:131073", "action": "move", "dest": "01,2"}]},
    {"tactical_overrides": [{"unit_id": "u0:131073", "action": "move", "dest": "-0,2"}]},
    {"tactical_overrides": [{"unit_id": "u0:131073", "action": "move", "dest": "1,2",
                             "target_id": "u1"}]},
    {"tactical_overrides": [{"unit_id": "u0:131073", "action": "hold", "dest": "1,2"}]},
    {"tactical_overrides": [{"unit_id": "u0:131073", "action": "attack"}]},
    {"tactical_overrides": [{"unit_id": "u0:131073", "action": "hold"}] * 2},
])
def test_invalid_directives_fail_before_execution(doc):
    with pytest.raises(ValueError):
        validate(doc)


def test_opaque_sim_and_full_engine_ids_do_not_infer_ownership():
    doc = {"tactical_overrides": [{"unit_id": "u1", "action": "hold"},
                                  {"unit_id": "u0:131073", "action": "attack",
                                   "target_id": "u1:131073"}]}
    original = copy.deepcopy(doc)
    result = validate(doc)
    assert doc == original
    assert result["tactical_overrides"][0]["unit_id"] == "u0:131073"
    assert result["tactical_overrides"][1]["unit_id"] == "u1"
