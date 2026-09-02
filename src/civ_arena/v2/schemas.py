"""Draft 2020-12 schema loading and fail-closed V2 validation.

The repository-level ``schemas/v2`` directory is the source of truth. Hatch
places those exact files under ``civ_arena/schemas/v2`` in built wheels; an
editable checkout falls back to the repository source directory.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


class ContractError(ValueError):
    """A public V2 document failed schema or semantic validation."""


SCHEMA_FILES = (
    "common.json",
    "observation.json",
    "action.json",
    "graph.json",
    "descriptor.json",
    "artifact.json",
    "receipt.json",
    "event.json",
)


def _schema_dir() -> Path | None:
    checkout = Path(__file__).resolve().parents[3] / "schemas" / "v2"
    if checkout.is_dir():
        return checkout
    return None


@lru_cache(maxsize=1)
def schema_documents() -> dict[str, dict[str, Any]]:
    source = _schema_dir()
    docs: dict[str, dict[str, Any]] = {}
    for filename in SCHEMA_FILES:
        if source is not None:
            text = (source / filename).read_text(encoding="utf-8")
        else:
            text = (
                resources.files("civ_arena")
                .joinpath("schemas", "v2", filename)
                .read_text(encoding="utf-8")
            )
        doc = json.loads(text)
        Draft202012Validator.check_schema(doc)
        schema_id = doc.get("$id")
        if not isinstance(schema_id, str) or not schema_id.startswith("urn:civ-arena:"):
            raise RuntimeError(f"V2 schema {filename} has no valid $id")
        if schema_id in docs:
            raise RuntimeError(f"duplicate V2 schema id: {schema_id}")
        docs[schema_id] = doc
    return docs


@lru_cache(maxsize=1)
def schema_registry() -> Registry:
    pairs = [
        (schema_id, Resource.from_contents(doc))
        for schema_id, doc in schema_documents().items()
    ]
    return Registry().with_resources(pairs)


def validate_doc(schema_ref: str, doc: Any) -> None:
    """Validate with a deliberately receipt-safe error surface.

    jsonschema's default message may echo the rejected value. V2 callers get
    only the structural path and validator keyword; privileged adapter data is
    never reflected into policy-visible errors.
    """

    if schema_ref.split("#", 1)[0] not in schema_documents():
        raise ContractError("unknown V2 schema reference")
    # Validate through a reference wrapper so local ``#/$defs`` references
    # retain the base URI of their owning document. Extracting a fragment and
    # validating it as a root would incorrectly resolve those refs inside the
    # fragment itself.
    validator = Draft202012Validator(
        {"$schema": "https://json-schema.org/draft/2020-12/schema", "$ref": schema_ref},
        registry=schema_registry(),
        format_checker=FormatChecker(),
    )
    error = next(iter(validator.iter_errors(doc)), None)
    if error is None:
        return
    path = "$" + "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}"
        for part in error.absolute_path
    )
    raise ContractError(
        f"V2 document rejected at {path} ({error.validator or 'schema'})"
    )


def validate_all_schemas() -> tuple[str, ...]:
    """Load/check every schema and return its stable identifier."""

    return tuple(sorted(schema_documents()))
