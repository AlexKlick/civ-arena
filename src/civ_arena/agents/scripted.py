"""Deterministic scripted policies: expansionist + turtler (+ research-loop
roster doctrines behind the same DOCTRINES registry).

The bots see ONLY their projected observations (the facade) — the same view
an LLM runtime will get. Decisions key on (turn, observation, rng) with no
wall clock, so matches replay and resume deterministically. Rejected tool
calls are normal policy noise and stay in the log.

Geometry seam: every seat-count-dependent constant routes through
``_march_target`` / ``_settler_target`` / ``_corners``. The 2-seat paths
return the historical literals verbatim (bit-identity is pinned by
tests/test_four_seat_sim.py golden hashes + test_learned_weights).
"""

from __future__ import annotations

import random
from typing import Any

from civ_arena.game.sim.layouts import STARTS_4, hex_line_greedy
from civ_arena.game.sim.state import hex_dist, neighbors, parse_key

DOCTRINES: dict[str, dict[str, Any]] = {
    "expansionist": {
        "research": ["POTTERY", "ARCHERY", "WRITING", "MINING", "MASONRY",
                     "BRONZE_WORKING", "ANIMAL_HUSBANDRY", "IRRIGATION"],
        "march": True,           # military pushes toward the far corridor
        "fortify_idle": False,
        "build_order": ["SETTLER", "WARRIOR", "ARCHER", "SCOUT"],
        "purchase_pref": ["SCOUT", "WARRIOR", "MONUMENT", "GRANARY"],
        "max_cities": 4,
    },
    "turtler": {
        "research": ["MINING", "MASONRY", "BRONZE_WORKING", "ANIMAL_HUSBANDRY",
                     "POTTERY", "ARCHERY", "WRITING", "IRRIGATION"],
        "march": False,
        "fortify_idle": True,
        "build_order": ["WALLS", "MONUMENT", "WARRIOR", "SPEARMAN"],
        "purchase_pref": ["MONUMENT", "GRANARY", "WARRIOR"],
        "max_cities": 2,
    },
    # --- research-loop batch-002 roster: proposed by glm-5.3-flash
    # (iteration 000, 0 drops), user-approved 2026-09-17. Transcribed
    # verbatim from research/iterations/000/proposal.json — thesis /
    # expected_signature / failure_mode ride along as provenance; run_policy
    # only reads the decision fields.
    "hyperwide_flood": {
        "research": ["POTTERY", "ARCHERY", "BRONZE_WORKING", "WRITING",
                     "MINING", "MASONRY", "ANIMAL_HUSBANDRY", "IRRIGATION"],
        "march": False,
        "fortify_idle": True,
        "build_order": ["SETTLER", "GRANARY", "ARCHER", "SPEARMAN"],
        "purchase_pref": ["GRANARY", "SETTLER", "SCOUT", "MONUMENT"],
        "max_cities": 6,
        "aggression": 1,
        "expand_ring": 3,
        "thesis": "Convert every point of production into settlers to reach "
                  "5-6 cities before turn 30, winning on the compounding "
                  "city/pop/science curve while the high max_cities cap "
                  "shrugs off foreign-city sight corruption.",
        "expected_signature": "Cities 2->3 by turn ~10 then step to 5-6 by "
                              "turn ~30; steepest population and tech curves "
                              "in the field (science = 2*pop/city); units "
                              "stuck around 5-8 (one archer/spear per city); "
                              "gold low, sawtoothing at the 120 gate on "
                              "granary buys.",
        "failure_mode": "An early archer/marcher neighbor that camps settler "
                        "corridors kills 80-prod settlers and unfortified "
                        "pop-1 cities faster than new foundings compound; "
                        "also a map where founding space inside ring 3 runs "
                        "out leaves settlers idling at 16 turns apiece.",
    },
    "granary_engine": {
        "research": ["POTTERY", "ARCHERY", "WRITING", "MINING",
                     "BRONZE_WORKING", "MASONRY", "ANIMAL_HUSBANDRY",
                     "IRRIGATION"],
        "march": False,
        "fortify_idle": True,
        "build_order": ["GRANARY", "MONUMENT", "SPEARMAN", "ARCHER"],
        "purchase_pref": ["GRANARY", "MONUMENT", "SPEARMAN", "SCOUT"],
        "max_cities": 3,
        "aggression": 0,
        "expand_ring": 2,
        "thesis": "Lock 3 cities fast, then maximize per-city efficiency: "
                  "granaries lift the pop cap 7->9 (stacking 20-pt pop and "
                  "compounding science) while monument income banks gold to "
                  "buy granaries outright on every turn%3 gate.",
        "expected_signature": "Cities flat at 3 by ~turn 8; the highest "
                              "per-city population trajectory (climbing "
                              "toward 9/city late); techs steady and "
                              "near-front of pack on 3-city science; small "
                              "fortified defensive core (~5 units, healing "
                              "10/turn); gold sawtooths 120+ with 120-gold "
                              "granary purchases replacing unit padding.",
        "failure_mode": "Any wide opponent that reaches a fixed 3-city "
                        "ceiling-beating 4-5 cities is mathematically out of "
                        "reach within 40 turns under own-minus-strongest-"
                        "rival scoring, and aggression 0 means archers farm "
                        "its perimeter for free 10-point kills it never "
                        "answers.",
    },
    "archer_horde": {
        "research": ["ARCHERY", "BRONZE_WORKING", "POTTERY", "WRITING",
                     "MINING", "MASONRY", "ANIMAL_HUSBANDRY", "IRRIGATION"],
        "march": True,
        "fortify_idle": True,
        "build_order": ["ARCHER", "SPEARMAN", "WARRIOR", "GRANARY"],
        "purchase_pref": ["ARCHER", "SPEARMAN", "SCOUT", "WARRIOR"],
        "max_cities": 2,
        "aggression": 6,
        "expand_ring": 1,
        "thesis": "Beeline ARCHERY for the earliest ranged unlock, spam "
                  "archers from a compact 2-city base, and march them into "
                  "rivals to farm 10-point kills per turn via range-2 "
                  "attacks that take no melee counter while keeping 6 "
                  "attacks/turn of tempo.",
        "expected_signature": "Cities flat at 2 from turn ~3; middling "
                              "pop/techs on the small base; units climbing "
                              "steadily toward 12-16 as archers are built "
                              "and purchased at the gate, punctuated by "
                              "near-zero losses (ranged chip + fortify/heal "
                              "when idle); each rival's unit count visibly "
                              "decaying; gold low from 100-gold archer "
                              "purchases.",
        "failure_mode": "Opponents who refuse contact and silently "
                        "out-expand: unit kills (10 pts) cannot close a "
                        "3-city (300+ pts) differential, and the hardcoded "
                        "shared march lanes mean three seats' hordes funnel "
                        "to the same tile, arriving stacked, unhealed, and "
                        "trading piecemeal into a fortified spearmen-with-"
                        "archer-backline defense. [Pre-seam text: the "
                        "geometry seam (3015cb4) removed the shared lanes "
                        "before this doctrine ever ran.]",
    },
    "settler_broker": {
        "research": ["POTTERY", "MINING", "ARCHERY", "BRONZE_WORKING",
                     "WRITING", "MASONRY", "ANIMAL_HUSBANDRY", "IRRIGATION"],
        "march": False,
        "fortify_idle": True,
        "build_order": ["MONUMENT", "GRANARY", "SETTLER", "ARCHER"],
        "purchase_pref": ["SETTLER", "GRANARY", "MONUMENT", "ARCHER"],
        "max_cities": 4,
        "aggression": 1,
        "expand_ring": 2,
        "thesis": "Run 3 quick cities on monument income, then treat the "
                  "turn%3 gold gate as a settler market: bank to 160 and "
                  "buy settlers directly, converting gold into instant "
                  "+100 foundings that skip the 16-turn production "
                  "pipeline entirely.",
        "expected_signature": "Cities 3 by ~turn 8, then discrete +1 jumps "
                              "every few gated turns as 160-gold purchases "
                              "land (4-5 by turn ~25); strong pop/techs on "
                              "the mid-wide base; modest defensive units; "
                              "gold oscillating 120-160 as banked income "
                              "drains into settler and granary buys.",
        "failure_mode": "If per-turn income stalls (cities harassed, "
                        "monuments delayed) gold never clears the 160 "
                        "threshold and the doctrine is left with few "
                        "mid-game units and no expansion engine; a flood "
                        "opponent that saturates all legal founding spots "
                        "first also converts every banked settler purchase "
                        "into a dead 160-gold liability.",
    },
    # settler_broker + the H2 fix, field-identical otherwise: the purchase
    # gate waits for the bank floor instead of opening at 120.
    "settler_broker_banked": {
        "research": ["POTTERY", "MINING", "ARCHERY", "BRONZE_WORKING",
                     "WRITING", "MASONRY", "ANIMAL_HUSBANDRY", "IRRIGATION"],
        "march": False,
        "fortify_idle": True,
        "build_order": ["MONUMENT", "GRANARY", "SETTLER", "ARCHER"],
        "purchase_pref": ["SETTLER", "GRANARY", "MONUMENT", "ARCHER"],
        "max_cities": 4,
        "aggression": 1,
        "expand_ring": 2,
        "bank_floor": 160,
        "thesis": "Identical market thesis to settler_broker, minus the "
                  "self-defeating gate: purchases wait for the bank floor "
                  "(160) so the settler bank is never spent on granaries "
                  "first (H2's registered fix, batch-004 test seat).",
        "expected_signature": "Gold climbs past 160 before each gate turn "
                              "and drops by exactly 160 (SETTLER), discrete "
                              "founding jumps earlier and more often than "
                              "settler_broker's; granary purchases near "
                              "zero until max_cities saturates.",
        "failure_mode": "If the gate now rarely opens (income below 160 "
                        "between gate turns) the seat banks gold it never "
                        "spends — the fix can starve purchases the old "
                        "gate would have made; that asymmetry is exactly "
                        "what batch-004 measures.",
    },
}

