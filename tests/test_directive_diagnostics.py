"""Value-free validation diagnostics and counted repair; all providers are fixtures."""

import asyncio
import copy
import json
from dataclasses import replace

import pytest

from civ_arena.agents.llm import strategic_controller as controller_module
from civ_arena.agents.llm.context_curator import ContextCurator
from civ_arena.agents.llm.request_budget import payload_hash
from civ_arena.agents.strategy_directive import (
    DirectiveValidationError,
    coordinate,
    validate_directive,
    validation_diagnostic,
)
from civ_arena.arena.referee import MatchAborted
from fakes import use
from test_adaptive_context import harness
from test_compact_terrain_context import RetainedProjection
from test_strategic_controller import advance
from test_strategic_controller import setup as setup

MARKER = "PRIVATE_MODEL_CONTENT_MUST_NOT_BE_COPIED"


def validate(value):
    return validate_directive(value, player_id=0, owned_unit_ids={"owned"})


@pytest.mark.parametrize(
    "value,code,path",
    [
        (None, "expected_object", []),
        ({MARKER: MARKER}, "unknown_property", []),
        ({"version": True}, "unsupported_version", ["version"]),
        ({"scouting": {MARKER: MARKER}}, "unknown_property", ["scouting"]),
        ({"scouting": {"policy": MARKER}}, "invalid_enum", ["scouting", "policy"]),
        ({"scouting": {"selection": MARKER}}, "invalid_enum", ["scouting", "selection"]),
        ({"scouting": {"temperature": MARKER}}, "invalid_temperature", ["scouting", "temperature"]),
        ({"scouting": {"unit_types": [MARKER]}}, "invalid_item", ["scouting", "unit_types", 0]),
        (
            {"scouting": {"unit_types": ["SCOUT", "SCOUT"]}},
            "duplicate_item",
            ["scouting", "unit_types"],
        ),
        ({"scouting": {"weights": {MARKER: MARKER}}}, "unknown_property", ["scouting", "weights"]),
        (
            {"scouting": {"weights": {"threat": MARKER}}},
            "invalid_weight",
            ["scouting", "weights", "threat"],
        ),
        (
            {"research_preferences": ["not valid " + MARKER]},
            "invalid_item",
            ["research_preferences", 0],
        ),
        ({"unit_targets": {MARKER: MARKER}}, "invalid_unit_target", ["unit_targets"]),
        ({"unit_targets": {"not valid " + MARKER: 1}}, "invalid_unit_targets", ["unit_targets"]),
        (
            {"tactical_overrides": [{"unit_id": MARKER, "action": "hold"}]},
            "unit_not_owned",
            ["tactical_overrides", 0, "unit_id"],
        ),
        (
            {"tactical_overrides": [{"unit_id": "owned", "action": "hold"}] * 2},
            "duplicate_unit_override",
            ["tactical_overrides", 1, "unit_id"],
        ),
        (
            {"tactical_overrides": [{"unit_id": "owned", "action": MARKER}]},
            "invalid_action",
            ["tactical_overrides", 0, "action"],
        ),
        (
            {"tactical_overrides": [{"unit_id": "owned", "action": "move", "dest": MARKER}]},
            "invalid_destination",
            ["tactical_overrides", 0, "dest"],
        ),
        (
            {"tactical_overrides": [{"unit_id": "owned", "action": "hold", "dest": MARKER}]},
            "action_arguments",
            ["tactical_overrides", 0],
        ),
        (
            {
                "tactical_overrides": [
                    {"unit_id": "owned", "action": "attack", "target_id": "not valid " + MARKER}
                ]
            },
            "invalid_target_id",
            ["tactical_overrides", 0, "target_id"],
        ),
        (
            {"tactical_overrides": [{"unit_id": "owned", "action": "hold", MARKER: MARKER}]},
            "unknown_property",
            ["tactical_overrides", 0],
        ),
    ],
)
def test_typed_codes_paths_do_not_copy_model_keys_values_or_identifiers(value, code, path):
    before = copy.deepcopy(value)
    with pytest.raises(DirectiveValidationError) as exc:
        validate(value)
    diagnostic = validation_diagnostic(exc.value)
    assert isinstance(exc.value, ValueError)
    assert diagnostic == {"code": code, "path": path}
    assert MARKER not in json.dumps(diagnostic)
    assert value == before


@pytest.mark.parametrize(
    "code,path",
    [
        (MARKER, ()),
        ("invalid_enum", (MARKER,)),
        ("invalid_enum", ("scouting", True)),
        ("invalid_enum", ("scouting", 16)),
        ("invalid_enum", ["scouting"]),
        ("invalid_enum", ("scouting",) * 5),
    ],
)
def test_nonallowlisted_or_mutated_exception_metadata_is_unknown(code, path):
    exc = DirectiveValidationError(MARKER, code=code, path=path)
    assert validation_diagnostic(exc) == {"code": "unknown", "path": []}


def test_coordinate_utility_contract_is_unchanged_outside_directives():
    assert coordinate("-2,3") == (-2, 3)
    with pytest.raises(ValueError) as exc:
        coordinate("01,3")
    assert type(exc.value) is ValueError
    assert str(exc.value) == "coordinate must be canonical bounded axial q,r"


