"""Translate observed live identities at the planner's numeric-simulator boundary.

Cantor pairing is exact and allocation-free: same (owner, raw) always gives the
same internal integer, including after a rewind. Only current entity observation
rows authorize reverse dispatch. Map tags and predicted/synthetic entities do not.
Legacy simulator IDs pass through unchanged, including its existing action flow;
a mixed-format observation stream is refused before aliases can collide.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from civ_arena.game.civ6.entity_ids import decode

LEGACY_ID = re.compile(r"[uc](0|[1-9][0-9]*)\Z")
ENTITY_ARGS = {"unit_id": "u", "target_id": "u", "city_id": "c"}
POSITIONAL_ENTITY_ARGS = {
    "get_available_production": ("city_id",),
    "move_unit": ("unit_id",), "attack": ("unit_id", "target_id"),
    "fortify": ("unit_id",), "found_city": ("unit_id",),
    "set_city_production": ("city_id",), "purchase": ("city_id",),
}


def planner_id(wire_id: str, kind: str) -> str:
    """Lossless numeric surrogate of an explicitly qualified live identity."""
    owner, raw = decode(wire_id, kind)
    total = owner + raw
    return kind + str(total * (total + 1) // 2 + raw + 1)


class EntityBoundary:
    def __init__(self, facade: Any) -> None:
        self._facade = facade
        self._mode: str | None = None
        self._known: dict[str, dict[str, str]] = {"u": {}, "c": {}}

    def _encode(self, value: str, kind: str, owner: int | None = None) -> str:
        if not isinstance(value, str) or not value.startswith(kind):
            raise ValueError("entity observation has an invalid kind")
        mode = "qualified" if ":" in value else "legacy"
        if self._mode is not None and mode != self._mode:
            raise ValueError("mixed qualified and legacy entity observations")
        if mode == "qualified":
            actual_owner, _ = decode(value, kind)
            if owner is not None and actual_owner != owner:
                raise ValueError("entity observation owner disagrees with qualified identity")
            encoded = planner_id(value, kind)
        else:
            if LEGACY_ID.fullmatch(value) is None:
                raise ValueError("invalid legacy entity observation")
            encoded = value
        self._mode = mode
        return encoded

    def _observations(self, docs: Any, kind: str) -> Any:
        if not isinstance(docs, list):
            raise ValueError("entity projection must be a list")
        field = "unit_id" if kind == "u" else "city_id"
        translated, known = [], {}
        for row in docs:
            if not isinstance(row, dict):
                raise ValueError("entity projection row must be an object")
            original = row.get(field)
            owner = row.get("owner", row.get("owner_id"))
            if type(owner) is not int or owner < 0:
                raise ValueError("entity projection owner is unavailable")
            encoded = self._encode(original, kind, owner)
            if encoded in known:
                raise ValueError("duplicate projected entity identity")
            known[encoded] = original
            translated.append({**copy.deepcopy(row), field: encoded})
        # Reconciliations replace the registry, retiring dead/no-longer-seen ids.
        self._known[kind] = known
        return translated if self._mode == "qualified" else docs

    async def get_units(self) -> Any:
        return self._observations(await self._facade.get_units(), "u")

    async def get_cities(self) -> Any:
        return self._observations(await self._facade.get_cities(), "c")

    async def get_visible_map(self) -> Any:
        doc = await self._facade.get_visible_map()
        if not isinstance(doc, dict) or not isinstance(doc.get("tiles"), dict):
            return doc
        result = copy.deepcopy(doc)
        for tile in result["tiles"].values():
            if not isinstance(tile, dict):
                continue
            if tile.get("city_id"):
                tile["city_id"] = self._encode(tile["city_id"], "c")
        return result if self._mode == "qualified" else doc

    def _wire(self, value: Any, kind: str) -> Any:
        if self._mode == "legacy":
            return value
        if self._mode is None or value not in self._known[kind]:
            raise ValueError("planner entity is not currently observed; synthetic dispatch refused")
        return self._known[kind][value]

    def _result(self, value: Any, key: str = "") -> Any:
        # Results may describe newly created entities, but only a subsequent
        # get_units/get_cities observation can authorize commands to them.
        if isinstance(value, dict):
            return {k: self._result(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self._result(v, "unit_id" if key == "unmoved_units" else key)
                    for v in value]
        if key in ENTITY_ARGS and isinstance(value, str) and ":" in value:
            return self._encode(value, ENTITY_ARGS[key])
        return value

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        original = getattr(self._facade, name)
        if not callable(original):
            return original

        async def call(*args, **kwargs):
            positional = list(args)
            try:
                for index, field in enumerate(POSITIONAL_ENTITY_ARGS.get(name, ())):
                    if index < len(positional):
                        positional[index] = self._wire(positional[index], ENTITY_ARGS[field])
                rewritten = {key: self._wire(value, ENTITY_ARGS[key])
                             if key in ENTITY_ARGS else value for key, value in kwargs.items()}
            except ValueError as exc:
                # Plan execution consumes rejections; do not send synthetic ids
                # to the real facade or invent an accepted engine result.
                return {"status": "rejected", "rejection": "unobserved_planner_entity",
                        "reason": str(exc)}
            result = await original(*positional, **rewritten)
            if self._mode == "qualified":
                return self._result(result)
            return result
        return call
