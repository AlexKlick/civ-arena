"""The 15 agent-visible tools. NO tool takes a player/owner identity parameter.

Identity is bound server-side: each function receives an opaque SessionCtx
whose player the referee injected at lease grant. An LLM AgentRuntime later
maps tool-schema calls onto this same registry — the seam stays stable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from civ_arena.arena.turn_lease import TurnLease
from civ_arena.game.adapter import ObserveKind


@dataclass
class SessionCtx:
    """Opaque per-turn context. NEVER handed to an agent — only to tools."""

    referee: Any
    player_id: int
    agent_id: str
    lease: TurnLease | None
    turn: int


# -- observations -----------------------------------------------------------

async def get_overview(ctx: SessionCtx) -> dict:
    return await ctx.referee.observe(ctx, ObserveKind.OVERVIEW)


async def get_units(ctx: SessionCtx) -> list[dict]:
    return await ctx.referee.observe(ctx, ObserveKind.UNITS)


async def get_cities(ctx: SessionCtx) -> list[dict]:
    return await ctx.referee.observe(ctx, ObserveKind.CITIES)


async def get_visible_map(ctx: SessionCtx) -> dict:
    return await ctx.referee.observe(ctx, ObserveKind.VISIBLE_MAP)


async def get_available_research(ctx: SessionCtx) -> list[dict]:
    return await ctx.referee.observe(ctx, ObserveKind.AVAILABLE_RESEARCH)


async def get_available_production(ctx: SessionCtx, city_id: str) -> list[dict]:
    return await ctx.referee.observe(ctx, ObserveKind.AVAILABLE_PRODUCTION,
                                     subject_id=city_id)


# -- actions -------------------------------------------------------------------

async def move_unit(ctx: SessionCtx, unit_id: str, dest: str,
                    *, idempotency_key: str | None = None) -> dict:
    return await ctx.referee.execute(
        ctx, "move_unit", {"unit_id": unit_id, "dest": dest},
        client_key=idempotency_key)


async def attack(ctx: SessionCtx, unit_id: str, target_id: str,
                 *, idempotency_key: str | None = None) -> dict:
    return await ctx.referee.execute(
        ctx, "attack", {"unit_id": unit_id, "target_id": target_id},
        client_key=idempotency_key)


async def fortify(ctx: SessionCtx, unit_id: str,
                  *, idempotency_key: str | None = None) -> dict:
    return await ctx.referee.execute(ctx, "fortify", {"unit_id": unit_id},
                                     client_key=idempotency_key)


async def found_city(ctx: SessionCtx, unit_id: str, name: str | None = None,
                     *, idempotency_key: str | None = None) -> dict:
    args: dict[str, Any] = {"unit_id": unit_id}
    if name is not None:
        args["name"] = name
    return await ctx.referee.execute(ctx, "found_city", args,
                                     client_key=idempotency_key)


async def set_research(ctx: SessionCtx, tech_id: str,
                       *, idempotency_key: str | None = None) -> dict:
    return await ctx.referee.execute(ctx, "set_research", {"tech_id": tech_id},
                                     client_key=idempotency_key)


async def set_city_production(ctx: SessionCtx, city_id: str, item_id: str,
                              *, idempotency_key: str | None = None) -> dict:
    return await ctx.referee.execute(
        ctx, "set_city_production", {"city_id": city_id, "item_id": item_id},
        client_key=idempotency_key)


async def purchase(ctx: SessionCtx, city_id: str, item_id: str,
                   *, idempotency_key: str | None = None) -> dict:
    return await ctx.referee.execute(
        ctx, "purchase", {"city_id": city_id, "item_id": item_id},
        client_key=idempotency_key)


async def end_turn(ctx: SessionCtx) -> dict:
    return await ctx.referee.end_turn(ctx)


async def write_diary(ctx: SessionCtx, text: str) -> dict:
    """Cross-turn memory note. Not a game action: no state change, no
    mutations — the referee stores it per-player and it is fed back to this
    agent (and only this agent) at the next turn's start."""
    return await ctx.referee.write_diary(ctx, text)


TOOL_REGISTRY: dict[str, Callable[..., Any]] = {
    "get_overview": get_overview,
    "get_units": get_units,
    "get_cities": get_cities,
    "get_visible_map": get_visible_map,
    "get_available_research": get_available_research,
    "get_available_production": get_available_production,
    "move_unit": move_unit,
    "attack": attack,
    "fortify": fortify,
    "found_city": found_city,
    "set_research": set_research,
    "set_city_production": set_city_production,
    "purchase": purchase,
    "end_turn": end_turn,
    "write_diary": write_diary,
}


def _bind(fn: Callable[..., Any], ctx: SessionCtx) -> Callable[..., Any]:
    """Closure binding: the ctx must NOT be stored as an attribute (a facade
    handed to arbitrary runtime code exposes every attribute, underscore or
    not). A closure keeps the reference off the attribute surface entirely;
    `functools.partial` would expose it via `.args`."""

    async def bound(*args: Any, **kwargs: Any) -> Any:
        return await fn(ctx, *args, **kwargs)

    return bound


class ToolFacade:
    """The object an agent runtime receives: bound tools, nothing else.

    No attribute of this object is (or reaches) the referee, the adapter, or
    any player identity. Deep reflection into closure cells is outside the
    spike's threat model (LLM tool-callers, not hostile Python); the
    attribute surface is what the tests pin."""

    def __init__(self, ctx: SessionCtx) -> None:
        self._bound = {name: _bind(fn, ctx) for name, fn in TOOL_REGISTRY.items()}

    def names(self) -> list[str]:
        return sorted(self._bound)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self.__dict__["_bound"][name]
        except KeyError:
            raise AttributeError(f"no such tool: {name}") from None

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(self._bound))
