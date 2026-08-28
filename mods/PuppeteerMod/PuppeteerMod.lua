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
-- Known live risks (upstream docs/agent-vs-agent.md open questions 1-2):
--   * does GameEvents.PlayerTurnStartComplete fire before the built-in AI
--     acts? If not, the freeze here is too late and the watchdog will say so.
--   * does PlayerManager.SetLocalPlayerAndObserver work from GameCore
--     context? If not, write paths must run entirely in InGame state.

Puppeteer = {}
Puppeteer.version = "0.1.0-draft"
Puppeteer.supports_freeze = true
Puppeteer.supports_ledger = true
Puppeteer.supports_digest = true

local PUPPET_PLAYERS = {}          -- set[playerID] = true; configured via SetPuppet
local lease = nil                  -- { playerID, turn, snapshot = {unitId -> state} }
local command_ledger = {}          -- {turn, playerID, unitId, kind, before, after}
local ambient_ledger = {}          -- effects booked during phase-boundary batches
local ambient_window_open = false  -- true ONLY inside BeginAmbientWindow/EndAmbientWindow

local function snapshot_units(playerID)
    local snap = {}
    local pUnits = Players[playerID]:GetUnits()
    for _, unit in pUnits:Members() do
        snap[unit:GetID()] = {
            x = unit:GetX(), y = unit:GetY(),
            movement = unit:GetMovementRemaining(),
            hp = unit:GetHP(), fortified = unit:IsFortified(),
        }
    end
    return snap
end

local function record(kind, playerID, unitId, before, after)
    table.insert(command_ledger, {
        turn = Game.GetCurrentGameTurn(), playerID = playerID,
        unitId = unitId, kind = kind, before = before, after = after,
    })
end

-- -- handshake -------------------------------------------------------------

function Puppeteer.Handshake()
    return string.format(
        "MOD_VERSION|%s\nSUPPORTS_FREEZE|%s\nSUPPORTS_LEDGER|%s\nSUPPORTS_DIGEST|%s\n---END---",
        Puppeteer.version,
        tostring(Puppeteer.supports_freeze),
        tostring(Puppeteer.supports_ledger),
        tostring(Puppeteer.supports_digest)
    )
end

function Puppeteer.SetPuppet(playerID, enabled)
    PUPPET_PLAYERS[playerID] = enabled and true or nil
    print("PUPPET_SET|" .. playerID .. "|" .. tostring(enabled and true or false))
    print("---END---")
end

-- -- freeze / lease -----------------------------------------------------------

function OnPlayerTurnStartComplete(playerID)
    if not PUPPET_PLAYERS[playerID] then
        return
    end
    -- Step 1: capture per-unit state, then freeze ALL of the puppet's units
    -- (zero movement) so the built-in AI cannot act during our lease.
    lease = { playerID = playerID, turn = Game.GetCurrentGameTurn(),
              snapshot = snapshot_units(playerID) }
    local pUnits = Players[playerID]:GetUnits()
    for _, unit in pUnits:Members() do
        UnitManager.FinishMoves(unit)
    end
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
    if lease == nil or lease.snapshot[unitId] == nil then
        return
    end
    local pUnits = Players[lease.playerID]:GetUnits()
    local unit = pUnits:FindID(unitId)
    if unit ~= nil then
        UnitManager.RestoreMovement(unit)
        UnitManager.RestoreUnitAttacks(unit)
    end
end

function Puppeteer.Release(playerID)
    lease = nil
    print("PUPPET_ACTIVE|false")
end

GameEvents.PlayerTurnStartComplete.Add(OnPlayerTurnStartComplete)
Events.PlayerTurnDeactivated.Add(OnPlayerTurnDeactivated)

-- -- ambient windows: the load-bearing contract ---------------------------------
-- Engine effects that happen INSIDE a window are booked as ambient (declared
-- at the phase boundary). Anything mutating OUTSIDE a window is a violation
-- by definition — that is what makes the Python-side diff sound.

function Puppeteer.BeginAmbientWindow(playerID)
    ambient_window_open = true
    print("AMBIENT_WINDOW|open|" .. playerID)
end

function Puppeteer.EndAmbientWindow(playerID)
    ambient_window_open = false
    print("AMBIENT_WINDOW|closed|" .. playerID)
    print("---END---")
end

-- -- verification digest -----------------------------------------------------------

-- Deterministic digest over sorted (unitId, x, y, movement, hp, fortified)
-- and (cityId, production, research, gold) tuples: the live before/after
-- "hash" the Python watchdog uses around each command.
function Puppeteer.Digest()
    local rows = {}
    for playerID, _ in pairs(PUPPET_PLAYERS) do
        local pUnits = Players[playerID]:GetUnits()
        for _, unit in pUnits:Members() do
            table.insert(rows, string.format("u%d|%d|%d|%d|%d|%s",
                unit:GetID(), unit:GetX(), unit:GetY(),
                unit:GetMovementRemaining(), unit:GetHP(),
                tostring(unit:IsFortified())))
        end
        for _, city in Players[playerID]:GetCities():Members() do
            table.insert(rows, string.format("c%s|%s|%s|%d",
                city:GetName(), tostring(city:GetProductionName()),
                tostring(Players[playerID]:GetTechs():GetResearchingTech()),
                Players[playerID]:GetTreasury():GetGold()))
        end
    end
    table.sort(rows)
    print("DIGEST|" .. table.concat(rows, ";"))
    print("---END---")
end

-- -- command ledger ------------------------------------------------------------

function Puppeteer.DumpLedger()
    for _, entry in ipairs(command_ledger) do
        print(string.format("LEDGER|%d|%d|%s|%s|%s|%s",
            entry.turn, entry.playerID, tostring(entry.unitId), entry.kind,
            tostring(entry.before), tostring(entry.after)))
    end
    command_ledger = {}
    print("---END---")
end

function Puppeteer.DumpAmbient()
    for _, entry in ipairs(ambient_ledger) do
        print("AMBIENT|" .. entry)
    end
    ambient_ledger = {}
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
