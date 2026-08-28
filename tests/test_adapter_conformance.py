"""Sim adapter conformance against the shared adapter-neutral contract suite."""

from __future__ import annotations

from civ_arena.game.conformance import assert_adapter_conformance
from civ_arena.game.sim.simulator import SimulatorAdapter
from civ_arena.game.sim.state import tile_key
from conftest import cmd, free_neighbor, own_units


async def _make(seed: int = 1) -> SimulatorAdapter:
    adapter = SimulatorAdapter()
    await adapter.setup({"seed": seed})
    return adapter


async def test_sim_adapter_conformance():
    # Preview adapter (same seed) computes deterministic probe inputs.
    preview = await _make(1)
    warrior = own_units(preview.state, 0, "WARRIOR")[0]
    dest = free_neighbor(preview.state, warrior["q"], warrior["r"])
    unit_id = warrior["unit_id"]
    await preview.teardown()

    probes = [
        {"cmd": cmd("move_unit", {"unit_id": unit_id, "dest": tile_key(*dest)}),
         "expect": "accepted"},
        # foreign unit (u6 is player 1's first settler)
        {"cmd": cmd("move_unit", {"unit_id": "u6", "dest": "0,0"}), "expect": "rejected"},
        # off-map destination
        {"cmd": cmd("move_unit", {"unit_id": unit_id, "dest": "99,99"}), "expect": "rejected"},
        {"cmd": cmd("fortify", {"unit_id": unit_id}), "expect": "accepted"},
    ]
    await assert_adapter_conformance(lambda: _make(1), probes)


async def test_act_outside_own_phase_rejected():
    adapter = await _make(2)
    await adapter.begin_phase(0, 1)
    enemy = own_units(adapter.state, 1, "WARRIOR")[0]
    from conftest import legal_move

    # player 1 acting inside player 0's phase
    move = legal_move(adapter.state, 1, enemy["unit_id"])
    res = await adapter.act(cmd("move_unit", move, player_id=1))
    assert res.status == "rejected" and res.rejection == "no_lease"
    await adapter.teardown()


async def test_begin_phase_turn_mismatch_raises():
    adapter = await _make(3)
    try:
        import pytest

        with pytest.raises(RuntimeError, match="turn mismatch"):
            await adapter.begin_phase(0, 99)
    finally:
        await adapter.teardown()
