"""Research overflow — batch-005's controlled rules change.

Every doctrine's list is the tier-1 catalog; it exhausts by t20-30 and
science froze for the rest of the game in batches 001-004. The fallback
converts science to the cheapest remaining catalog tech instead.
"""

from __future__ import annotations

from typing import Any

from civ_arena.agents.runtime import AgentProfile, ScriptedRuntime
from civ_arena.agents.scripted import DOCTRINES, run_policy
from civ_arena.game.sim.state import TECHS


class OverflowFacade:
    """One city, no units; research options are injected per test."""

    def __init__(self, turn: int, options: list[dict[str, Any]]) -> None:
        self._turn = turn
        self._options = options
        self.researched: list[str] = []

    async def get_overview(self) -> dict[str, Any]:
        return {"turn": self._turn, "you": {"gold": 0, "researched": []},
                "public": {"players": [0, 1, 2, 3]}}

    async def get_units(self) -> list[dict[str, Any]]:
        return []

    async def get_cities(self) -> list[dict[str, Any]]:
        return [{"city_id": "c1", "q": 0, "r": 0, "population": 2,
                 "production_queue": ["MONUMENT"]}]

    async def get_available_research(self) -> list[dict[str, Any]]:
        return self._options

    async def get_available_production(self, _city_id: str) -> list[dict[str, Any]]:
        return []

    async def set_research(self, tech: str) -> None:
        self.researched.append(tech)

    async def set_city_production(self, *_a: Any, **_k: Any) -> None:
        pass

    async def end_turn(self) -> None:
        pass


async def drive(policy: str, options: list[dict[str, Any]]) -> list[str]:
    runtime = ScriptedRuntime(AgentProfile(
        agent_id="a", player_id=0, policy=policy, seed=7))
    facade = OverflowFacade(25, options)
    await run_policy(runtime, facade)
    return facade.researched


def _opt(tech: str) -> dict[str, Any]:
    return {"tech_id": tech, "cost": TECHS[tech]["cost"],
            "prereq": list(TECHS[tech]["prereq"])}


async def test_exhausted_list_overflows_to_cheapest() -> None:
    # turtler's list is the whole tier-1 catalog; only tier-2 remains
    options = [_opt(t) for t in ("SAILING", "CURRENCY", "CONSTRUCTION",
                                 "HORSEBACK_RIDING")]
    assert await drive("turtler", options) == ["SAILING"]  # 50 < 60 < 70


async def test_tie_breaks_by_name_deterministically() -> None:
    options = [_opt(t) for t in ("CURRENCY", "CONSTRUCTION", "THE_WHEEL",
                                 "CALENDAR")]  # all cost 60
    assert await drive("turtler", options) == ["CALENDAR"]


async def test_doctrine_list_still_wins_over_overflow() -> None:
    # a list tech is available -> the doctrine preference order rules
    options = [_opt("SAILING"), _opt("MINING")]  # MINING is a list tech
    assert await drive("turtler", options) == ["MINING"]


async def test_no_options_means_no_research_call() -> None:
    assert await drive("turtler", []) == []


def test_tier2_catalog_is_sim_legal() -> None:
    tier1 = {"POTTERY", "MINING", "ANIMAL_HUSBANDRY", "ARCHERY", "WRITING",
             "MASONRY", "BRONZE_WORKING", "IRRIGATION"}
    for tech, spec in TECHS.items():
        assert spec["cost"] > 0 and isinstance(spec["prereq"], list)
        for p in spec["prereq"]:
            assert p in TECHS, (tech, p)
    assert tier1 | {"SAILING", "CONSTRUCTION", "CURRENCY", "THE_WHEEL",
                    "CALENDAR", "HORSEBACK_RIDING"} == set(TECHS)


def test_every_doctrine_list_still_catalog_complete() -> None:
    for doctrine_id, doctrine in DOCTRINES.items():
        for tech in doctrine["research"]:
            assert tech in TECHS, (doctrine_id, tech)
