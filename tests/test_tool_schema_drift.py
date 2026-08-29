"""Drift meta-test: the static tool-schema manifest must mirror the real
tool registry exactly. Adding a 16th tool or changing any signature without
updating the manifest fails here — a stale manifest silently misleads the
model."""

from __future__ import annotations

import inspect

from civ_arena.agents.llm.tool_schemas import TOOL_SCHEMAS
from civ_arena.session.tools import TOOL_REGISTRY


def _signature_sets(fn) -> tuple[set[str], set[str]]:
    """(required, properties) derived from the real tool signature: params
    after ctx, excluding keyword-only ones (idempotency_key is never
    model-supplied); required = no default."""
    sig = inspect.signature(fn)
    params = [p for name, p in sig.parameters.items() if name != "ctx"]
    positional = [p for p in params if p.kind not in
                  (p.KEYWORD_ONLY, p.POSITIONAL_ONLY)]
    properties = {p.name for p in positional}
    required = {p.name for p in positional if p.default is inspect.Parameter.empty}
    return required, properties


def test_manifest_covers_exactly_the_registry():
    manifest_names = {s["name"] for s in TOOL_SCHEMAS}
    assert manifest_names == set(TOOL_REGISTRY), (
        f"manifest drift: missing={sorted(set(TOOL_REGISTRY) - manifest_names)} "
        f"extra={sorted(manifest_names - set(TOOL_REGISTRY))}"
    )


def test_manifest_params_match_signatures():
    for name, fn in TOOL_REGISTRY.items():
        schema = next(s for s in TOOL_SCHEMAS if s["name"] == name)
        required, properties = _signature_sets(fn)
        declared_props = set(schema["input_schema"].get("properties", {}))
        declared_req = set(schema["input_schema"].get("required", []))
        assert declared_props == properties, (
            f"{name}: manifest properties {sorted(declared_props)} != "
            f"signature {sorted(properties)}"
        )
        assert declared_req == required, (
            f"{name}: manifest required {sorted(declared_req)} != "
            f"signature {sorted(required)}"
        )
        assert declared_req <= declared_props


def test_every_tool_and_property_is_documented():
    for schema in TOOL_SCHEMAS:
        assert schema.get("description", "").strip(), f"{schema['name']}: no docs"
        assert schema["input_schema"]["type"] == "object"
        for prop, spec in schema["input_schema"].get("properties", {}).items():
            assert spec.get("type"), f"{schema['name']}.{prop}: no type"
            assert spec.get("description", "").strip(), (
                f"{schema['name']}.{prop}: no docs"
            )


def test_manifest_never_mentions_idempotency_or_identity():
    """The model must never be told about idempotency keys or identity."""
    for schema in TOOL_SCHEMAS:
        assert "idempotency" not in schema["description"].lower()
        for prop in schema["input_schema"].get("properties", {}):
            assert prop not in ("player_id", "owner_id", "idempotency_key")