# The flash-proposed doctrine ids (research loop, iteration 000) — pinned so
# a transcription typo in DOCTRINES fails loudly here, not silently in a
# 16-game batch.
FLASH_ROSTER_IDS = ("hyperwide_flood", "granary_engine", "archer_horde",
                    "settler_broker")

_MILITARY = ("WARRIOR", "SPEARMAN", "ARCHER")
_CORNERS = [(4, 1), (1, 4), (-4, -1), (-1, -4)]
# radius-7 scaling of the scout corners (4*7//5)
_CORNERS_4 = [(5, 1), (1, 5), (-5, -1), (-1, -5)]
_MAP_ORIGIN = (0, 0)


def _march_target(pid: int, n_players: int,
                  my_start: tuple[int, int]) -> tuple[int, int]:
    """Military push destination. 2 seats: the historical duel-axis
    literals. 4 seats: the NEAREST foreign start (deterministic tie-break
    by seat id) — the seat-luck the research loop measures comes from
    contact timing, and this seam is where the batch-1 seat-2 anomaly
    (constant last place) was traced to."""
    if n_players == 2:
        return (4, -3) if pid == 0 else (-4, 3)
    others = [(hex_dist(my_start, start), other, start)
              for other, start in STARTS_4.items() if other != pid]
    return min(others)[2]


