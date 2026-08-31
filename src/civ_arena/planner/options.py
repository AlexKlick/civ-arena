"""M15d — the hand-authored option library (temporally extended strategies).

An Option is executable data over a belief-derived SimState: an initiation
predicate (may this strategy start here?), a termination predicate (is it
finished?), and a per-turn step compiler that emits a within-turn plan for
the M15c executor. Compilers draw ONLY from ``legal_actions`` output and
never emit MUTEX pairs, so a compiled step prevalidates clean by
construction. Everything is a pure function of (state, player_id) —
deterministic, integer-only, no I/O.

Options that never terminate on their own (economy, fortify_line) are
ended by the runtime's reselection cadence — the M16 interrupt vocabulary
lands later.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from civ_arena.game.sim.rules import legal_actions
from civ_arena.game.sim.state import (
    BUILDINGS,
    TECHS,
    TERRAIN,
    SimState,
    hex_dist,
    parse_key,
)

Plan = list[tuple[str, dict[str, Any]]]


@dataclass(frozen=True)
class Option:
    option_id: str
    initiation: Callable[[SimState, int], bool]
    termination: Callable[[SimState, int], bool]
    compile_step: Callable[[SimState, int], Plan]


# ---------------------------------------------------------------- helpers


def _by_tool(state: SimState, pid: int) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    out: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for tool, args in legal_actions(state, pid):
        out.setdefault(tool, []).append((tool, args))
    return out


def _own_units(state: SimState, pid: int, types: tuple[str, ...] | None = None) -> list[dict]:
    return sorted(
        (u for u in state.units.values() if u["owner"] == pid
         and (types is None or u["type"] in types)),
        key=lambda u: int(u["unit_id"][1:]))


def _own_cities(state: SimState, pid: int) -> list[dict]:
    return sorted((c for c in state.cities.values() if c["owner"] == pid),
                  key=lambda c: int(c["city_id"][1:]))


def _military(state: SimState, pid: int) -> list[dict]:
    return [u for u in _own_units(state, pid)
            if u["strength"] > 0 or u["ranged_strength"] > 0]


def _foreign_coords(state: SimState, pid: int) -> list[tuple[int, int]]:
    out = [(u["q"], u["r"]) for u in state.units.values() if u["owner"] != pid]
    out += [(c["q"], c["r"]) for c in state.cities.values() if c["owner"] != pid]
    return sorted(out)


def _move_toward(state: SimState, pid: int, by_tool: dict, unit: dict,
                 target: tuple[int, int]) -> Plan:
    """The unit's legal SIGHT-BOUNDED move that most reduces distance to
    target (ties by sorted dest); empty when no in-sight move improves.

    Restricting dests to currently-observable tiles is the fog-safe creep:
    every enemy on an observable tile is in the belief, so an enumerated
    in-sight move can never be rejected OCCUPIED by an unseen unit — the
    one rejection class prevalidation cannot otherwise absorb.
    """
    sight = state.sight_tiles(pid)
    here = (unit["q"], unit["r"])
    best: tuple[int, str] | None = None
    for _, args in by_tool.get("move_unit", []):
        if args["unit_id"] != unit["unit_id"] or args["dest"] not in sight:
            continue
        dest = parse_key(args["dest"])
        d = hex_dist(dest, target)
        if best is None or (d, args["dest"]) < best:
            best = (d, args["dest"])
    if best is None or best[0] >= hex_dist(here, target):
        return []
    return [("move_unit", {"unit_id": unit["unit_id"], "dest": best[1]})]


def _research_step(state: SimState, pid: int, by_tool: dict) -> Plan:
    if state.player(pid)["researching"] == "" and by_tool.get("set_research"):
        return [by_tool["set_research"][0]]
    return []


def _production_step(state: SimState, pid: int, by_tool: dict,
                     preference: tuple[str, ...]) -> Plan:
    plan: Plan = []
    idle = {c["city_id"] for c in _own_cities(state, pid)
            if not c["production_queue"]}
    for city_id in sorted(idle, key=lambda c: int(c[1:])):
        choices = [(t, a) for t, a in by_tool.get("set_city_production", [])
                   if a["city_id"] == city_id]
        ranked = sorted(
            choices,
            key=lambda ta: (preference.index(ta[1]["item_id"])
                            if ta[1]["item_id"] in preference else len(preference),
                            ta[1]["item_id"]))
        if ranked:
            plan.append(ranked[0])
    return plan


def _fortify_step(state: SimState, pid: int, by_tool: dict,
                  exclude: set[str]) -> Plan:
    plan: Plan = []
    for tool, args in by_tool.get("fortify", []):
        unit = state.unit(args["unit_id"])
        if (unit and args["unit_id"] not in exclude
                and (unit["strength"] > 0 or unit["ranged_strength"] > 0)):
            plan.append((tool, args))
    return plan


def _planned_units(plan: Plan) -> set[str]:
    return {a.get("unit_id") for _, a in plan if "unit_id" in a}


def _found_targets(state: SimState, pid: int) -> list[tuple[int, int]]:
    """Foundable spots: unowned passable tiles >2 from every known city."""
    out = []
    for key, tile in state.tiles.items():
        if tile["owner"] != -1:
            continue
        if TERRAIN[tile["terrain"]]["move"] == 0:
            continue
        q, r = parse_key(key)
        if all(hex_dist((c["q"], c["r"]), (q, r)) > 2
               for c in state.cities.values()):
            out.append((q, r))
    return sorted(out)


# ---------------------------------------------------------------- options


def _expand_step(state: SimState, pid: int) -> Plan:
    by_tool = _by_tool(state, pid)
    plan: Plan = []
    if by_tool.get("found_city"):
        plan.append(by_tool["found_city"][0])
    targets = _found_targets(state, pid)
    for settler in _own_units(state, pid, ("SETTLER",)):
        if settler["unit_id"] in _planned_units(plan) or not targets:
            continue
        here = (settler["q"], settler["r"])
        target = min(targets, key=lambda t: (hex_dist(here, t), t))
        plan += _move_toward(state, pid, by_tool, settler, target)
    if not _own_units(state, pid, ("SETTLER",)):
        plan += _production_step(state, pid, by_tool, ("SETTLER",))
    plan += _research_step(state, pid, by_tool)
    plan += _fortify_step(state, pid, by_tool, _planned_units(plan))
    return plan


def _develop_step(state: SimState, pid: int) -> Plan:
    by_tool = _by_tool(state, pid)
    plan = _production_step(state, pid, by_tool, ("MONUMENT", "GRANARY", "WALLS"))
    plan += _research_step(state, pid, by_tool)
    plan += _fortify_step(state, pid, by_tool, set())
    return plan


def _threat_within(state: SimState, pid: int, radius: int) -> bool:
    own = [(c["q"], c["r"]) for c in _own_cities(state, pid)]
    own += [(u["q"], u["r"]) for u in _own_units(state, pid)]
    foreign = [(u["q"], u["r"]) for u in state.units.values()
               if u["owner"] != pid and (u["strength"] > 0 or u["ranged_strength"] > 0)]
    return any(hex_dist(a, b) <= radius for a in own for b in foreign)


def _defend_step(state: SimState, pid: int) -> Plan:
    by_tool = _by_tool(state, pid)
    plan: Plan = []
    for _, args in by_tool.get("attack", []):
        if args["unit_id"] not in _planned_units(plan):
            plan.append(("attack", args))
    plan += _production_step(state, pid, by_tool, ("WALLS", "WARRIOR"))
    gold = state.player(pid)["gold"]
    buys = [(t, a) for t, a in by_tool.get("purchase", [])
            if a["item_id"] == "WARRIOR"]
    if gold >= 160 and buys:
        plan.append(buys[0])
    plan += _research_step(state, pid, by_tool)
    plan += _fortify_step(state, pid, by_tool, _planned_units(plan))
    return plan


def _rush_step(state: SimState, pid: int) -> Plan:
    by_tool = _by_tool(state, pid)
    plan: Plan = []
    foreign = _foreign_coords(state, pid)
    for unit in _military(state, pid):
        attacks = [(t, a) for t, a in by_tool.get("attack", [])
                   if a["unit_id"] == unit["unit_id"]]
        if attacks:
            plan.append(attacks[0])
        elif foreign:
            here = (unit["q"], unit["r"])
            target = min(foreign, key=lambda t: (hex_dist(here, t), t))
            plan += _move_toward(state, pid, by_tool, unit, target)
    plan += _production_step(state, pid, by_tool, ("WARRIOR",))
    plan += _research_step(state, pid, by_tool)
    return plan


def _frontier_targets(state: SimState, pid: int) -> list[tuple[int, int]]:
    known = state.revealed_keys(pid)
    return sorted(parse_key(k) for k in state.tiles if k not in known)


def _scout_step(state: SimState, pid: int) -> Plan:
    by_tool = _by_tool(state, pid)
    plan: Plan = []
    frontier = _frontier_targets(state, pid)
    for scout in _own_units(state, pid, ("SCOUT",)):
        if frontier:
            here = (scout["q"], scout["r"])
            target = min(frontier, key=lambda t: (hex_dist(here, t), t))
            plan += _move_toward(state, pid, by_tool, scout, target)
    plan += _research_step(state, pid, by_tool)
    plan += _production_step(state, pid, by_tool, ("MONUMENT",))
    plan += _fortify_step(state, pid, by_tool, _planned_units(plan))
    return plan


def _tech_step(state: SimState, pid: int) -> Plan:
    by_tool = _by_tool(state, pid)
    plan = _research_step(state, pid, by_tool)
    plan += _production_step(state, pid, by_tool, ("MONUMENT", "GRANARY"))
    plan += _fortify_step(state, pid, by_tool, set())
    return plan


def _building_buy(state: SimState, plan: Plan, by_tool: dict, item: str) -> Plan:
    """A purchase of ``item`` whose city is NOT already producing it (queued
    or planned this turn) — the engine appends duplicates on completion, so
    buy-what-you-queued would double the building and its gold yield."""
    producing = {a["city_id"] for t, a in plan
                 if t == "set_city_production" and a["item_id"] == item}
    for t, a in by_tool.get("purchase", []):
        if a["item_id"] != item or a["city_id"] in producing:
            continue
        city = state.city(a["city_id"])
        if city is not None and item in city["production_queue"]:
            continue
        return [(t, a)]
    return []


def _economy_step(state: SimState, pid: int) -> Plan:
    by_tool = _by_tool(state, pid)
    plan = _production_step(state, pid, by_tool, ("GRANARY", "MONUMENT"))
    if state.player(pid)["gold"] >= 200:
        plan += _building_buy(state, plan, by_tool, "MONUMENT")
    plan += _research_step(state, pid, by_tool)
    plan += _fortify_step(state, pid, by_tool, set())
    return plan


def _fortify_line_step(state: SimState, pid: int) -> Plan:
    by_tool = _by_tool(state, pid)
    plan = _fortify_step(state, pid, by_tool, set())
    plan += _research_step(state, pid, by_tool)
    plan += _production_step(state, pid, by_tool, ("WALLS", "WARRIOR"))
    return plan


def _all_buildings_done(state: SimState, pid: int) -> bool:
    cities = _own_cities(state, pid)
    return bool(cities) and all(
        set(BUILDINGS) <= set(c["buildings"]) for c in cities)


OPTIONS: dict[str, Option] = {o.option_id: o for o in (
    Option("expand",
           lambda s, p: len(_own_cities(s, p)) < 3,
           lambda s, p: len(_own_cities(s, p)) >= 3,
           _expand_step),
    Option("develop",
           lambda s, p: bool(_own_cities(s, p)),
           _all_buildings_done,
           _develop_step),
    Option("defend",
           lambda s, p: _threat_within(s, p, 4),
           lambda s, p: not _threat_within(s, p, 6),
           _defend_step),
    Option("rush",
           lambda s, p: len(_military(s, p)) >= 2 and bool(_foreign_coords(s, p)),
           # units only: the sim has no city capture, so a known foreign
           # CITY would make a coords-based termination unreachable forever
           lambda s, p: not any(u["owner"] != p for u in s.units.values())
           or not _military(s, p),
           _rush_step),
    Option("scout_frontier",
           lambda s, p: bool(_own_units(s, p, ("SCOUT",)))
           and bool(_frontier_targets(s, p)),
           lambda s, p: not _own_units(s, p, ("SCOUT",))
           or not _frontier_targets(s, p),
           _scout_step),
    Option("tech_race",
           lambda s, p: True,
           lambda s, p: len(s.player(p)["researched"]) == len(TECHS),
           _tech_step),
    Option("economy",
           lambda s, p: bool(_own_cities(s, p)),
           lambda s, p: False,
           _economy_step),
    Option("fortify_line",
           lambda s, p: bool(_military(s, p)),
           lambda s, p: False,
           _fortify_line_step),
)}
