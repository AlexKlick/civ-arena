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


def mod_handshake() -> str:
    """PuppeteerMod capability handshake — the phase-1 live gate.

    Tolerates both mod shapes: v0.1's Handshake() RETURNS a multi-line
    string (printed here line-by-line so each row arrives as its own
    message), and a print-direct Handshake() needs no help. ``Puppeteer``
    being nil (mod absent/disabled) is an answer, not an error.
    """
    return """
if Puppeteer == nil then
    print("MOD_PRESENT|false")
else
    print("MOD_PRESENT|true")
    local hs = Puppeteer.Handshake()
    if hs ~= nil then
        for line in string.gmatch(hs, "[^\n]+") do print(line) end
    end
end
print("---END---")
"""


def mod_status() -> str:
    """Pollable puppet/lease state (mod >= 0.2 — D3: poll, never push).

    The vendored connection drains unsolicited messages around every
    command, so hook-time prints are unreliable; Status() is the only
    sound way to observe lease state.
    """
    return """
if Puppeteer == nil or Puppeteer.Status == nil then
    print("MOD_STATUS|unavailable")
else
    local s = Puppeteer.Status()
    if s ~= nil then
        for line in string.gmatch(s, "[^\n]+") do print(line) end
    end
end
print("---END---")
"""


def mod_digest() -> str:
    """Puppeteer.Digest() — the live before/after state digest rows."""
    return """
if Puppeteer == nil or Puppeteer.Digest == nil then
    print("MOD_DIGEST|unavailable")
else
    local d = Puppeteer.Digest()
    if d ~= nil then
        for line in string.gmatch(d, "[^\n]+") do print(line) end
    end
end
print("---END---")
"""


_ = lua_error_probe  # exported for tests
