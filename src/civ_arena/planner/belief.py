"""M15b — the belief forward model: projections in, rollout-ready SimState out.

``PlannerBelief`` accumulates the player's own projected observations (the
exact docs the tool facade returns). ``build_state_doc`` determinizes the
belief into a complete, canonical SimState doc that ``legal_actions`` /
``apply_action`` / ``run_ambient`` can roll out. The planner NEVER forks
ground truth; this reconstruction is its only world.

Fairness at the input seam is SHAPE validation, honestly scoped: foreign
entries carrying any field beyond the projection allowlists are refused
loudly, own entries must match the own-projection field set exactly, and
determinized values are pure functions of allowlisted fields (hp from
hp_bucket, movement from the type table, fortified False). This defends
against buggy or mis-scoped callers, NOT against a caller that fabricates
perfectly-shaped docs from ground truth — the ownership discriminators
(``owner_id``/``owner``) and the map's observable-vs-remembered shape are
trusted as projection outputs, the same trust class as the tool facade's
closure binding (deliberate self-deception is out of threat model).

Declared priors, all deliberate and test-visible:
- own ``science_bucket`` is not projected anywhere → prior 0 (the model
  under-predicts its own research completion; a rollout is a lower bound);
- foreign gold prior = 100 (the start value), science_bucket 0,
  researching "";
- foreign researched techs are inferred from seen unit types via
  ``UNIT_TECH_REQ`` with transitive prereq closure;
- foreign last-seen entities persist with no expiry (the M11 belief
  philosophy: an unobserved death must not erase the last known position);
- remembered tiles keep the last LIVE-OBSERVED ownership (same no-expiry
  epistemics — set only while the tile was observable, never from a
  terrain-only projection);
- a rival with NO last-seen units and no known cities gets the PUBLIC
  duel roster (2 settlers + 2 warriors + 1 scout) at its layout start —
  game-setup knowledge, not hidden state; once anything of that rival has
  been observed, only last-seen entities are reconstructed;
- unknown tiles sample terrain from the map generator's weights, seeded;
- foreign revealed sets are empty (opponent vision is not modeled).
"""

from __future__ import annotations

import random
from typing import Any

from civ_arena.arena.visibility import FOREIGN_CITY_FIELDS, FOREIGN_UNIT_FIELDS
from civ_arena.canonical import rng_to_doc
from civ_arena.game.sim.layouts import STARTS, _weighted_terrain
from civ_arena.game.sim.state import (
    MAP_RADIUS,
    TECHS,
    UNIT_TECH_REQ,
    UNIT_TYPES,
    map_tiles,
    parse_key,
    tile_key,
)
from civ_arena.planner.uncertainty import staleness_confidence

OWN_GOLD_PRIOR = 100  # the duel start value; used for never-observed rivals

# Public game setup (layouts.duel_start): the starting roster every player
# receives. Used as the prior force for a rival nothing has been seen of.
START_ROSTER = ("SETTLER", "SETTLER", "WARRIOR", "WARRIOR", "SCOUT")

# The exact field set the projection emits for OWN units — an own-labeled
# entry with any other shape is refused (shape validation, see module doc).
OWN_UNIT_FIELDS = frozenset({
    "unit_id", "owner_id", "type", "coord", "hp", "hp_bucket", "movement",
    "max_movement", "strength", "ranged_strength", "fortified",
})

# Keys an own-city projection must carry (the full sim city doc + coord).
OWN_CITY_REQUIRED = frozenset({
    "city_id", "owner", "name", "coord", "q", "r", "population", "hp",
    "food_bucket", "production_bucket", "production_queue", "buildings",
    "border_radius",
})


def _hp_from_bucket(bucket: int) -> int:
    """Pure function of the projected bucket — never of true hp."""
    return min(100, max(1, 25 * bucket + 12))


def _tech_closure(techs: set[str]) -> list[str]:
    out = set(techs)
    frontier = list(techs)
    while frontier:
        tech = frontier.pop()
        for prereq in TECHS[tech]["prereq"]:
            if prereq not in out:
                out.add(prereq)
                frontier.append(prereq)
    return sorted(out)


