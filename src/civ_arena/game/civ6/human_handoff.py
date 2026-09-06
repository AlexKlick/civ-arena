"""Keep native AI out of controlled turns on the tuner-safe single-player load.

SetLocalPlayerAndObserver demotes the outgoing seat. Switch only after that
seat is inactive, then reflag it for its next turn. GameCore human flags are
authoritative; InGame Players may retain stale flags after a configuration edit.
"""
from __future__ import annotations

import asyncio
import uuid


def _identity(player: int, turn: int, seats: tuple[int, ...]) -> str:
    if (type(player) is not int or type(turn) is not int or not 0 < turn < 2**53
            or not isinstance(seats, tuple) or not 2 <= len(seats) <= 4
            or any(type(p) is not int for p in seats)
            or seats != tuple(range(len(seats))) or player not in seats):
        raise ValueError("invalid human handoff identity")
    return "{" + ",".join(map(str, seats)) + "}"


def script(operation: str, player: int, turn: int, seats: tuple[int, ...], token: str) -> str:
    roster = _identity(player, turn, seats)
    if len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("invalid human handoff token")
    if operation not in {"activate", "reflag", "verify", "end"}:
        raise ValueError("invalid human handoff operation")
    prefix = f"HUMAN_HANDOFF|{token}|{operation}|{player}|{turn}"
    common = f"""
local seats={roster}
assert(Game.GetCurrentGameTurn()=={turn}, 'human handoff wrong turn')
assert(Players[{player}]:IsTurnActive(), 'human handoff target inactive')
"""
    if operation in {"activate", "verify"}:
        body = f"""
local s=Puppeteer.Status()
assert(s:find('PUPPET_ACTIVE|true',1,true)
  and s:find('LEASE_PLAYER|{player}\\n',1,true)
  and s:find('LEASE_TURN|{turn}\\n',1,true), 'human handoff wrong lease')
for _,pid in ipairs(seats) do
 assert(Players[pid]~=nil and Players[pid]:IsHuman(), 'human handoff nonhuman seat')
 if pid~={player} then
  assert(not Players[pid]:IsTurnActive(), 'human handoff outgoing active')
 end
end
"""
        if operation == "activate":
            body += f"""
if Game.GetLocalPlayer()~={player} then
 local old=Game.GetLocalPlayer()
 local controlled=false
 for _,pid in ipairs(seats) do if pid==old then controlled=true end end
 assert(controlled and not Players[old]:IsTurnActive(), 'human handoff unsafe switch')
 PlayerManager.SetLocalPlayerAndObserver({player})
end
"""
        body += f"assert(Game.GetLocalPlayer()=={player}, 'human handoff wrong local')\n"
    elif operation == "reflag":
        body = f"""
assert(Game.GetLocalPlayer()=={player}, 'human handoff wrong local')
assert(PlayerConfigurations[{player}]:IsHuman(), 'human handoff current not human')
-- Complete every precondition before changing any slot.
for _,pid in ipairs(seats) do
 assert(PlayerConfigurations[pid]~=nil and Players[pid]~=nil, 'human handoff missing seat')
 if pid~={player} then assert(not Players[pid]:IsTurnActive(), 'human handoff active reflag') end
end
for _,pid in ipairs(seats) do
 if pid~={player} and not PlayerConfigurations[pid]:IsHuman() then
  PlayerConfigurations[pid]:SetSlotStatus(SlotStatus.SS_TAKEN)
  Network.BroadcastPlayerInfo(pid)
 end
 assert(PlayerConfigurations[pid]:IsHuman(), 'human handoff configuration not human')
end
"""
    else:
        body = f"""
assert(Game.GetLocalPlayer()=={player}, 'human handoff wrong local')
for _,pid in ipairs(seats) do
 assert(PlayerConfigurations[pid]:IsHuman(), 'human handoff configuration not human')
 if pid~={player} then assert(not Players[pid]:IsTurnActive(), 'human handoff outgoing active') end
end
UI.RequestAction(ActionTypes.ACTION_ENDTURN)
"""
    return (f"-- arena:human_handoff={operation},{player},{turn},{token}\n"
            + common + body + f"print('{prefix}|observed')\nprint('---END---')\n")


async def _rpc(conn, operation, player, turn, seats):
    """Exactly one dispatch on the current connection; no reconnect or replay.

    The five-second bound includes lock wait and execution. A lost result is
    ambiguous and aborts the match. TapConnection retains the same wire path.
    """
    token = uuid.uuid4().hex
    lua = script(operation, player, turn, seats, token)
    state = "GameCore_Tuner" if operation in {"activate", "verify"} else "InGame"
    reader, writer = conn._reader, conn._writer
    try:
        async with asyncio.timeout(5):
            async with conn._lock:
                if (not conn.is_connected or conn._reader is not reader
                        or conn._writer is not writer):
                    raise RuntimeError("human handoff connection changed")
                indices = [i for i, name in conn.lua_states.items() if name == state]
                if len(indices) != 1:
                    raise RuntimeError("human handoff VM unavailable or ambiguous")
                rows = await conn._locked_execute(indices[0], lua, 5)
                expected = f"HUMAN_HANDOFF|{token}|{operation}|{player}|{turn}|observed"
                if rows != [expected]:
                    raise RuntimeError("human handoff receipt missing or mismatched")
    except BaseException:
        # A following game action must never reuse an ambiguous response stream.
        # Normal driver cleanup owns disconnect and its 20-second bound.
        raise
    return {"operation": operation, "status": "observed", "player": player, "turn": turn,
            "seats": list(seats), "receipt": expected}


async def _sequence(conn, player, turn, seats, operations, on_result):
    receipts = []
    for operation in operations:
        try:
            receipt = await _rpc(conn, operation, player, turn, seats)
        except BaseException as exc:
            if on_result is not None:
                on_result({"operation": operation, "status": "failed",
                           "player": player, "turn": turn, "seats": list(seats),
                           "error_type": type(exc).__name__,
                           "final_observation": "unavailable; effects may have occurred"})
            raise
        receipts.append(receipt)
        # Persist each positive receipt before starting any later operation.
        # Storage failure stops the sequence; it is not a native command failure.
        if on_result is not None:
            on_result(receipt)
    return receipts


async def activate(conn, player, turn, seats, *, on_result=None):
    return await _sequence(conn, player, turn, seats,
                           ("activate", "reflag", "verify"), on_result)


async def end_current(conn, player, turn, seats, *, on_result=None):
    return await _sequence(conn, player, turn, seats, ("verify", "end"), on_result)
