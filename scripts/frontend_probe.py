"""M17b — front-end (main-menu) state probe for the zero-touch ladder.

At the MAIN MENU the tuner exposes no GameCore_Tuner/InGame states — only
``Main State`` (the front-end Lua VM) and ``DebugHotloadCache``. The
live-driver smoke therefore CANNOT pass S1 at the menu, and the vendor
connection's auto-reconnect trips the tuner's single-client limit, which
masks the real condition as "Cannot connect ... EnableTuner=1?".

This probe works on ONE connection (the tuner services a single client;
rapid reconnects are refused) and asks Main State what it can do — the
zero-touch question is whether the front-end VM exposes the game-hosting
API (Network / GameConfiguration / MapConfiguration), i.e. whether a game
can be created without a human click.

    uv run python scripts/frontend_probe.py                     # states + globals
    uv run python scripts/frontend_probe.py --probe types       # API surface check
    uv run python scripts/frontend_probe.py --probe keys --symbol Network
    uv run python scripts/frontend_probe.py --lua snippet.lua --state 0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from civ_arena.game.civ6.vendor import SENTINEL, tuner_client
from civ_arena.game.civ6.vendor.connection import GameConnection, _parse_output

# API symbols the hosting path needs (Civ VI front-end Lua) — checked for
# presence + type in Main State. Anything absent is reported, not assumed.
CANDIDATE_APIS = [
    "Network", "GameConfiguration", "MapConfiguration", "GameTypes",
    "PlayerConfigurations", "UI", "LuaEvents", "GameEvents", "ContextPtr",
    "Automation", "GameSetup", "FrontEnd", "Modding", "GameState",
    "GameInfo", "Database", "LeaderManager", "Ruleset",
]

KEYWORD_FILTER = ("etwork", "onfiguration", "aunch", "Host", "utomation",
                  "GameType", "Setup", "Modding", "LuaEvents", "FrontEnd")

# UI contexts are sandboxed: _G is nil there. Resolve the environment
# through the Lua 5.4 _ENV upvalue, then the 5.1 getfenv, before giving up.
ENV_RESOLVE = """
local env = _G
if env == nil and _ENV ~= nil then env = _ENV end
if env == nil and getfenv ~= nil then env = getfenv() end
if env == nil then print("ENV|UNRESOLVABLE") print("{sent}") return end
"""

GLOBALS_LUA = f"""
{ENV_RESOLVE.format(sent=SENTINEL)}
local keys = {{}}
for k, v in pairs(env) do table.insert(keys, k) end
table.sort(keys)
for _, n in ipairs(keys) do
  local hit = false
  for _, kw in ipairs({{ {", ".join(repr(k) for k in KEYWORD_FILTER)} }}) do
    if string.find(n, kw, 1, true) then hit = true break end
  end
  if hit then print(n .. "|" .. type(env[n])) end
end
print("{SENTINEL}")
"""

TYPES_LUA = f"""
{ENV_RESOLVE.format(sent=SENTINEL)}
for _, n in ipairs({{ {", ".join(repr(a) for a in CANDIDATE_APIS)} }}) do
  local v = env[n]
  if v == nil then print(n .. "|MISSING")
  else
    local members = ""
    if type(v) == "table" then
      local names = {{}}
      for k in pairs(v) do table.insert(names, tostring(k)) end
      table.sort(names)
      if #names > 0 then members = table.concat(names, ","):sub(1, 400) end
    end
    print(n .. "|" .. type(v) .. "|" .. members)
  end
