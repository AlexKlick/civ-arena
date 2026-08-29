"""Compact observation digests: what the player SAW, persisted to the log.

Operates on PROJECTED docs only — never the omniscient one — so a digest is
by construction within the visibility allowlists: the foreign lists are the
projected foreign entities, field for field. Riding on the observation
TOOL_RESULT as an additive ``observed`` field, a digest makes last-known
beliefs and own-state facts rebuildable from the log alone (the replay
projection ignores the field, so model-free replay cannot diverge on it).

Ownership note: own-unit projections carry ``owner_id``, but own-city
projections are deep copies of the raw city doc and carry ``owner`` — the
split normalizes both keys (a city classified by the wrong key would land
in the foreign list with FULL own fields: an allowlist violation).

The digest is canonically validated BEFORE it is returned: a non-canonical
value (a float from a hostile adapter on the live leg) must fail here,
loudly, before any log record is written — not at the next checkpoint's
prefix hash with the bad record already fsync'd.
"""

from __future__ import annotations

from typing import Any

from civ_arena.canonical import CanonicalError, canonical

DIGEST_KINDS = frozenset({"units", "cities", "overview"})


def observation_digest(
    kind: str, projected: Any, player_id: int,
) -> dict[str, Any] | None:
    """Build the digest for a projected observation, or None for kinds the
    strategy plane does not track (visible_map, available_*)."""
    digest: dict[str, Any] | None = None
    if kind == "units" and isinstance(projected, list):
        own, foreign = _split(projected, player_id)
        digest = {"own_units": len(own), "foreign_units": foreign}
    elif kind == "cities" and isinstance(projected, list):
        own, foreign = _split(projected, player_id)
        population = 0
        for city in own:
            pop = city.get("population")
            if isinstance(pop, int) and not isinstance(pop, bool):
                population += pop
        digest = {
            "own_cities": len(own), "own_population": population,
            "foreign_cities": foreign,
        }
    elif kind == "overview" and isinstance(projected, dict):
        you = projected.get("you")
        if isinstance(you, dict):
            digest = {}
            gold = you.get("gold")
            if isinstance(gold, int) and not isinstance(gold, bool):
                digest["gold"] = gold
            researched = you.get("researched")
            if isinstance(researched, list):
                digest["techs"] = len(researched)
            researching = you.get("researching")
            if isinstance(researching, str):
                digest["researching"] = researching
    if digest is None:
        return None
    try:
        canonical(digest)
    except CanonicalError as exc:
        raise CanonicalError(
            f"observation digest is not canonical ({kind}): {exc}") from exc
    return digest


def _split(
    projected: list[Any], player_id: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Classify projected entities as own/foreign. Own docs may carry either
    ownership key (units: owner_id; cities: owner, deep-copied raw); foreign
    projections always carry owner_id within the allowlist."""
    own: list[dict[str, Any]] = []
    foreign: list[dict[str, Any]] = []
    for entry in projected:
        if isinstance(entry, dict):
            owner = entry.get("owner_id", entry.get("owner"))
            (own if owner == player_id else foreign).append(entry)
    return own, foreign
