"""Adapter-neutral conformance contract shared by sim (now) and live (later).

The suite checks what every GameAdapter must guarantee: phase ordering,
journal completeness, hash stability, snapshot/restore and export/import
round-trips, and the rejection taxonomy — without assuming any game content.
Caller supplies probe commands appropriate for the adapter under test.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


async def assert_adapter_conformance(
    setup: Callable[[], Awaitable[Any]],
    probes: list[dict[str, Any]],
    seed: int = 1,
) -> None:
    """Run the contract suite on a fresh adapter from ``setup()``.

    ``probes`` is a list of ``{"cmd": ActionCommand, "expect": "accepted" |
    "rejected"}`` evaluated in order inside player-0's first phase.
    """
    adapter = await setup()
    try:
        _ = seed
        caps = adapter.capabilities()
        assert caps.acts, "conformance suite requires an acting adapter"
        assert caps.state_hash and caps.save_load and caps.rollback, (
            "sim-class adapters must support hash/save/rollback"
        )

        # hash stability without acts
        h1, h2 = adapter.state_hash(), adapter.state_hash()
        assert h1 == h2, "state_hash must be stable across calls"

        # act outside any phase is rejected, not raised
        res = await adapter.act(probes[0]["cmd"])
        assert res.status == "rejected", "act with no open phase must reject"

        # begin_phase returns a manifest; double begin raises
        info = await adapter.begin_phase(0, 1)
        manifest = info["manifest"]
        try:
            await adapter.begin_phase(0, 1)
            raise AssertionError("double begin_phase must raise")
        except RuntimeError:
            pass

        # the journal drains exactly the declared manifest at a phase boundary
        drained = adapter.drain_mutations()
        assert [m.to_doc() for m in drained] == manifest, (
            "journal at begin_phase must equal the declared manifest"
        )

        # probes
        for probe in probes:
            pre = adapter.state_hash()
            res = await adapter.act(probe["cmd"])
            assert res.status == probe["expect"], (
                f"probe {probe['cmd'].tool}: expected {probe['expect']}, "
                f"got {res.status} ({res.rejection})"
            )
            if res.status == "accepted":
                assert res.mutations, "accepted actions must journal mutations"
                assert adapter.state_hash() != pre, "accepted action must change state"

        # snapshot/restore round-trip
        snap = adapter.snapshot()
        h_before = adapter.state_hash()
        adapter.restore(snap)
        assert adapter.state_hash() == h_before

        # export/import round-trip
        doc = adapter.export_state()
        h_doc = adapter.state_hash()
        adapter.import_state(doc)
        assert adapter.state_hash() == h_doc

        # phase ordering through a full turn
        await adapter.end_phase(0, 1)
        await adapter.begin_phase(1, 1)
        await adapter.end_phase(1, 1)
        phase = await adapter.current_phase()
        assert phase["turn"] == 2 and phase["phase_index"] == 0, (
            f"turn must advance after all phases: {phase}"
        )
    finally:
        await adapter.teardown()
