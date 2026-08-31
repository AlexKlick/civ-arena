"""One-shot civic/policy blocker resolver (M14d live recovery).

WHY: a fresh game completes CODE_OF_LAWS (free at start) during the
engine's end-of-turn processing; two ENDTURN_BLOCKING notifications then
await the local player — "Choose a Civic" + "Fill Policy Slot". The
arena's forced end-turn bypasses their popups and the WHOLE turn cycle
freezes (the AI's turn never starts; live-learned 2026-08-30, run 011).

Resolves both with the engine's own safe primitives:
- civic choice: GameCore ``SetProgressingCivic`` (upstream's gamecore
  route — the FORBIDDEN ``SetCivic`` is never emitted);
- policy fill: InGame ``UNLOCK_POLICIES`` + ``RequestPolicyChanges``
  (upstream's exact sequence).

Every fresh game needs this once before the first driven turn; each use
is recorded dated in docs/live-validation.md §6. Refuses while a lease
is engaged.
"""

from __future__ import annotations

import argparse
import asyncio

from civ_arena.game.civ6 import lua_translator, response_parser
from civ_arena.game.civ6.vendor.connection import GameConnection

BLOCKER_QUERY = """
local me = Game.GetLocalPlayer()
local list = NotificationManager.GetList(me)
local seen = {}
local found = 0
if list then
    for _, nid in ipairs(list) do
        local entry = NotificationManager.Find(me, nid)
        if entry and not entry:IsDismissed() then
            local bt = entry:GetEndTurnBlocking()
            if bt and bt ~= 0 then
                local typeName = "UNKNOWN"
                for k, v in pairs(EndTurnBlockingTypes) do
                    if v == bt then typeName = k break end
                end
                if not seen[typeName] then
                    seen[typeName] = true
                    print("BLOCKING|" .. typeName)
                    found = found + 1
                end
            end
        end
    end
end
if found == 0 then print("NONE") end
print("---END---")
"""

# GameCore: pick and set the first progressable civic (SetProgressingCivic,
# the safe primitive — NEVER SetCivic)
SET_CIVIC = """
local me = Game.GetLocalPlayer()
local cu = Players[me]:GetCulture()
for row in GameInfo.Civics() do
    -- upstream's gate: simply not-yet-researched (CanProgress is not a
    -- culture-object method — cost one resolver round to learn)
    local has = true
    pcall(function() has = cu:HasCivic(row.Index) end)
    if not has then
        cu:SetProgressingCivic(row.Index)
        print("CIVIC_SET|" .. row.CivicType)
        return
    end
end
print("CIVIC_FAIL|no-progressable-civic")
print("---END---")
"""

# InGame: fill every EMPTY policy slot with the first unlocked policy whose
# type matches (upstream's UNLOCK_POLICIES + RequestPolicyChanges sequence)
FILL_SLOTS = """
local me = Game.GetLocalPlayer()
local cu = Players[me]:GetCulture()
local slotTypeMap = {SLOT_ECONOMIC=0, SLOT_MILITARY=1, SLOT_DIPLOMATIC=2,
                     SLOT_WILDCARD=3, SLOT_GREAT_PERSON=4}
local n = 0
pcall(function() n = cu:GetNumPolicySlots() end)
if n <= 0 then print("SLOTS_NONE|0") print("---END---") return end
local addList = {}
local clearList = {}
local picks = {}
for i = 0, n - 1 do
    local cur = -1
    pcall(function() cur = cu:GetSlotPolicy(i) end)
    if cur == -1 or cur == nil or cur == 0 then
        local st = 3
        pcall(function() st = cu:GetSlotType(i) end)
        for row in GameInfo.Policies() do
            local unlocked = false
            pcall(function() unlocked = cu:IsPolicyUnlocked(row.Index) end)
            local ptype = slotTypeMap[row.GovernmentSlotType or "SLOT_WILDCARD"] or 3
            if unlocked and (ptype == st or st == 3 or ptype == 3) then
                table.insert(clearList, i)
                addList[i] = row.Hash
                table.insert(picks, i .. ":" .. row.PolicyType)
                break
            end
        end
    end
end
local count = 0
for _ in pairs(addList) do count = count + 1 end
if count == 0 then print("SLOTS_NONE|" .. count) print("---END---") return end
UI.RequestPlayerOperation(me, PlayerOperations.UNLOCK_POLICIES, {})
cu:RequestPolicyChanges(clearList, addList)
print("POLICIES_SET|" .. count .. "|" .. table.concat(picks, ","))
print("---END---")
"""


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4318)
    ap.add_argument("--preemptive", action="store_true",
                    help="also set civic + fill empty slots when NO blocker "
                         "is listed yet (the fresh-game turn-1 park: the "
                         "CODE_OF_LAWS blockers only APPEAR during "
                         "end-of-turn processing, when policy changes "
                         "already no-op — the only legal moment is NOW)")
    opts = ap.parse_args()
    conn = GameConnection(opts.host, opts.port)
    await conn.connect()

    status = response_parser.parse_kv_lines(
        await conn.execute_read(lua_translator.mod_status()))
    if status.get("PUPPET_ACTIVE") is True:
        print(f"REFUSED: lease engaged ({dict(status)})")
        await conn.disconnect()
        return 2

    async def blockers() -> list[str]:
        lines = await conn.execute_write(BLOCKER_QUERY)
        rows = response_parser._split_lines(lines)  # noqa: SLF001
        return [r for r in rows if r.startswith("BLOCKING|")]

    before = await blockers()
    print("blockers:", before or ["none"])
    if not any("CIVIC" in b for b in before) and not opts.preemptive:
        print("no civic blockers to resolve")
        await conn.disconnect()
        return 0

    if opts.preemptive or any(
            b.endswith("ENDTURN_BLOCKING_CIVIC") for b in before):
        out = await conn.execute_read(SET_CIVIC)
        print("civic:", [r for r in out if not r.endswith("---END---")])
    if opts.preemptive or any("FILL_CIVIC_SLOT" in b for b in before):
        out = await conn.execute_write(FILL_SLOTS)
        print("policies:", [r for r in out if not r.endswith("---END---")])
    after = await blockers()
    print("blockers after:", after or ["none"])
    await conn.disconnect()
    return 0 if not after else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
