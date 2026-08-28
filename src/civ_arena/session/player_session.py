"""PlayerSession: binds an agent to a civilization, server-side.

The session holds the player identity; the agent only ever receives a
ToolFacade. This is the structural enforcement of "no agent-supplied
player_id" — there is no parameter to supply.
"""

from __future__ import annotations

from typing import Any, Protocol

from civ_arena.arena.turn_lease import TurnLease
from civ_arena.session.tools import SessionCtx, ToolFacade


class AgentRuntime(Protocol):
    """The seam scripted bots use now and LLM runtimes will use later."""

    async def take_turn(self, facade: ToolFacade) -> None: ...


class PlayerSession:
    def __init__(self, referee: Any, player_id: int, agent_id: str) -> None:
        self._referee = referee
        self._player_id = player_id
        self.agent_id = agent_id

    async def take_turn(self, lease: TurnLease, runtime: AgentRuntime) -> None:
        ctx = SessionCtx(
            referee=self._referee,
            player_id=self._player_id,
            agent_id=self.agent_id,
            lease=lease,
            turn=lease.turn,
        )
        facade = ToolFacade(ctx)
        await runtime.take_turn(facade)