end
print("{SENTINEL}")
"""

KEYS_LUA = """
{env_resolve}
local sym = env["{sym}"]
if sym == nil then print("MISSING") print("{sent}") return end
local names = {{}}
for k in pairs(sym) do table.insert(names, tostring(k)) end
table.sort(names)
for _, n in ipairs(names) do print(n) end
print("{sent}")
"""


def keys_lua(symbol: str) -> str:
    return KEYS_LUA.format(sym=symbol, sent=SENTINEL,
                           env_resolve=ENV_RESOLVE.format(sent=SENTINEL))


# Sandboxed UI contexts expose neither _G nor _ENV nor getfenv — but a BARE
# name still resolves through the environment chain at compile time, so
# static per-symbol probes work where dynamic lookup cannot.
STATIC_LUA = "\n".join(
    f'if {name} ~= nil then print("{name}|" .. type({name})) '
    f'else print("{name}|MISSING") end'
    for name in CANDIDATE_APIS + ["NetworkMode", "GameModeTypes",
                                  "Version", "Configuration"]
) + f'\nprint("{SENTINEL}")\n'


def skeys_lua(symbol: str) -> str:
    """pairs() over a BARE-NAME table — works in sandboxed contexts where
    the env table itself is unreachable but bare names resolve."""
    return f"""
local t = {symbol}
if t == nil then print("MISSING") print("{SENTINEL}") return end
local ok, err = pcall(function()
  local names = {{}}
  for k, v in pairs(t) do table.insert(names, tostring(k) .. "=" .. tostring(v)) end
  table.sort(names)
  for _, n in ipairs(names) do print(n) end
end)
if not ok then print("PAIRS-FAILED|" .. tostring(err)) end
print("{SENTINEL}")
"""


async def run(host: str, port: int, state_spec: str, lua: str,
              as_json: bool) -> int:
    conn = GameConnection(host, port)
    await conn.connect()
    states = conn.lua_states
    if not as_json:
        print(f"APP states: {states}", file=sys.stderr)

    if state_spec.isdigit():
        idx = int(state_spec)
    else:
        exact = {i: n for i, n in states.items() if n == state_spec}
        matches = exact or {i: n for i, n in states.items()
                            if state_spec in n}
        if not matches:
            print(f"no state matching {state_spec!r}: {states}",
                  file=sys.stderr)
            return 2
        idx = min(matches)
    name = states.get(idx, "?")

    # collect parsed O-lines until the sentinel (single connection; the
    # tuner serves one client and refuses rapid reconnects)
    lines: list[str] = []
    await tuner_client.drain_messages(conn._reader, timeout=0.2)  # noqa: SLF001
    await tuner_client.send_message(
        conn._writer, tuner_client.TAG_COMMAND, f"CMD:{idx}:{lua}")  # noqa: SLF001
    for _ in range(200):
        msg = await tuner_client.recv_message_timeout(conn._reader, timeout=2.0)
        if msg is None:
            break
        if msg.payload.startswith("ERR:"):
            print(f"LuaError in {name}: {msg.payload}", file=sys.stderr)
            break
        text = _parse_output(msg.payload)
        if text is None:
            continue
        if text.strip() == SENTINEL:
            break
        lines.append(text)
    await conn.disconnect()

    if as_json:
        print(json.dumps({"state": name, "index": idx, "lines": lines},
                         sort_keys=True))
    else:
        print(f"-- state {idx} ({name}), {len(lines)} rows")
        for ln in lines:
            print(ln)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4318)
    ap.add_argument("--state", default="Main",
                    help="state name substring or numeric index")
    ap.add_argument("--probe", default="globals",
                    choices=("globals", "types", "keys", "static", "skeys"))
    ap.add_argument("--symbol", default="Network",
                    help="probe=keys: enumerate this table's keys")
    ap.add_argument("--lua-file", type=Path, default=None,
                    help="run arbitrary Lua (trailing print of the sentinel "
                         "terminates collection)")
    ap.add_argument("--json", action="store_true")
    opts = ap.parse_args()

    if opts.lua_file is not None:
        lua = opts.lua_file.read_text(encoding="utf-8")
    elif opts.probe == "types":
        lua = TYPES_LUA
    elif opts.probe == "keys":
        lua = keys_lua(opts.symbol)
    elif opts.probe == "static":
        lua = STATIC_LUA
    elif opts.probe == "skeys":
        lua = skeys_lua(opts.symbol)
    else:
        lua = GLOBALS_LUA
    try:
        return asyncio.run(run(opts.host, opts.port, opts.state, lua,
                               opts.json))
    except ConnectionError as e:
        print(f"CONNECT FAILED: {e}", file=sys.stderr)
        return 10


if __name__ == "__main__":
    sys.exit(main())
