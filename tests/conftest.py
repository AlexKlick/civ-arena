"""Shared test fixtures/helpers: sim scenario builders and command factories."""

from __future__ import annotations

from typing import Any

from civ_arena.game.adapter import ActionCommand, ObserveKind, ObserveRequest
from civ_arena.game.sim.rules import check_action
from civ_arena.game.sim.simulator import SimulatorAdapter
from civ_arena.game.sim.state import TERRAIN, SimState, neighbors, tile_key

# turn-by-turn research picks so both players advance the tree deterministically
RESEARCH_PICKS: dict[int, list[str]] = {
    0: ["POTTERY", "ARCHERY", "WRITING", "MINING"],
    1: ["MINING", "MASONRY", "BRONZE_WORKING", "ANIMAL_HUSBANDRY"],
}


def cmd(
    tool: str, args: dict[str, Any], player_id: int = 0, key: str = "probe",
    lease: str = "l0",
) -> ActionCommand:
    return ActionCommand(
        tool=tool, args=args, player_id=player_id,
        idempotency_key=key, lease_id=lease,
    )


async def observe(adapter: SimulatorAdapter, kind: ObserveKind, player_id: int,
                  subject_id: str | None = None) -> Any:
    return await adapter.observe(
        ObserveRequest(kind=kind, player_id=player_id, subject_id=subject_id)
    )


def own_units(state: SimState, pid: int, type_: str | None = None) -> list[dict]:
    units = [
        u for u in state.units.values()
        if u["owner"] == pid and (type_ is None or u["type"] == type_)
    ]
    return sorted(units, key=lambda u: int(u["unit_id"][1:]))


def free_neighbor(state: SimState, q: int, r: int) -> tuple[int, int]:
    for nq, nr in sorted(neighbors(q, r)):
        tile = state.tile(nq, nr)
        if tile is None:
            continue
        if TERRAIN[tile["terrain"]]["move"] > 0 and not state.units_at(nq, nr):
            return nq, nr
    raise AssertionError("no free neighbor")


def legal_move(state: SimState, pid: int, unit_id: str) -> dict[str, Any] | None:
    unit = state.unit(unit_id)
    assert unit is not None
    for nq, nr in sorted(neighbors(unit["q"], unit["r"])):
        args = {"unit_id": unit_id, "dest": tile_key(nq, nr)}
        if check_action(state, pid, "move_unit", args) is None:
            return args
    return None


async def scenario_adapter(seed: int = 1, turns: int = 2) -> SimulatorAdapter:
    """A short played-out game: cities founded, research set, scouts moved."""
    adapter = SimulatorAdapter()
    await adapter.setup({"seed": seed})
    for turn in range(1, turns + 1):
        for pid in (0, 1):
            await adapter.begin_phase(pid, turn)
            state = adapter.state
            if turn == 1:
                settler = own_units(state, pid, "SETTLER")[0]
                await adapter.act(cmd("found_city", {"unit_id": settler["unit_id"]}, pid))
            picks = RESEARCH_PICKS[pid]
            tech = picks[(turn - 1) % len(picks)]
            if check_action(state, pid, "set_research", {"tech_id": tech}) is None:
                await adapter.act(cmd("set_research", {"tech_id": tech}, pid))
            if turn == 2:
                city_id = sorted(
                    c["city_id"] for c in state.cities.values() if c["owner"] == pid
                )[0]
                args = {"city_id": city_id, "item_id": "WARRIOR"}
                if check_action(state, pid, "set_city_production", args) is None:
                    await adapter.act(cmd("set_city_production", args, pid))
            scout = own_units(state, pid, "SCOUT")
            if scout:
                move = legal_move(state, pid, scout[0]["unit_id"])
                if move:
                    await adapter.act(cmd("move_unit", move, pid))
            await adapter.end_phase(pid, turn)
    return adapter


def teleport(state: SimState, unit_id: str, q: int, r: int,
             terrain: str | None = None) -> None:
    unit = state.unit(unit_id)
    assert unit is not None
    unit["q"], unit["r"] = q, r
    if terrain is not None:
        state.tiles[tile_key(q, r)]["terrain"] = terrain