class PlannerBelief:
    """Accumulates one player's projected observations across turns."""

    def __init__(self, player_id: int) -> None:
        self.player_id = player_id
        self.turn = 1
        self.own_player: dict[str, Any] = {}
        self.public_players: dict[int, dict[str, Any]] = {}
        self.own_units: dict[str, dict[str, Any]] = {}
        self.own_cities: dict[str, dict[str, Any]] = {}
        self.foreign_units: dict[str, dict[str, Any]] = {}
        self.foreign_cities: dict[str, dict[str, Any]] = {}
        # tile key -> {"terrain": str} plus, when currently seen,
        # {"owner": int, "city": str}
        self.tiles: dict[str, dict[str, Any]] = {}
        # the CURRENT observation's live sets (rebuilt per observe call):
        # used to suppress last-seen entries a live observation contradicts
        self.current_observable: set[str] = set()
        self.current_foreign_ids: set[str] = set()

    # -- observation intake (the ONLY inputs this layer accepts) ----------

    def observe_overview(self, doc: dict[str, Any]) -> None:
        self.turn = max(self.turn, int(doc.get("turn") or 0))
        you = doc["you"]
        self.own_player = {
            "player_id": you["player_id"],
            "civ_name": you["civ_name"],
            "gold": you["gold"],
            "researched": list(you["researched"]),
            "researching": you["researching"],
        }
        for p in doc.get("public", {}).get("players", []):
            self.public_players[p["player_id"]] = {
                "civ_name": p["civ_name"], "alive": p["alive"],
            }

    def observe_units(self, docs: list[dict[str, Any]], turn: int) -> None:
        self.turn = max(self.turn, turn)
        own: dict[str, dict[str, Any]] = {}
        for u in docs:
            if u["owner_id"] == self.player_id:
                if set(u) != OWN_UNIT_FIELDS:
                    raise ValueError(
                        f"own unit {u.get('unit_id')} does not match the "
                        f"own-projection shape — refusing")
                own[u["unit_id"]] = dict(u)
                continue
            extra = set(u) - FOREIGN_UNIT_FIELDS
            if extra:
                raise ValueError(
                    f"foreign unit {u.get('unit_id')} carries non-allowlisted "
                    f"fields {sorted(extra)} — refusing an over-informative "
                    "observation")
            self.foreign_units[u["unit_id"]] = {**u, "last_seen_turn": turn}
        # a units observation is complete for own units: replace wholesale
        self.own_units = own
        self.current_foreign_ids = {
            u["unit_id"] for u in docs if u["owner_id"] != self.player_id}

    def observe_cities(self, docs: list[dict[str, Any]], turn: int) -> None:
        self.turn = max(self.turn, turn)
        own: dict[str, dict[str, Any]] = {}
        for c in docs:
            owner = c.get("owner", c.get("owner_id"))
            if owner == self.player_id:
                missing = OWN_CITY_REQUIRED - set(c)
                if missing:
                    raise ValueError(
                        f"own city {c.get('city_id')} missing projection "
                        f"fields {sorted(missing)} — refusing")
                own[c["city_id"]] = dict(c)
                continue
            extra = set(c) - FOREIGN_CITY_FIELDS
            if extra:
                raise ValueError(
                    f"foreign city {c.get('city_id')} carries non-allowlisted "
                    f"fields {sorted(extra)} — refusing an over-informative "
                    "observation")
            self.foreign_cities[c["city_id"]] = {**c, "last_seen_turn": turn}
        self.own_cities = own

    def observe_map(self, doc: dict[str, Any]) -> None:
        self.turn = max(self.turn, int(doc.get("turn") or 0))
        self.current_observable = {
            key for key, tile in doc.get("tiles", {}).items()
            if "owner_id" in tile}
        for key, tile in doc.get("tiles", {}).items():
            entry: dict[str, Any] = {"terrain": tile["terrain"]}
            if "owner_id" in tile:  # currently observed: live ownership known
                entry["owner"] = tile["owner_id"]
                entry["city"] = tile["city_id"]
            elif key in self.tiles and "owner" in self.tiles[key]:
                # remembered tile: keep the last live ownership we saw
                entry["owner"] = self.tiles[key]["owner"]
                entry["city"] = self.tiles[key]["city"]
            self.tiles[key] = entry


