"""Strict JSON strategy validation with no engine/provider traffic."""
import copy
import math
import re

import pytest

from civ_arena.agents.strategy_directive import (
    DEFAULT_DIRECTIVE,
    DIRECTIVE_SCHEMA,
    validate_directive,
)


def validate(doc):
    return validate_directive(doc, player_id=0, owned_unit_ids={"u0:131073", "u1"})


def test_default_normalization_copies_and_canonicalizes():
    doc = validate({})
    assert doc == DEFAULT_DIRECTIVE
    assert validate(doc) == doc
    doc["scouting"]["weights"]["threat"] = 0
    assert validate({})["scouting"]["weights"]["threat"] == 5
    assert validate({"scouting": {"unit_types": ["WARRIOR", "SCOUT"]}}) == validate({})


def test_integral_json_number_version_normalizes_to_integer():
    assert validate({"version": 1.0}) == validate({"version": 1})
    assert type(validate({"version": 1.0})["version"]) is int


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


def _matches_tactical_schema(order):
    """Evaluate the closed string-only tactical schema cases, without dependencies.

    This checks provider-facing allowed inputs against the independent runtime
    validator. It is intentionally not a general JSON Schema implementation.
    """
    cases = DIRECTIVE_SCHEMA["properties"]["tactical_overrides"]["items"]["oneOf"]
    matches = 0
    for case in cases:
        assert case["type"] == "object" and case["additionalProperties"] is False
        properties = case["properties"]
        if not set(case["required"]) <= set(order) <= set(properties):
            continue
        accepted = True
        for key, value in order.items():
            spec = properties[key]
            assert spec["type"] == "string"
            if (not isinstance(value, str) or not spec.get("minLength", 0) <= len(value)
                    <= spec.get("maxLength", 1000)
                    or ("enum" in spec and value not in spec["enum"])
                    or ("pattern" in spec and re.fullmatch(spec["pattern"], value) is None)):
                accepted = False
                break
        matches += accepted
    return matches == 1


@pytest.mark.parametrize("action", ["hold", "move", "attack", "found_city"])
@pytest.mark.parametrize("args", [{}, {"dest": "1,-2"}, {"target_id": "u1:131073"},
                                  {"dest": "1,-2", "target_id": "u1:131073"}])
def test_provider_schema_and_runtime_accept_exact_same_tactical_arguments(action, args):
    order = {"unit_id": "u0:131073", "action": action, **args}
    schema_accepts = _matches_tactical_schema(order)
    try:
        validate({"tactical_overrides": [order]})
    except ValueError:
        runtime_accepts = False
    else:
        runtime_accepts = True
    assert schema_accepts == runtime_accepts
    expected_keys = ({"dest"} if action == "move" else {"target_id"} if action == "attack"
                     else set())
    assert schema_accepts == (set(args) == expected_keys)


@pytest.mark.parametrize("dest", ["east", "01,0", "-0,1", "1, 2", "1000000,0", "1,2junk"])
def test_provider_schema_refuses_noncanonical_destinations(dest):
    order = {"unit_id": "u0:131073", "action": "move", "dest": dest}
    assert not _matches_tactical_schema(order)
    with pytest.raises(ValueError):
        validate({"tactical_overrides": [order]})


@pytest.mark.parametrize("targets", [None, [], {"SCOUT": True}, {"SCOUT": -1},
                                      {"SCOUT": 33}, {"SCOUT": 1.5}, {"SCOUT": math.nan},
                                      {"SCOUT": math.inf}, {"SCOUT": 10 ** 1000}, {"SCOUT": "2"},
                                      {"scout": 2}, {"SCOUT;INJECT": 2}, {1: 2},
                                      {f"UNIT_{i}": 1 for i in range(17)}])
def test_unit_targets_reject_nonbounded_json_counts_and_ids(targets):
    with pytest.raises(ValueError, match="unit_targets"):
        validate({"unit_targets": targets})


def test_unit_target_optional_legacy_compatibility_and_integer_normalization():
    legacy = validate({"version": 1, "production_preferences": ["SCOUT"]})
    assert "unit_targets" not in legacy
    assert validate({"unit_targets": {}}) == validate({})
    normalized = validate({"unit_targets": {"SCOUT": 2.0, "BUILDER": 0, "WARRIOR": 32}})
    assert normalized["unit_targets"] == {"BUILDER": 0, "SCOUT": 2, "WARRIOR": 32}
    assert all(type(count) is int for count in normalized["unit_targets"].values())
    schema = DIRECTIVE_SCHEMA["properties"]["unit_targets"]
    assert schema["maxProperties"] == 16
    assert schema["additionalProperties"] == {"type": "integer", "minimum": 0, "maximum": 32}
