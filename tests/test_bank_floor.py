"""H2's purchase-gate fix: bank_floor holds the gate at the top preference's
cost so the settler bank is never spent on the next pref (GRANARY, 120).

The mechanism (batch-002 trajectories, RESEARCH-LEDGER.md iteration 002):
the sim's legal purchase options are affordability-filtered, so at gold 130
SETTLER (160) is absent from opts and _pick_purchase's first hit is GRANARY.
Raising the gate to 160 makes SETTLER legal the moment the gate opens.
"""

from __future__ import annotations

from typing import Any

from civ_arena.agents.runtime import AgentProfile, ScriptedRuntime
from civ_arena.agents.scripted import DOCTRINES, run_policy

# rules.py purchase_cost = 2x production: SETTLER 160, GRANARY 120.
COSTS = {"SETTLER": 160, "GRANARY": 120, "MONUMENT": 120, "ARCHER": 120}


class PurchaseFacade:
    """One city, no units, empty research — just the purchase path. The
    second get_available_production call per city (inside the gate) returns
    affordability-filtered options, mirroring legal_actions."""

    def __init__(self, turn: int, gold: int) -> None:
        self._turn = turn
        self._gold = gold
        self.purchases: list[str] = []
        self._prod_calls = 0

    async def get_overview(self) -> dict[str, Any]:
        return {"turn": self._turn,
                "you": {"gold": self._gold, "researched": []},
                "public": {"players": [0, 1, 2, 3]}}

    async def get_units(self) -> list[dict[str, Any]]:
        return []

    async def get_cities(self) -> list[dict[str, Any]]:
        return [{"city_id": "c1", "q": 0, "r": 0, "population": 2,
                 "production_queue": ["MONUMENT"]}]

    async def get_available_research(self) -> list[dict[str, Any]]:
        return []

    async def get_available_production(self, _city_id: str) -> list[dict[str, Any]]:
        self._prod_calls += 1
        if self._prod_calls == 1:  # build poll (queue non-empty anyway)
            return []
        return [{"item_id": item} for item, cost in COSTS.items()
                if cost <= self._gold]

    async def set_city_production(self, *_a: Any, **_k: Any) -> None:
        pass

    async def purchase(self, _city_id: str, item: str, **_k: Any) -> None:
        self.purchases.append(item)

    async def end_turn(self) -> None:
        pass


async def drive(policy: str, turn: int, gold: int) -> list[str]:
    runtime = ScriptedRuntime(AgentProfile(
        agent_id="a", player_id=0, policy=policy, seed=7))
    facade = PurchaseFacade(turn, gold)
    await run_policy(runtime, facade)
    return facade.purchases


async def test_bank_floor_holds_the_gate_below_160() -> None:
    # THE H2 pin: gold 130 on a gate turn opens the old 120 gate and bought
    # GRANARY; the banked doctrine must not purchase at all.
    assert await drive("settler_broker_banked", turn=3, gold=130) == []


async def test_banked_gate_buys_the_settler_when_open() -> None:
    # gate open at 170: SETTLER is legal and first in purchase_pref.
    assert await drive("settler_broker_banked", turn=3, gold=170) == ["SETTLER"]


async def test_unbanked_broker_still_buys_granary_at_130() -> None:
    # the defect itself, pinned as the control arm: the unfixed doctrine
    # still opens at 120 and its first AFFORDABLE pref is GRANARY.
    assert await drive("settler_broker", turn=3, gold=130) == ["GRANARY"]


async def test_banked_doctrine_registered() -> None:
    from civ_arena.config import VALID_POLICIES
    assert "settler_broker_banked" in DOCTRINES
    assert "settler_broker_banked" in VALID_POLICIES
    assert DOCTRINES["settler_broker_banked"]["bank_floor"] == 160