def _settler_target(pid: int, n_players: int, expand_ring: int) -> tuple[int, int]:
    """Settler push destination. 2 seats: historical literals. 4 seats:
    ``expand_ring + 2`` hexes from the own start along the start→origin
    line — the +2 guarantees the walk crosses the founding threshold
    (founding needs dist > 2 from any city) before reaching the target."""
    if n_players == 2:
        return (2, 2) if pid == 0 else (-2, -2)
    start = STARTS_4[pid]
    line = hex_line_greedy(start, _MAP_ORIGIN)
    return line[min(len(line) - 1, expand_ring + 2)]


def _corners(n_players: int) -> list[tuple[int, int]]:
    return _CORNERS if n_players == 2 else _CORNERS_4


def _coord_of(entity: dict[str, Any]) -> tuple[int, int]:
    return parse_key(entity["coord"])


def _steps_toward(unit: dict[str, Any], target: tuple[int, int]) -> list[str]:
    q, r = _coord_of(unit)
    candidates = sorted(
        neighbors(q, r), key=lambda n: (hex_dist(n, target), n)
    )
    return [f"{nq},{nr}" for nq, nr in candidates[:3]]


async def run_policy(runtime: Any, facade: Any,
                     doctrine: dict[str, Any] | None = None) -> None:
    profile = runtime.profile
    if doctrine is None:
        # default: the runtime's own doctrine (policy name == doctrine key)
        if profile.policy not in DOCTRINES:
            raise ValueError(
                f"policy {profile.policy!r} is not a doctrine; the caller "
                "must pass one explicitly (e.g. the adaptive switcher)")
        doctrine = DOCTRINES[profile.policy]
    rng: random.Random = runtime.rng
    pid = profile.player_id

    overview = await facade.get_overview()
    turn = overview["turn"]
    units = await facade.get_units()
    cities = await facade.get_cities()
    if turn % 7 == 1:
        await facade.get_visible_map()
    # seat-count-dependent geometry routes through one seam (2 seats =>
    # historical literals, bit-identical)
    n_players = len(overview.get("public", {}).get("players", [])) or 2
    my_start = STARTS_4[pid] if n_players != 2 else (0, 0)

    # research: first doctrine tech still available
    options = await facade.get_available_research()
    for pick in doctrine["research"]:
        if any(o["tech_id"] == pick for o in options):
            await facade.set_research(pick)
            break
    else:
        # Research overflow (batch-005's controlled rules change): the
        # doctrine list is exhausted by t20-30 and science previously
        # froze for the rest of the game in every batch; now it converts
        # to the cheapest remaining catalog tech (cost, then name —
        # deterministic) instead of idling.
        if options:
            overflow = min(options, key=lambda o: (o["cost"], o["tech_id"]))
            await facade.set_research(overflow["tech_id"])

    # cities: keep production going, occasionally purchase. ALWAYS set the
    # doctrine pick (idempotent — the read-back wants cur == item.Hash):
    # lease-start housekeeping fills empty queues BEFORE the policy runs,
    # and conditioning on an empty queue stopped this tool from ever
    # rehearsing (test_dispatch_rehearsal_end_to_end, regression from the
    # B2 housekeeping order — found 2026-09-03).
    for city in cities:
        opts = await facade.get_available_production(city["city_id"])
        item = _pick_build(opts, doctrine, len(cities))
        if item:
            await facade.set_city_production(city["city_id"], item)
        # H2 (research/RESEARCH-LEDGER.md): a flat 120 gate opens while
        # SETTLER (160) is unaffordable, and the next pref GRANARY (exactly
        # 120) drains the settler bank before it can ever reach 160 on a
        # gate turn. bank_floor raises the gate to the top preference's
        # cost, so when the gate opens the first affordable pick IS the top.
        floor = doctrine.get("bank_floor", 120)
        if turn % 3 == 0 and overview["you"]["gold"] >= floor:
            opts = await facade.get_available_production(city["city_id"])
            item = _pick_purchase(opts, doctrine)
            if item:
                await facade.purchase(
                    city["city_id"], item,
                    idempotency_key=f"buy-{turn}-{city['city_id']}-{rng.randint(0, 9999)}",
                )

    my_units = [u for u in units if u["owner_id"] == pid]
    foreigners = [u for u in units if u["owner_id"] != pid]
    settled = await _handle_settlers(facade, my_units, cities, profile,
                                     turn, n_players, doctrine)

    attacks_this_turn = 0
    aggression_cap = doctrine.get("aggression", 2)
    for unit in my_units:
        if unit["type"] == "SETTLER":
            continue  # handled above
        if unit["type"] in _MILITARY:
            target = _in_range_target(unit, foreigners) if foreigners else None
            if target and unit["movement"] > 0 and attacks_this_turn < aggression_cap:
                await facade.attack(unit["unit_id"], target["unit_id"])
                attacks_this_turn += 1
            elif doctrine["fortify_idle"] and not unit["fortified"]:
                await facade.fortify(unit["unit_id"])
            elif doctrine["march"] and unit["movement"] > 0:
                await _march(facade, unit, _march_target(pid, n_players, my_start))
        elif unit["type"] == "SCOUT" and unit["movement"] > 0:
            corners = _corners(n_players)
            corner = corners[rng.randint(0, len(corners) - 1)]
            await _march(facade, unit, corner)

    # TURN-COMPLETENESS GATE contract: on the structured unmoved_units
    # bounce, put a standing order on each listed unit and re-end — the
    # rehearsal exercises the same loop the LLM seats run live.
    res = await facade.end_turn()
    if isinstance(res, dict) and res.get("rejection") == "unmoved_units":
        for uid in res.get("unmoved_units") or []:
            await facade.fortify(uid)
        await facade.end_turn()
    _ = settled


