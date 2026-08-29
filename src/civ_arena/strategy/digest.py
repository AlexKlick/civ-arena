"""Compact observation digests: what the player SAW, persisted to the log.

Operates on PROJECTED docs only — never the omniscient one — so a digest is
by construction within the visibility allowlists: the foreign lists are the
projected foreign entities, field for field. Riding on the observation
TOOL_RESULT as an additive ``observed`` field, a digest makes last-known
beliefs and own-state facts rebuildable from the log alone (the replay
projection ignores the field, so model-free replay cannot diverge on it).
"""

from __future__ import annotations

from typing import Any

DIGEST_KINDS = frozenset({"units", "cities", "overview"})


def observation_digest(
    kind: str, projected: Any, player_id: int,
) -> dict[str, Any] | None:
    """Build the digest for a projected observation, or None for kinds the
    strategy plane does not track (visible_map, available_*)."""
    if kind == "units" and isinstance(projected, list):
        own, foreign = _split(projected, "owner_id", player_id)
        return {"own_units": len(own), "foreign_units": foreign}
    if kind == "cities" and isinstance(projected, list):
        own, foreign = _split(projected, "owner_id", player_id)
        population = 0
        for city in own:
            pop = city.get("population")
            if isinstance(pop, int) and not isinstance(pop, bool):
                population += pop
        return {
            "own_cities": len(own), "own_population": population,
            "foreign_cities": foreign,
        }
    if kind == "overview" and isinstance(projected, dict):
        you = projected.get("you")
        if not isinstance(you, dict):
            return None
        digest: dict[str, Any] = {}
        gold = you.get("gold")
        if isinstance(gold, int) and not isinstance(gold, bool):
            digest["gold"] = gold
        researched = you.get("researched")
        if isinstance(researched, list):
            digest["techs"] = len(researched)
        researching = you.get("researching")
        if isinstance(researching, str):
            digest["researching"] = researching
        return digest or None
    return None


def _split(
    projected: list[Any], owner_key: str, player_id: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    own: list[dict[str, Any]] = []
    foreign: list[dict[str, Any]] = []
    for entry in projected:
        if isinstance(entry, dict):
            (own if entry.get(owner_key) == player_id else foreign).append(entry)
    return own, foreign