async def test_typed_diagnostic_enters_repair_counted_body_without_stale_valid_diagnostic(
    monkeypatch,
):
    runtime, ctl, requests, records, ledger, kinds = harness(
        monkeypatch,
        replies=[
            [use("submit_directive", {MARKER: MARKER})],
            [use("submit_directive", {"version": 1})],
        ],
    )
    curator = ContextCurator(RetainedProjection(), 1, 8000)
    await curator.refresh()
    try:
        result = await ctl._decide(runtime, curator, ["initial_strategy"])
    finally:
        await runtime.aclose()
    assert result["version"] == 1 and not result["tactical_overrides"]
    assert kinds == ["count_tokens", "generation"] * 2 and ledger == [1, 2, 3, 4]
    assert ctl._turn_strategy_requests == 2
    first, repaired = requests[0][1], requests[2][1]
    assert first["system"] == repaired["system"] and first["tools"] == repaired["tools"]
    assert (
        first["tool_choice"]
        == repaired["tool_choice"]
        == {"type": "tool", "name": "submit_directive"}
    )
    meta = json.loads(repaired["messages"][0]["content"].split("\n", 1)[0])
    diagnostic = {"code": "unknown_property", "path": []}
    assert meta["format_repair"]["validation_error"] == diagnostic
    assert meta["format_repair"]["previous_reason"] == "schema_or_ownership"
    shapes = [r for r in records if r["audit"] == "strategy_response_shape"]
    assert shapes[0]["validation_error"] == diagnostic
    assert shapes[0]["category"] == "invalid_args" and shapes[0]["reason"] == "schema_or_ownership"
    assert shapes[1]["category"] == "valid" and "validation_error" not in shapes[1]
    counted = [r for r in records if r["audit"] == "strategy_token_count_request"]
    assert counted[1]["input_payload_sha256"] == payload_hash(repaired)
    assert {k: v for k, v in requests[3][1].items() if k != "max_tokens"} == repaired
    without = copy.deepcopy(repaired)
    prefix, suffix = without["messages"][0]["content"].split("\n", 1)
    omitted = json.loads(prefix)
    del omitted["format_repair"]["validation_error"]
    without["messages"][0]["content"] = json.dumps(omitted) + "\n" + suffix
    assert payload_hash(without) != counted[1]["input_payload_sha256"]
    assert MARKER not in json.dumps(records) and MARKER not in json.dumps(requests)


@pytest.mark.parametrize("exception", [ValueError, TypeError, OverflowError, RecursionError])
async def test_unclassified_validator_error_never_copies_exception_text(monkeypatch, exception):
    runtime, ctl, requests, records, _, _ = harness(monkeypatch)
    curator = ContextCurator(RetainedProjection(), 1, 8000)
    await curator.refresh()

    def broken(*args, **kwargs):
        raise exception(MARKER)

    monkeypatch.setattr(controller_module, "validate_directive", broken)
    try:
        with pytest.raises(MatchAborted, match="invalid_args/schema_or_ownership"):
            await ctl._decide(runtime, curator, ["initial_strategy"])
    finally:
        await runtime.aclose()
    shapes = [r for r in records if r["audit"] == "strategy_response_shape"]
    assert len(shapes) == 2
    assert all(r["validation_error"] == {"code": "unknown", "path": []} for r in shapes)
    assert MARKER not in json.dumps(records) and MARKER not in json.dumps(requests)


@pytest.mark.parametrize("cap", [1, 2])
async def test_final_typed_rejection_has_no_actions_closure_or_extra_attempts(setup, cap):
    ctl, runtime, model, facade, records, scouting = setup
    runtime.llm = replace(runtime.llm, max_tool_rounds=cap)
    model.script = [
        [
            use(
                "submit_directive",
                {"tactical_overrides": [{"unit_id": "foreign", "action": "hold"}]},
            )
        ]
    ]
    with pytest.raises(MatchAborted, match="invalid_args/schema_or_ownership"):
        await advance(ctl, runtime, facade, 1)
    assert model.posts_sent == cap and not scouting.await_count
    assert "end_turn" not in facade.calls
    assert not [c for c in facade.calls if isinstance(c, tuple)]
    shapes = [r for r in records if r["audit"] == "strategy_response_shape"]
    assert len(shapes) == cap and shapes[-1]["repair_available"] is False
    assert all(
        r["validation_error"]
        == {"code": "unit_not_owned", "path": ["tactical_overrides", 0, "unit_id"]}
        for r in shapes
    )
    with pytest.raises(MatchAborted, match="previously failed"):
        await advance(ctl, runtime, facade, 1)
    assert model.posts_sent == cap


@pytest.mark.parametrize("attribute", ["code", "path"])
def test_missing_diagnostic_attribute_uses_unknown(attribute):
    error = DirectiveValidationError(MARKER, code="invalid_enum", path=("scouting", "policy"))
    delattr(error, attribute)
    assert validation_diagnostic(error) == {"code": "unknown", "path": []}


@pytest.mark.parametrize("attribute", ["code", "path"])
@pytest.mark.parametrize("raised", [ValueError, OSError])
def test_throwing_diagnostic_accessor_uses_unknown(attribute, raised):
    class BrokenMetadata(DirectiveValidationError):
        def __getattribute__(self, name):
            if name == attribute:
                raise raised(MARKER)
            return super().__getattribute__(name)

    error = BrokenMetadata(MARKER, code="invalid_enum", path=("scouting", "policy"))
    assert validation_diagnostic(error) == {"code": "unknown", "path": []}


@pytest.mark.parametrize("raised", [asyncio.CancelledError, KeyboardInterrupt, SystemExit])
def test_diagnostic_accessor_preserves_baseexception_control(raised):
    class CancelledMetadata(DirectiveValidationError):
        def __getattribute__(self, name):
            if name in {"code", "path"}:
                raise raised()
            return super().__getattribute__(name)

    error = CancelledMetadata(MARKER, code="invalid_enum")
    with pytest.raises(raised):
        validation_diagnostic(error)
