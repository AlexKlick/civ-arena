-- PuppeteerMod.lua — UNVALIDATED DRAFT — never run against a real save
-- without following docs/live-validation.md first.
--
-- Turn-interception mod for referee-driven agent play. This draft follows
-- the civ-arena watchdog contract (docs/design-notes.md):
--   * ambient effects are DECLARED at phase boundaries (ambient ledger),
--     never inferred from mutation shape;
--   * authorization comes from the coordinator's ledger — DumpLedger is
--     what the Python referee diffs, not a self-reported "authorized" bit;
--   * the freeze captures per-unit snapshots and restore is PER-UNIT and
--     referee-driven. The upstream proposal's FinishMoves-then-immediate-
--     RestoreMovement sequence is EXPLICITLY REJECTED: restoring movement
--     before the stock AI finishes processing hands control back to the AI.
--
-- v0.2 (M14b):
--   * Status() — POLLABLE lease state. The tuner wire drains unsolicited
--     output around every command, so hook-time prints never arrive;
--     every wait must poll Status() instead (docs: D3, poll never push).
--   * ambient recorder — BeginAmbientWindow snapshots the player, the
--     EndAmbientWindow diff is booked as the DECLARED manifest; Release
--     re-diffs against the lease snapshot and books any remaining drift
--     into the command ledger (undeclared actuals => watchdog flags).
--   * canonical wire rows (LEDGER|/AMBIENT|kind|entity_type|entity_id|
--     attr|before|after, integers floored, numeric ids u<id>/c<id>).
--   * Digest() covers ALL alive majors (the whole-board analogue of the
--     simulator's state hash), integers floored, rows sorted.
--
-- Known live risks (upstream docs/agent-vs-agent.md open questions 1-2):
--   * does GameEvents.PlayerTurnStartComplete fire before the built-in AI
--     acts? If not, the freeze here is too late and the watchdog will say so.
--   * does PlayerManager.SetLocalPlayerAndObserver work from GameCore
--     context? If not, write paths must run entirely in InGame state.

Puppeteer = {}
Puppeteer.version = "0.2.0-draft"
Puppeteer.supports_freeze = true
Puppeteer.supports_ledger = true
Puppeteer.supports_digest = true

local PUPPET_PLAYERS = {}          -- set[playerID] = true; configured via SetPuppet
local lease = nil                  -- { playerID, turn, snapshot = {unitId -> state} }
local command_ledger = {}          -- UNDECLARED actuals: drift the referee never asked for
local ambient_ledger = {}          -- DECLARED at phase boundaries: the authorization manifest
local ambient_window_open = false  -- true ONLY inside BeginAmbientWindow/EndAmbientWindow
local ambient_snapshot = nil       -- BeginAmbientWindow's per-player snapshot

local function ifloor(v) return string.format("%d", math.floor(v or 0)) end
local function boolstr(v) return tostring(v and true or false) end

-- live-learned 2026-08-30: the GameCore unit object exposes
-- GetMovesRemaining / GetDamage — NOT GetMovementRemaining / GetHP, and no
-- fortified accessor at all (dropped from snapshots/digest accordingly).
local function snapshot_units(playerID)
    local snap = {}
    local pUnits = Players[playerID]:GetUnits()
    for _, unit in pUnits:Members() do
        snap[unit:GetID()] = {
            x = unit:GetX(), y = unit:GetY(),
            moves = unit:GetMovesRemaining(),
            damage = unit:GetDamage(),
        }
    end
    return snap
end

-- v0.2 ambient-recorder snapshot: units AND cities AND treasury/research of
-- one player. Scope note (Codex P1-2, 2026-08-30): this recorder + the
-- digest cover units/cities/population/gold/research/production-name.
-- Districts, build queues, promotions, and tile ownership are OUTSIDE both
-- — a recorded, accepted limitation (docs/live-validation.md §6), not a
-- silent hole: nothing may claim live watchdog authority over them.
local function snapshot_player(playerID)
    local snap = { units = {}, cities = {} }
    pcall(function()
        for id, u in pairs(snapshot_units(playerID)) do snap.units[id] = u end
    end)
    pcall(function()
        for _, city in Players[playerID]:GetCities():Members() do
            snap.cities[city:GetID()] = {
                population = city:GetPopulation(),
            }
        end
    end)
    pcall(function()
        local p = Players[playerID]
        local tech = p:GetTechs():GetResearchingTech()
        snap.player = {
            -- live-learned: GetGoldBalance, not GetGold (GameCore treasury)
            gold = math.floor(p:GetTreasury():GetGoldBalance()),
            researching = (tech ~= nil) and tech or -1,
        }
    end)
    return snap
end

local function book_ambient(kind, entity_type, entity_id, attr, before, after)
    table.insert(ambient_ledger, string.format(
        "AMBIENT|%s|%s|%s|%s|%s|%s",
        kind, entity_type, entity_id, attr, tostring(before), tostring(after)))
end

local function book_actual(kind, entity_type, entity_id, attr, before, after)
    table.insert(command_ledger, string.format(
        "LEDGER|%s|%s|%s|%s|%s|%s",
        kind, entity_type, entity_id, attr, tostring(before), tostring(after)))
end

-- Diff after-against-before and book every drift. `book` selects the
-- destination: the ambient ledger (DECLARED manifest) inside a window,
-- the command ledger (UNDECLARED actuals) at release time.
local function diff_player(playerID, before, book)
    local after = snapshot_player(playerID)
    local seen = {}
    for uid, u in pairs(after.units) do
        seen[uid] = true
        local b = before.units[uid]
        local id = "u" .. uid
        if b == nil then
            book("unit.spawned", "unit", id, "exists", "false", "true")
        else
            if u.x ~= b.x or u.y ~= b.y then
                book("unit.moved", "unit", id, "pos",
                     b.x .. "," .. b.y, u.x .. "," .. u.y)
            end
            if ifloor(u.moves) ~= ifloor(b.moves) then
                book("unit.moves", "unit", id, "moves",
                     ifloor(b.moves), ifloor(u.moves))
            end
            if ifloor(u.damage) ~= ifloor(b.damage) then
                book("unit.damage", "unit", id, "damage",
                     ifloor(b.damage), ifloor(u.damage))
            end
        end
    end
    for uid in pairs(before.units) do
        if not seen[uid] then
            book("unit.despawned", "unit", "u" .. uid, "exists", "true", "false")
        end
    end
    local cseen = {}
    for cid, c in pairs(after.cities) do
        cseen[cid] = true
        local b = before.cities[cid]
        local id = "c" .. cid
        if b == nil then
            book("city.founded", "city", id, "exists", "false", "true")
        elseif ifloor(c.population) ~= ifloor(b.population) then
            book("city.growth", "city", id, "population",
                 ifloor(b.population), ifloor(c.population))
        end
    end
    for cid in pairs(before.cities) do
        if not cseen[cid] then
            book("city.lost", "city", "c" .. cid, "exists", "true", "false")
        end
    end
    -- treasury / research / per-city production (digest parity: Codex P1-2)
    if before.player ~= nil and after.player ~= nil then
        if ifloor(before.player.gold) ~= ifloor(after.player.gold) then
            book("player.gold", "player", "p" .. playerID, "gold",
                 ifloor(before.player.gold), ifloor(after.player.gold))
        end
        if before.player.researching ~= after.player.researching then
            book("player.research_set", "player", "p" .. playerID,
                 "researching", tostring(before.player.researching),
                 tostring(after.player.researching))
        end
    end
    -- production-name coverage DEFERRED: no accessor in GameCore
    -- (GetProductionName is UI-context only) — recorded in §6
end

-- -- handshake -------------------------------------------------------------

function Puppeteer.Handshake()
    print("MOD_VERSION|" .. Puppeteer.version)
    print("SUPPORTS_FREEZE|" .. boolstr(Puppeteer.supports_freeze))
    print("SUPPORTS_LEDGER|" .. boolstr(Puppeteer.supports_ledger))
    print("SUPPORTS_DIGEST|" .. boolstr(Puppeteer.supports_digest))
    print("---END---")
end

function Puppeteer.SetPuppet(playerID, enabled)
    PUPPET_PLAYERS[playerID] = enabled and true or nil
    if not enabled and lease ~= nil and lease.playerID == playerID then
        lease = nil
    end
    print("PUPPET_SET|" .. playerID .. "|" .. boolstr(enabled))
    print("---END---")
end

function Puppeteer.Status()
    local active = lease ~= nil
    return string.format(
        "TURN|%d\nPUPPET_ACTIVE|%s\nLEASE_PLAYER|%d\nLEASE_TURN|%d\n---END---",
        Game.GetCurrentGameTurn(), boolstr(active),
        (lease ~= nil) and lease.playerID or -1,
        (lease ~= nil) and lease.turn or -1)
end

-- -- freeze / lease -----------------------------------------------------------

function OnPlayerTurnStartComplete(playerID)
    if not PUPPET_PLAYERS[playerID] then
        return
    end
    -- Step 1: freeze ALL of the puppet's units (zero movement) so the
    -- built-in AI cannot act during our lease, THEN snapshot. Order is
    -- load-bearing (Codex P1-1): the snapshot is the release-diff baseline,
    -- so it must capture the FROZEN state — snapshotting first would book
    -- our own freeze (movement 2->0) as an undeclared violation at release.
    local pUnits = Players[playerID]:GetUnits()
    for _, unit in pUnits:Members() do
        UnitManager.FinishMoves(unit)
    end
    lease = { playerID = playerID, turn = Game.GetCurrentGameTurn(),
              snapshot = snapshot_player(playerID) }
    print("PUPPET_ACTIVE|true")
end

function OnPlayerTurnDeactivated(playerID)
    if PUPPET_PLAYERS[playerID] then
        Puppeteer.Release(playerID)
    end
end

-- Step 2 of the contract: restore exactly ONE unit, immediately before the
-- coordinator's command for that unit executes. NEVER bulk-restore.
function Puppeteer.RestoreUnit(unitId)
    if lease == nil then
        return
    end
    local unit = Players[lease.playerID]:GetUnits():FindID(unitId)
    if unit ~= nil then
        UnitManager.RestoreMovement(unit)
        UnitManager.RestoreUnitAttacks(unit)
    end
end

function Puppeteer.Release(playerID)
    -- Re-diff the whole lease: anything the referee never declared lands in
    -- the command ledger as an undeclared actual (the watchdog flags it).
    if lease ~= nil and lease.playerID == playerID and lease.snapshot ~= nil then
        diff_player(playerID, lease.snapshot, book_actual)
    end
    if lease ~= nil and lease.playerID == playerID then
        lease = nil
    end
    print("PUPPET_ACTIVE|" .. boolstr(lease ~= nil))
    print("---END---")
end

-- Re-injection hygiene (D9): the adapter re-executes this file at every
-- attach — retire the PREVIOUS injection's hooks first or both live on
-- and fight over one `lease`.
if type(PUPPETEER_CLEANUP) == "function" then PUPPETEER_CLEANUP() end
GameEvents.PlayerTurnStartComplete.Add(OnPlayerTurnStartComplete)
Events.PlayerTurnDeactivated.Add(OnPlayerTurnDeactivated)
PUPPETEER_CLEANUP = function()
    GameEvents.PlayerTurnStartComplete.Remove(OnPlayerTurnStartComplete)
    Events.PlayerTurnDeactivated.Remove(OnPlayerTurnDeactivated)
end

-- -- ambient windows: the load-bearing contract ---------------------------------
-- Engine effects that happen INSIDE a window are booked as ambient (declared
-- at the phase boundary). Anything mutating OUTSIDE a window is a violation
-- by definition — that is what makes the Python-side diff sound.

function Puppeteer.BeginAmbientWindow(playerID)
    ambient_snapshot = snapshot_player(playerID)
    ambient_window_open = true
    print("AMBIENT_WINDOW|open|" .. playerID)
    print("---END---")
end

function Puppeteer.EndAmbientWindow(playerID)
    if ambient_snapshot ~= nil then
        diff_player(playerID, ambient_snapshot, book_ambient)
        ambient_snapshot = nil
    end
    ambient_window_open = false
    print("AMBIENT_WINDOW|closed|" .. playerID)
    print("---END---")
end

-- -- verification digest -----------------------------------------------------------

-- Deterministic whole-board digest over sorted rows for every ALIVE MAJOR:
--   u<unitID>|<owner>|<x>|<y>|<movesRemaining>|<damage>
--   c<cityID>|<owner>|<population>
--   p<playerID>|<gold>|<researchingTechID or -1>
-- Integers are floored (canonical JSON rejects floats); this is the live
-- before/after "hash" source the Python referee sha256s.
function Puppeteer.Digest()
    local rows = {}
    for _, p in ipairs(PlayerManager.GetAliveMajors()) do
        local pid = p:GetID()
        for _, unit in p:GetUnits():Members() do
            table.insert(rows, string.format("u%d|%d|%d|%d|%d|%d",
                unit:GetID(), pid, unit:GetX(), unit:GetY(),
                math.floor(unit:GetMovesRemaining()),
                math.floor(unit:GetDamage())))
        end
        for _, city in p:GetCities():Members() do
            table.insert(rows, string.format("c%d|%d|%d",
                city:GetID(), pid, math.floor(city:GetPopulation())))
        end
        local tech = p:GetTechs():GetResearchingTech()
        table.insert(rows, string.format("p%d|%d|%d",
            pid, math.floor(p:GetTreasury():GetGoldBalance()),
            (tech ~= nil) and tech or -1))
    end
    table.sort(rows)
    print("DIGEST|" .. table.concat(rows, ";"))
    print("---END---")
end

-- -- command ledger ------------------------------------------------------------

function Puppeteer.DumpLedger()
    for _, row in ipairs(command_ledger) do
        print(row)
    end
    command_ledger = {}
    print("---END---")
end

function Puppeteer.DumpAmbient()
    for _, row in ipairs(ambient_ledger) do
        print(row)
    end
    ambient_ledger = {}
    print("---END---")
end

-- -- turn-end experiments (D7; the driver picks, outcomes recorded dated) --------

-- H2 primitive: zero out remaining movement so the engine can auto-complete.
function Puppeteer.FinishAllMoves(playerID)
    local n = 0
    for _, unit in Players[playerID]:GetUnits():Members() do
        UnitManager.FinishMoves(unit)
        n = n + 1
    end
    print("FINISHED_MOVES|" .. playerID .. "|" .. n)
    print("---END---")
end

-- -- command application ---------------------------------------------------------
-- Routing follows the GameCore-safe vs InGame-switch split (upstream
-- docs/agent-vs-agent.md): 1-tile UnitManager.MoveUnit runs in GameCore;
-- multi-tile RequestOperation(MOVE_TO), RANGE_ATTACK, FOUND_CITY,
-- BUILD_IMPROVEMENT, CityManager RequestOperation/RequestCommand run inside
-- a pcall-wrapped local-player switch. NEVER call pCulture:SetCivic
-- (permanently breaks AI civic research) — use SetCulturalProgress.

function Puppeteer.WithPlayerContext(playerID, fn)
    local origPlayer = Game.GetLocalPlayer()
    PlayerManager.SetLocalPlayerAndObserver(playerID)
    local ok, err = pcall(fn)
    PlayerManager.SetLocalPlayerAndObserver(origPlayer)
    if not ok then
        print("ERR:PUPPET_ERROR|" .. tostring(err))
    end
    return ok
end