def build_state_doc(belief: PlannerBelief, seed: int) -> dict[str, Any]:
    """Determinize the belief into a complete canonical SimState doc.

    Same belief + same seed => byte-identical doc (test-pinned via
    state_hash); the seed varies only what was never observed.
    """
    rng = random.Random(seed)
    pid = belief.player_id

    tiles: dict[str, dict[str, Any]] = {}
    for q, r in map_tiles(MAP_RADIUS):
        key = tile_key(q, r)
        known = belief.tiles.get(key)
        terrain = known["terrain"] if known else _weighted_terrain(rng)
        tiles[key] = {
            "q": q, "r": r, "terrain": terrain,
            "owner": known.get("owner", -1) if known else -1,
            "city": known.get("city", "") if known else "",
        }

    units: dict[str, dict[str, Any]] = {}
    for uid in sorted(belief.own_units, key=lambda u: int(u[1:])):
        u = belief.own_units[uid]
        q, r = parse_key(u["coord"])
        units[uid] = {
            "unit_id": uid, "owner": pid, "type": u["type"], "q": q, "r": r,
            "movement": u["movement"], "max_movement": u["max_movement"],
            "hp": u["hp"], "strength": u["strength"],
            "ranged_strength": u["ranged_strength"], "fortified": u["fortified"],
        }
    for uid in sorted(belief.foreign_units, key=lambda u: int(u[1:])):
        u = belief.foreign_units[uid]
        # A live observation CONTRADICTS this entry: its recorded tile is
        # currently observable but the unit was not in the current units
        # observation — it died or moved; materializing it there would
        # offer phantom attack targets and false blockers. The entry stays
        # in the store (no-expiry: it may reappear elsewhere), it just
        # does not reconstruct while contradicted.
        if (u["coord"] in belief.current_observable
                and uid not in belief.current_foreign_ids):
            continue
        q, r = parse_key(u["coord"])
        spec = UNIT_TYPES[u["type"]]
        units[uid] = {
            "unit_id": uid, "owner": u["owner_id"], "type": u["type"],
            "q": q, "r": r,
            "movement": spec["mv"], "max_movement": spec["mv"],
            "hp": _hp_from_bucket(u["hp_bucket"]),
            "strength": u["strength"], "ranged_strength": u["ranged_strength"],
            "fortified": False,
            # M16c: derived read-time uncertainty (the STORE entry never
            # expires; the strategy's confidence in it does)
            "confidence": staleness_confidence(u["last_seen_turn"], belief.turn),
        }

    cities: dict[str, dict[str, Any]] = {}
    for cid in sorted(belief.own_cities, key=lambda c: int(c[1:])):
        c = dict(belief.own_cities[cid])
        q, r = parse_key(c.pop("coord"))
        c["q"], c["r"] = q, r
        cities[cid] = c
    for cid in sorted(belief.foreign_cities, key=lambda c: int(c[1:])):
        c = belief.foreign_cities[cid]
        q, r = parse_key(c["coord"])
        cities[cid] = {
            "city_id": cid, "owner": c["owner_id"], "name": c["name"],
            "q": q, "r": r, "population": c["population"], "hp": c["hp"],
            "food_bucket": 0, "production_bucket": 0, "production_queue": [],
            "buildings": [], "border_radius": 2,
        }

    seen_types: dict[int, set[str]] = {}
    for u in belief.foreign_units.values():
        seen_types.setdefault(u["owner_id"], set()).add(u["type"])
    seen_city_owners = {c["owner_id"] for c in belief.foreign_cities.values()}

    # Prior forces: a rival NOTHING has been seen of gets the public duel
    # roster at its layout start (game-setup knowledge, not hidden state).
    # Ids are allocated above every id in play so nothing collides.
    prior_uid = max(
        [int(u[1:]) for u in units]
        + [int(u["unit_id"][1:]) for u in belief.foreign_units.values()]
        + [0]) + 1
    for opid in sorted(belief.public_players):
        if opid == pid or opid in seen_types or opid in seen_city_owners:
            continue
        start = STARTS.get(opid)
        if start is None:
            continue
        for type_ in START_ROSTER:
            spec = UNIT_TYPES[type_]
            uid = f"u{prior_uid}"
            prior_uid += 1
            units[uid] = {
                "unit_id": uid, "owner": opid, "type": type_,
                "q": start[0], "r": start[1],
                "movement": spec["mv"], "max_movement": spec["mv"],
                "hp": 100, "strength": spec["strength"],
                "ranged_strength": spec["ranged"], "fortified": False,
                # setup knowledge decays like a turn-1 sighting (Codex M16c
                # P2): complete ignorance must never target with MORE
                # confidence than partial observation — scout before rush
                "confidence": staleness_confidence(1, belief.turn),
            }

    players: dict[str, dict[str, Any]] = {}
    players[str(pid)] = {
        "player_id": pid,
        "civ_name": belief.own_player.get("civ_name", ""),
        "gold": belief.own_player.get("gold", OWN_GOLD_PRIOR),
        "science_bucket": 0,  # declared prior: never projected
        "researched": sorted(belief.own_player.get("researched", [])),
        "researching": belief.own_player.get("researching", ""),
        "alive": True,
    }
    for opid, pub in sorted(belief.public_players.items()):
        if opid == pid:
            continue
        inferred = {UNIT_TECH_REQ[t] for t in seen_types.get(opid, set())
                    if t in UNIT_TECH_REQ}
        players[str(opid)] = {
            "player_id": opid,
            "civ_name": pub["civ_name"],
            "gold": OWN_GOLD_PRIOR,
            "science_bucket": 0,
            "researched": _tech_closure(inferred),
            "researching": "",
            "alive": pub["alive"],
        }

    max_uid = max((int(u[1:]) for u in units), default=0)
    # City ids also live on tile tags: a border tile can be legally observed
    # while its city center is hidden, and a colliding freshly-founded city
    # would inherit that foreign territory in run_ambient.
    tile_cids = [int(t["city"][1:]) for t in tiles.values()
                 if t["city"] and t["city"][1:].isdigit()]
    max_cid = max([int(c[1:]) for c in cities] + tile_cids + [0])
    revealed = {p: [] for p in sorted(players)}
    revealed[str(pid)] = sorted(belief.tiles)

    return {
        "turn": belief.turn,
        "phase_index": 0,
        "phase_player": -1,
        "next_unit_id": max_uid + 1,
        "next_city_id": max_cid + 1,
        "freeze_active": False,
        "tiles": tiles,
        "units": units,
        "cities": cities,
        "players": players,
        "revealed": revealed,
        "rng": rng_to_doc(random.Random(seed * 7919 + 17)),
    }