def _in_range_target(unit: dict[str, Any],
                     foreigners: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Nearest foreign unit this unit can actually strike this turn."""
    if not foreigners:
        return None
    rng_max = 2 if unit["type"] == "ARCHER" else 1
    pos = _coord_of(unit)
    in_range = [
        f for f in foreigners
        if hex_dist(pos, _coord_of(f)) <= rng_max
    ]
    if not in_range:
        return None
    return min(in_range, key=lambda f: (hex_dist(pos, _coord_of(f)), f["unit_id"]))


async def _handle_settlers(facade: Any, my_units: list[dict], cities: list[dict],
                           profile: Any, turn: int, n_players: int,
                           doctrine: dict[str, Any]) -> int:
    founded = 0
    for unit in (u for u in my_units if u["type"] == "SETTLER"):
        pos = _coord_of(unit)
        near = any(hex_dist(pos, _coord_of(c)) <= 2 for c in cities)
        if not near:
            doc = await facade.found_city(
                unit["unit_id"], name=f"{profile.agent_id[:12]}-t{turn}")
            if doc.get("status") == "accepted":
                founded += 1
        else:
            await _march(facade, unit, _settler_target(
                profile.player_id, n_players, doctrine.get("expand_ring", 2)))
    return founded


async def _march(facade: Any, unit: dict[str, Any], target: tuple[int, int]) -> None:
    for dest in _steps_toward(unit, target):
        doc = await facade.move_unit(unit["unit_id"], dest)
        if doc.get("status") == "accepted":
            return


def _pick_build(opts: list[dict], doctrine: dict[str, Any], city_count: int) -> str | None:
    if city_count < doctrine["max_cities"]:
        wanted = ["SETTLER", *doctrine["build_order"]]
    else:
        wanted = doctrine["build_order"]
    by_id = {o["item_id"] for o in opts}
    for item in wanted:
        if item in by_id:
            return item
    return None


def _pick_purchase(opts: list[dict], doctrine: dict[str, Any]) -> str | None:
    by_id = {o["item_id"] for o in opts}
    for item in doctrine["purchase_pref"]:
        if item in by_id:
            return item
    return None
