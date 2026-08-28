"""Pure semantic→Lua translation (the live leg's only Lua source).

Every function returns Lua source text suitable for the vendored
GameConnection's execute_read/execute_write. Output is pipe-delimited
KEY|value lines terminated by the sentinel, matching the response_parser.
Adding live support for an arena tool = one entry here + one parser entry +
one FakeTunerServer canned test — zero arena-core changes.
"""

from __future__ import annotations


def poll_turn_state() -> str:
    """Upstream's turn-state probe (docs/agent-vs-agent.md `poll_turn_state`)."""
    return """
print("TS|1")
local me = Game.GetLocalPlayer()
print("TURN|" .. Game.GetCurrentGameTurn())
print("LOCAL|" .. me)
print("PUPPET_ACTIVE|" .. tostring(__puppet_turn_active or false))
print("---END---")
"""


def overview_read() -> str:
    """Minimal omniscient overview: turn + alive majors (referee scope only)."""
    return """
print("OV|1")
print("TURN|" .. Game.GetCurrentGameTurn())
local players = PlayerManager.GetAliveMajors()
local count = 0
for _, p in ipairs(players) do
    count = count + 1
    print("PLAYER|" .. p:GetID() .. "|" .. p:GetCivilizationTypeName())
end
print("ALIVE|" .. count)
print("---END---")
"""


def lua_error_probe() -> str:
    """Deliberately erroneous Lua used to verify the error path."""
    return "this is not ( valid lua"


_ = lua_error_probe  # exported for tests
