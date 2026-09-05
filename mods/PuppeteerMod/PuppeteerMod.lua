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
--     output around every command; hook-time prints can contaminate replies;
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
-- v0.3 (M14d):
--   * DiffSinceLast() — the commanded-effects seam. The freeze snapshot is
--     now a ROLLING baseline: each DiffSinceLast call returns the player's
--     drift since the previous call (or lease start) as LEDGER rows and
--     advances the baseline, so Release's final re-diff books only what no
--     command ever covered. The Python adapter journals the returned rows
--     as BOTH the command's mutations (allowed) and actuals — the
--     watchdog's multiset diff then matches commanded effects exactly.
--   * FreezeUnit(id) — the undo for RestoreUnit when the command that
--     followed was rejected: re-freeze so the restore never books as
--     undeclared movement at release.
--
-- Known live risks (upstream docs/agent-vs-agent.md open questions 1-2):
--   * does GameEvents.PlayerTurnStartComplete fire before the built-in AI
--     acts? If not, the freeze here is too late and the watchdog will say so.
--   * does PlayerManager.SetLocalPlayerAndObserver work from GameCore
--     context? If not, write paths must run entirely in InGame state.

-- Same-version reinjection must not discard an active lease or its restore
-- budget. The adapter normally avoids reinjection; this guards direct loads.
if type(Puppeteer) == "table" and Puppeteer.version == "0.3.9"
    and type(Puppeteer.AttachCurrentTurn) == "function"
    and type(Puppeteer.GuardedHandoff) == "function"
    and type(Puppeteer.BeginRewardCommand) == "function"
    and type(Puppeteer.FinishRewardCommand) == "function"
    and Puppeteer.supports_freeze and Puppeteer.supports_ledger
    and Puppeteer.supports_digest and Puppeteer.supports_command_diff then
    return
end

Puppeteer = {}
Puppeteer.version = "0.3.9"
Puppeteer.supports_freeze = true
Puppeteer.supports_ledger = true
Puppeteer.supports_digest = true
Puppeteer.supports_command_diff = true

-- v0.3.1 diagnostic (live run 007): after an ARENA-driven (leased) H1
-- end-turn, the NEXT local turn starts WITHOUT PlayerTurnStartComplete
-- ever firing (t7/t9/t11 stable TURN_ACTIVE-no-lease; t6/t8/t10 hooks all
-- followed no-lease bootstrap ends). PUPPETEER_TRACE is a pollable ring
-- (D3) the driver reads to see hook entries and where they die.
PUPPETEER_TRACE = PUPPETEER_TRACE or {}
local function trace(msg)
    table.insert(PUPPETEER_TRACE,
        Game.GetCurrentGameTurn() .. "|" .. tostring(msg))
    if #PUPPETEER_TRACE > 64 then table.remove(PUPPETEER_TRACE, 1) end
end

local PUPPET_PLAYERS = {}          -- set[playerID] = true; configured via SetPuppet
local lease = nil                  -- { playerID, turn, snapshot = {unitId -> state} }
-- Survives mod replacement within this GameCore VM, not a fresh game. Once
-- turn one was acquired (or an attach freeze attempted), release cannot make
-- that same turn eligible for a new movement allowance.
PUPPETEER_INITIAL_ATTACH_USED = PUPPETEER_INITIAL_ATTACH_USED or {}
local command_ledger = {}          -- UNDECLARED actuals: drift the referee never asked for
local reward_window = nil
local reward_finished = nil
local reward_row = nil
local reward_hook_registered = false
-- A consumed village without a timely event makes subsequent event attribution unsafe.
-- This quarantine survives lease changes and same-version reinjection.
local reward_quarantined = false
local ambient_ledger = {}          -- DECLARED at phase boundaries: the authorization manifest
local ambient_window_open = false  -- true ONLY inside BeginAmbientWindow/EndAmbientWindow
local ambient_snapshot = nil       -- BeginAmbientWindow's per-player snapshot

local function ifloor(v) return string.format("%d", math.floor(v or 0)) end
local function boolstr(v) return tostring(v and true or false) end

-- live-learned 2026-08-30: the GameCore unit object exposes
-- GetMovesRemaining / GetDamage — NOT GetMovementRemaining / GetHP, and no
-- fortified accessor at all (dropped from snapshots/digest accordingly).
-- live-learned glm-g1: consumed/dead units report position (-9999,-9999)
-- (upstream guards the same) — they must read as GONE, not as units that
-- "moved to null island" (that booked a phantom move violation on the
-- founding settler)
local function unit_gone(unit)
    local ok, x = pcall(function() return unit:GetX() end)
    return (not ok) or x == -9999
end

local function snapshot_units(playerID)
    local snap = {}
    local pUnits = Players[playerID]:GetUnits()
    for _, unit in pUnits:Members() do
        if not unit_gone(unit) then
            snap[unit:GetID()] = {
                x = unit:GetX(), y = unit:GetY(),
                moves = unit:GetMovesRemaining(),
                damage = unit:GetDamage(),
            }
        end
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
        local id = "u" .. playerID .. ":" .. uid
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
            book("unit.despawned", "unit", "u" .. playerID .. ":" .. uid, "exists", "true", "false")
        end
    end
    local cseen = {}
    for cid, c in pairs(after.cities) do
        cseen[cid] = true
        local b = before.cities[cid]
        local id = "c" .. playerID .. ":" .. cid
        if b == nil then
            book("city.founded", "city", id, "exists", "false", "true")
        elseif ifloor(c.population) ~= ifloor(b.population) then
            book("city.growth", "city", id, "population",
                 ifloor(b.population), ifloor(c.population))
        end
    end
    for cid in pairs(before.cities) do
        if not cseen[cid] then
            book("city.lost", "city", "c" .. playerID .. ":" .. cid, "exists", "true", "false")
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

-- v0.3: same diff, but the rows are COLLECTED and returned as one string
-- (the commanded-effects seam the Python adapter reads after each act).
local function diff_rows(playerID, before)
    local rows = {}
    local function collect(kind, entity_type, entity_id, attr, b, a)
        table.insert(rows, string.format(
            "LEDGER|%s|%s|%s|%s|%s|%s", kind, entity_type, entity_id, attr,
            tostring(b), tostring(a)))
    end
    diff_player(playerID, before, collect)
    return rows
end

-- -- handshake -------------------------------------------------------------

function Puppeteer.Handshake()
    print("MOD_VERSION|" .. Puppeteer.version)
    print("SUPPORTS_FREEZE|" .. boolstr(Puppeteer.supports_freeze))
    print("SUPPORTS_LEDGER|" .. boolstr(Puppeteer.supports_ledger))
    print("SUPPORTS_DIGEST|" .. boolstr(Puppeteer.supports_digest))
    print("SUPPORTS_COMMAND_DIFF|" .. boolstr(Puppeteer.supports_command_diff))
    print("SUPPORTS_REWARD_RECEIPTS|" .. boolstr(reward_hook_registered
        and type(Puppeteer.BeginRewardCommand) == "function"
        and type(Puppeteer.FinishRewardCommand) == "function"))
    print("SUPPORTS_GUARDED_HANDOFF|" .. boolstr(type(Puppeteer.GuardedHandoff) == "function"))
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
    -- IsTurnActive discriminates the dispatch race (live-learned run 005):
    -- TURN is shared by all players, so "TURN=7, no lease" is ambiguous —
    -- mid-AI-transition (our hook is still AHEAD: drive TURN) vs a parked
    -- local turn whose hook already fired pre-injection (drive TURN+1).
    -- Reported for the LOCAL player (the seat the driver drives).
    local turn_active = false
    pcall(function()
        local me = Game.GetLocalPlayer()
        local p = Players[me]
        if p ~= nil and p.IsTurnActive ~= nil then
            turn_active = p:IsTurnActive()
        end
    end)
    return string.format(
        "TURN|%d\nPUPPET_ACTIVE|%s\nLEASE_PLAYER|%d\nLEASE_TURN|%d"
        .. "\nTURN_ACTIVE|%s\n---END---",
        Game.GetCurrentGameTurn(), boolstr(active),
        (lease ~= nil) and lease.playerID or -1,
        (lease ~= nil) and lease.turn or -1,
        boolstr(turn_active))
end

-- -- freeze / lease -----------------------------------------------------------

local function acquire_lease(playerID, initialAllowances)
    -- Step 1: freeze ALL of the puppet's units (zero movement) so the
    -- built-in AI cannot act during our lease, THEN snapshot. Order is
    -- load-bearing (Codex P1-1): the snapshot is the release-diff baseline,
    -- so it must capture the FROZEN state — snapshotting first would book
    -- our own freeze (movement 2->0) as an undeclared violation at release.
    -- v0.3.1: per-unit pcall — one poisoned unit must not kill the hook
    -- (and the trace shows exactly which one, if any).
    local pUnits = Players[playerID]:GetUnits()
    for _, unit in pUnits:Members() do
        if not unit_gone(unit) then
            local ok, err = pcall(function() UnitManager.FinishMoves(unit) end)
            if not ok then trace("FREEZE_ERR|u" .. tostring(unit:GetID())
                                 .. "|" .. tostring(err)) end
            if initialAllowances ~= nil then
                if not ok then return false, "freeze_failed" end
                local checked, preserved = pcall(function()
                    return unit:GetMovesRemaining() == 0
                        and unit:GetAttacksRemaining() == initialAllowances[unit:GetID()]
                end)
                if not checked or not preserved then return false, "freeze_verification_failed" end
            end
        end
    end
    reward_window, reward_finished, reward_row = nil, nil, nil
    lease = { playerID = playerID, turn = Game.GetCurrentGameTurn(),
              snapshot = snapshot_player(playerID),
              preserve_attacks = initialAllowances ~= nil }
    if lease.turn == 1 then PUPPETEER_INITIAL_ATTACH_USED[playerID] = true end
    trace("LEASE_SET|" .. tostring(playerID) .. "|"
          .. tostring(Game.GetCurrentGameTurn()))
    -- Native hooks run between RPCs: never print onto the command-response
    -- channel here. Status() and Trace() are the explicit polling surfaces.
    return true, "acquired"
end

local function OnPlayerTurnStartComplete(playerID)
    trace("HOOK_ENTER|" .. tostring(playerID))
    if not PUPPET_PLAYERS[playerID] then
        trace("HOOK_SKIP|not-puppet|" .. tostring(playerID))
        return
    end
    -- A delayed native callback after explicit initial acquisition must not
    -- freeze again or clear the once-per-lease restored-unit set.
    if lease ~= nil and lease.playerID == playerID
        and lease.turn == Game.GetCurrentGameTurn() then
        trace("HOOK_DUPLICATE|" .. tostring(playerID))
        return
    end
    if Game.GetCurrentGameTurn() == 1 and PUPPETEER_INITIAL_ATTACH_USED[playerID] then
        trace("HOOK_SKIP|initial-already-acquired|" .. tostring(playerID))
        return
    end
    acquire_lease(playerID, nil)
end

-- Acquire only the initial local human turn whose natural start hook was
-- missed before injection. No end-turn or global event is synthesized.
-- GetAttacksRemaining is source-backed; no max/used-attacks accessor is
-- assumed. Remaining attacks survive both freeze and per-unit restore.
function Puppeteer.AttachCurrentTurn(expectedPlayer, expectedTurn)
    local function report(status, reason)
        print("ATTACH_CURRENT|" .. status .. "|" .. tostring(expectedPlayer)
            .. "|" .. tostring(expectedTurn) .. "|" .. reason)
        print("---END---")
    end
    local checked, reason, allowances = pcall(function()
        if type(expectedPlayer) ~= "number" or expectedPlayer < 0
            or expectedPlayer ~= math.floor(expectedPlayer) then return "invalid_player" end
        if expectedTurn ~= 1 then return "not_initial_turn" end
        if Game.GetCurrentGameTurn() ~= expectedTurn then return "turn_mismatch" end
        if Game.GetLocalPlayer() ~= expectedPlayer then return "not_local_player" end
        if not PUPPET_PLAYERS[expectedPlayer] then return "not_armed" end
        local player = Players[expectedPlayer]
        if player == nil then return "missing_player" end
        if not player:IsHuman() then return "not_human" end
        if not player:IsTurnActive() then return "not_active" end
        if lease ~= nil then
            if lease.playerID == expectedPlayer and lease.turn == expectedTurn then
                return "duplicate"
            end
            return "existing_lease"
        end
        if PUPPETEER_INITIAL_ATTACH_USED[expectedPlayer] then return "already_acquired" end
        local remainingAttacks = {}
        local count = 0
        -- Complete every precondition before freezing ANY unit. Missing API
        -- or partially spent movement fails closed without mutating the game.
        for _, unit in player:GetUnits():Members() do
            local x = unit:GetX()
            if x ~= -9999 then
                local moves, maximum = unit:GetMovesRemaining(), unit:GetMaxMoves()
                local attacks = unit:GetAttacksRemaining()
                if type(moves) ~= "number" or type(maximum) ~= "number"
                    or maximum < 0 or moves ~= maximum then return "movement_spent" end
                if type(attacks) ~= "number" or attacks < 0
                    or attacks ~= math.floor(attacks) then return "invalid_attacks" end
                remainingAttacks[unit:GetID()] = attacks
                count = count + 1
            end
        end
        if count == 0 then return "no_live_units" end
        return "ready", remainingAttacks
    end)
    if not checked then report("rejected", "guard_unavailable") return end
    if reason == "duplicate" then report("duplicate", "already_engaged") return end
    if reason ~= "ready" then report("rejected", reason) return end
    -- Consume the attempt before the first mutation, including failed freezes.
    PUPPETEER_INITIAL_ATTACH_USED[expectedPlayer] = true
    local ok, acquired, detail = pcall(acquire_lease, expectedPlayer, allowances)
    if not ok then report("failed", "acquisition_error") return end
    if not acquired then report("failed", detail) return end
    trace("ATTACH_CURRENT|" .. tostring(expectedPlayer) .. "|" .. tostring(expectedTurn))
    report("accepted", "acquired")
end

local release_lease  -- shared silent transition; explicit RPC wrapper prints its receipt

local function OnPlayerTurnDeactivated(playerID)
    trace("HOOK_DEACT|" .. tostring(playerID))
    if PUPPET_PLAYERS[playerID] then
        release_lease(playerID, Game.GetCurrentGameTurn())
    end
end

-- Step 2 of the contract: restore exactly ONE unit, immediately before the
-- coordinator's command for that unit executes. NEVER bulk-restore.
-- Codex P1-1 (2026-08-30): ONCE per unit per lease — restore-before-every-
-- command refilled movement/attacks each call, i.e. UNLIMITED actions per
-- turn. The once-only bound keeps each unit to its natural allowance.
function Puppeteer.RestoreUnit(unitId, expectedPlayer)
    if lease == nil or lease.playerID ~= expectedPlayer then
        return "wrong_lease"
    end
    if lease.restored == nil then lease.restored = {} end
    if lease.restored[unitId] then
        return "already_restored"
    end
    local unit = Players[lease.playerID]:GetUnits():FindID(unitId)
    if unit == nil or unit:GetID() ~= unitId then
        return "unknown_entity"
    end
    UnitManager.RestoreMovement(unit)
    if not lease.preserve_attacks then UnitManager.RestoreUnitAttacks(unit) end
    lease.restored[unitId] = true
    return "restored"
end

-- v0.3: the undo for RestoreUnit when the command that followed was
-- rejected — re-freeze so the restored-but-unused movement never books as
-- an undeclared actual at release.
function Puppeteer.FreezeUnit(unitId, expectedPlayer)
    if lease == nil or lease.playerID ~= expectedPlayer then
        return
    end
    local unit = Players[lease.playerID]:GetUnits():FindID(unitId)
    if unit ~= nil and unit:GetID() == unitId then
        UnitManager.FinishMoves(unit)
    end
    print("FROZEN|" .. unitId)
    print("---END---")
end

-- v0.3: the commanded-effects seam. Diffs the player since the PREVIOUS
-- call (or lease start), advances the rolling baseline, and RETURNS the
-- rows as one string. Release's final re-diff therefore books only what no
-- command ever covered.
--   attrCsv (Codex P1-5): comma-list of attrs this COMMAND may touch
--   (pos,moves,damage,exists,population,gold,researching). Rows outside it
--   are NOT returned — engine drift inside the command window can no
--   longer ride the command's authorization.
--   Idempotence (Codex P1-6): the caller passes a monotonically
--   increasing seq. A call with the SAME seq as the last one is a wire
--   RETRY (lost response) and re-serves the same rows; a new seq computes
--   fresh. Rehearsal-proven necessary: a heuristically re-served cache
--   shifts every command's window into the NEXT command's attr scope.
local diff_cache = nil
local diff_cache_seq = -1

-- attr of a LEDGER row, no patterns (the tuner lexer rejects them):
-- LEDGER|kind|entity_type|entity_id|attr|before|after -> field 5
local function row_attr(row)
    local rest = row
    for _ = 1, 4 do
        local s = string.find(rest, "|", 1, true)
        if s == nil then return nil end
        rest = string.sub(rest, s + 1)
    end
    local s = string.find(rest, "|", 1, true)
    if s == nil then return rest end
    return string.sub(rest, 1, s - 1)
end

function Puppeteer.DiffSinceLast(attrCsv, seq)
    if lease == nil or lease.snapshot == nil then
        return ""
    end
    if diff_cache ~= nil and seq ~= nil and seq == diff_cache_seq then
        return diff_cache  -- wire retry of the same call: same rows
    end
    local rows = diff_rows(lease.playerID, lease.snapshot)
    local out = {}
    for _, row in ipairs(rows) do
        local keep = true
        if attrCsv ~= nil and attrCsv ~= "" then
            local attr = row_attr(row)
            keep = attr ~= nil
                and (("," .. attrCsv .. ","):find(
                    "," .. attr .. ",", 1, true) ~= nil)
        end
        if row == reward_row then keep = true end
        if keep then
            table.insert(out, row)
        else
            -- NOT attributable to this command: engine drift inside the
            -- command window — book it as an UNDECLARED actual right now
            -- (Codex P1-5: it must never ride the command's authorization)
            table.insert(command_ledger, row)
        end
    end
    reward_row = nil
    lease.snapshot = snapshot_player(lease.playerID)
    diff_cache = table.concat(out, "\n")
    diff_cache_seq = seq
    return diff_cache
end

release_lease = function(playerID, turn)
    -- Re-diff from the ROLLING baseline (v0.3): anything no command ever
    -- covered lands in the command ledger as an undeclared actual — the
    -- watchdog flags it.
    -- TURN-BOUND (Codex P1-8, live-confirmed t7/t9/t11): releasing by
    -- player alone let end_phase's trailing release KILL the next turn's
    -- freshly-engaged lease — the parked-turn-no-lease state that stalled
    -- every second turn. -1 = release any (diagnostics).
    if lease ~= nil and lease.playerID == playerID
        and (turn == nil or turn == -1 or lease.turn == turn) then
        if lease.snapshot ~= nil then
            diff_player(playerID, lease.snapshot, book_actual)
        end
        lease = nil
        reward_window, reward_row = nil, nil
        diff_cache = nil
    end
end

function Puppeteer.Release(playerID, turn)
    release_lease(playerID, turn)
    print("PUPPET_ACTIVE|" .. boolstr(lease ~= nil))
    print("---END---")
end

-- v0.3.1: lifecycle cross-check (trace-only) — PlayerTurnActivated is
-- the earliest turn-start event; if it fires while StartComplete does
-- not, the engine's turn-start chain is stopping in between (the run-007
-- suppression, localized).
local function OnPlayerTurnActivated(playerID, isHuman)
    trace("ACTIVATED|" .. tostring(playerID) .. "|" .. tostring(isHuman))
end

-- A native reward is causal evidence only inside one dispatched move. No
-- growth-shaped allowance and no baseline reset: unmatched rows remain actuals.
local function reward_identity(w)
    return w ~= nil and lease ~= nil and lease.playerID == w.player
        and lease.turn == w.turn and Game.GetCurrentGameTurn() == w.turn
        and Game.GetLocalPlayer() == w.player
end

local function OnGoodyHutReward(playerID, unitID, rewardType, rewardSubType)
    local w = reward_window
    if w == nil then return end
    -- Even a duplicate matching event invalidates attribution; never grant twice.
    if playerID ~= w.player or unitID ~= w.unit then return end
    w.events = w.events + 1
    w.reward_type = type(rewardType) == 'number' and rewardType == math.floor(rewardType)
        and math.abs(rewardType) < 4294967296 and rewardType or 0
    w.reward_subtype = type(rewardSubType) == 'number' and rewardSubType == math.floor(rewardSubType)
        and math.abs(rewardSubType) < 4294967296 and rewardSubType or 0
    local ok, valid = pcall(function()
        if not reward_identity(w) or w.events ~= 1 then return false end
        local kind = GameInfo.GoodyHuts[rewardType]
        local sub = GameInfo.GoodyHutSubTypes[rewardSubType]
        local unit = Players[playerID]:GetUnits():FindID(unitID)
        local modifier = GameInfo.Modifiers['GOODY_SURVIVORS_ADD_POPULATION']
        local amount, amount_count = nil, 0
        for arg in GameInfo.ModifierArguments() do
            if arg.ModifierId == 'GOODY_SURVIVORS_ADD_POPULATION' and arg.Name == 'Amount' then
                amount, amount_count = tonumber(arg.Value), amount_count + 1
            end
        end
        return amount == 1 and amount_count == 1 and modifier ~= nil
            and modifier.ModifierType == 'MODIFIER_PLAYER_NEAREST_CITY_ADD_POPULATION'
            and kind ~= nil and kind.GoodyHutType == 'GOODYHUT_SURVIVORS'
            and sub ~= nil and sub.SubTypeGoodyHut == 'GOODYHUT_ADD_POP'
            and sub.GoodyHut == 'GOODYHUT_SURVIVORS'
            and sub.ModifierID == 'GOODY_SURVIVORS_ADD_POPULATION'
            and unit ~= nil and unit:GetX() == w.x and unit:GetY() == w.y
    end)
    w.valid_event = ok and valid
end

-- Sumeria alone receives an additional goody reward when clearing a camp.
-- Bind the live configuration and active data chain; a camp is not generally
-- a reward site, and absent/modified data must not create an expectation.
local function sumerian_camp_reward(playerID)
    local ok, eligible = pcall(function()
        local cfg = PlayerConfigurations[playerID]
        if cfg == nil or cfg:GetCivilizationTypeName() ~= 'CIVILIZATION_SUMERIA' then
            return false
        end
        local trait = 'TRAIT_CIVILIZATION_FIRST_CIVILIZATION'
        local modifierID = 'TRAIT_BARBARIAN_CAMP_GOODY'
        local civilizationLinks, modifierLinks = 0, 0
        for row in GameInfo.CivilizationTraits() do
            if row.CivilizationType == 'CIVILIZATION_SUMERIA' and row.TraitType == trait then
                civilizationLinks = civilizationLinks + 1
            end
        end
        for row in GameInfo.TraitModifiers() do
            if row.TraitType == trait and row.ModifierId == modifierID then
                modifierLinks = modifierLinks + 1
            end
        end
        if civilizationLinks ~= 1 or modifierLinks ~= 1 then return false end
        local modifier = GameInfo.Modifiers[modifierID]
        if modifier == nil
            or modifier.ModifierType ~= 'MODIFIER_PLAYER_ADJUST_IMPROVEMENT_GOODY_HUT' then
            return false
        end
        local effect = GameInfo.DynamicModifiers[modifier.ModifierType]
        if effect == nil or effect.CollectionType ~= 'COLLECTION_OWNER'
            or effect.EffectType ~= 'EFFECT_ADJUST_IMPROVEMENT_GOODY_HUT' then return false end
        local args, count = {}, 0
        for row in GameInfo.ModifierArguments() do
            if row.ModifierId == modifierID then
                if args[row.Name] ~= nil then return false end
                args[row.Name], count = row.Value, count + 1
            end
        end
        return count == 2 and args.ImprovementType == 'IMPROVEMENT_BARBARIAN_CAMP'
            and args.GoodyHutImprovementType == 'IMPROVEMENT_GOODY_HUT'
    end)
    return ok and eligible
end

function Puppeteer.BeginRewardCommand(playerID, turn, unitID, nonce, x, y, seq)
    local function report(status)
        print('REWARD_BEGIN|' .. tostring(nonce) .. '|' .. status)
        print('---END---')
    end
    local ok, status = pcall(function()
        if type(nonce) ~= 'string' or #nonce ~= 64 or nonce:find('[^0-9a-f]') then
            return 'rejected'
        end
        for _, value in ipairs({playerID, turn, unitID, x, y, seq}) do
            if type(value) ~= 'number' or value ~= math.floor(value)
                or value < 0 or value > 9007199254740991 then return 'rejected' end
        end
        if not reward_hook_registered then return 'unsupported' end
        if reward_quarantined then return 'quarantined_missing_event' end
        if reward_window ~= nil then
            local w = reward_window
            if w.nonce == nonce and w.player == playerID and w.turn == turn
                and w.unit == unitID and w.x == x and w.y == y and w.seq == seq
                and reward_identity(w) then return 'duplicate' end
            return 'rejected'
        end
        if reward_finished ~= nil and reward_finished.nonce == nonce then return 'rejected' end
        if diff_cache ~= nil and seq <= diff_cache_seq then return 'rejected' end
        if lease == nil or lease.playerID ~= playerID or lease.turn ~= turn
            or Game.GetCurrentGameTurn() ~= turn or Game.GetLocalPlayer() ~= playerID
            or not Players[playerID]:IsTurnActive() then return 'rejected' end
        if Players[playerID]:GetUnits():FindID(unitID) == nil then return 'rejected' end
        local plot = Map.GetPlot(x,y)
        local improvement = plot ~= nil and GameInfo.Improvements[plot:GetImprovementType()] or nil
        local site_kind = nil
        if improvement ~= nil then
            local kind = improvement.ImprovementType
            if kind == 'IMPROVEMENT_GOODY_HUT'
                or (kind == 'IMPROVEMENT_BARBARIAN_CAMP' and sumerian_camp_reward(playerID)) then
                site_kind = kind
            end
        end
        local w = {site_kind=site_kind, player=playerID, turn=turn, unit=unitID, nonce=nonce,
            x=x, y=y, seq=seq, events=0, populations={}}
        local best, tied = nil, false
        for _, city in Players[playerID]:GetCities():Members() do
            local id, population = city:GetID(), city:GetPopulation()
            if city:GetOwner() ~= playerID or population < 1
                or population ~= math.floor(population) then return 'rejected' end
            w.populations[id] = population
            local distance = Map.GetPlotDistance(x, y, city:GetX(), city:GetY())
            if best == nil or distance < best then
                best, tied, w.city = distance, false, id
            elseif distance == best then tied = true end
        end
        if tied then w.city = nil end
        reward_window = w
        return 'accepted'
    end)
    report(ok and status or 'failed')
end

function Puppeteer.CancelRewardCommand(nonce)
    if reward_window ~= nil and reward_window.nonce == nonce then reward_window = nil end
    print('REWARD_CANCEL|' .. tostring(nonce))
    print('---END---')
end

function Puppeteer.FinishRewardCommand(nonce, attrs, seq)
    if reward_finished ~= nil and reward_finished.nonce == nonce
        and reward_finished.seq == seq and reward_finished.attrs == attrs then
        print(reward_finished.output) print('---END---') return
    end
    local w = reward_window
    if not reward_identity(w) or w.nonce ~= nonce or w.seq ~= seq then
        error('reward command identity mismatch')
    end
    reward_window = nil -- consumed even if validation/diff fails
    local causal = nil
    local checked, consumed = pcall(function()
        local plot = Map.GetPlot(w.x,w.y)
        if w.site_kind == nil or plot == nil then return false end
        local improvement = GameInfo.Improvements[plot:GetImprovementType()]
        return improvement == nil or improvement.ImprovementType ~= w.site_kind
    end)
    if checked and consumed and w.events == 0 then reward_quarantined = true end
    local ok, matched = pcall(function()
        if w.site_kind == nil or not w.valid_event or w.events ~= 1 or w.city == nil then return false end
        local plot = Map.GetPlot(w.x,w.y)
        if plot == nil then return false end
        local improvement = GameInfo.Improvements[plot:GetImprovementType()]
        if improvement ~= nil and improvement.ImprovementType == w.site_kind then
            return false
        end
        local before = w.populations[w.city]
        if lease.snapshot.cities[w.city] == nil
            or lease.snapshot.cities[w.city].population ~= before then return false end
        local changed, seen = 0, {}
        for _, city in Players[w.player]:GetCities():Members() do
            local id, after = city:GetID(), city:GetPopulation()
            seen[id] = true
            if city:GetOwner() ~= w.player then return false end
            if w.populations[id] == nil then return false end
            if after ~= w.populations[id] then
                changed = changed + 1
                if id ~= w.city or after ~= before + 1 then return false end
            end
        end
        for id in pairs(w.populations) do if not seen[id] then return false end end
        if changed ~= 1 then return false end
        reward_row = 'LEDGER|city.growth|city|c' .. w.player .. ':' .. w.city
            .. '|population|' .. ifloor(before) .. '|' .. ifloor(before + 1)
        causal = 'REWARD_CAUSE|' .. nonce .. '|' .. w.player .. '|' .. w.turn
            .. '|' .. w.unit .. '|GOODYHUT_SURVIVORS|GOODYHUT_ADD_POP|'
            .. w.city .. '|' .. ifloor(before) .. '|' .. ifloor(before + 1) .. '|' .. w.site_kind
        return true
    end)
    if not ok or not matched then reward_row, causal = nil, nil end
    local rows = Puppeteer.DiffSinceLast(attrs, seq)
    -- Only emit provenance if the exact authorized row was actually returned.
    local output = 'REWARD_FINISH|' .. nonce .. '|' .. tostring(seq)
    local classification = 'no_matching_event'
    if reward_quarantined then classification = 'missing_consumption_event'
    elseif w.events > 1 then classification = 'duplicate_events'
    elseif w.events == 1 then
        classification = causal ~= nil and 'matched_add_population' or 'unsupported_or_unmatched'
    end
    output = output .. '\nREWARD_OBSERVATION|' .. nonce .. '|' .. w.events
        .. '|' .. tostring(w.reward_type or 0) .. '|' .. tostring(w.reward_subtype or 0)
        .. '|' .. classification
    if causal ~= nil then output = output .. '\n' .. causal end
    if rows ~= '' then output = output .. '\n' .. rows end
    reward_finished = {nonce=nonce, seq=seq, attrs=attrs, output=output}
    print(output) print('---END---')
end

-- Re-injection hygiene (D9): the adapter re-executes this file at every
-- attach — retire the PREVIOUS injection's hooks first or both live on
-- and fight over one `lease`.
if type(PUPPETEER_CLEANUP) == "function" then PUPPETEER_CLEANUP() end
if Events.GoodyHutReward ~= nil then
    reward_hook_registered = pcall(function()
        Events.GoodyHutReward.Add(OnGoodyHutReward)
    end)
end
GameEvents.PlayerTurnStartComplete.Add(OnPlayerTurnStartComplete)
Events.PlayerTurnDeactivated.Add(OnPlayerTurnDeactivated)
GameEvents.PlayerTurnActivated.Add(OnPlayerTurnActivated)
PUPPETEER_CLEANUP = function()
    if reward_hook_registered then Events.GoodyHutReward.Remove(OnGoodyHutReward) end
    GameEvents.PlayerTurnStartComplete.Remove(OnPlayerTurnStartComplete)
    Events.PlayerTurnDeactivated.Remove(OnPlayerTurnDeactivated)
    GameEvents.PlayerTurnActivated.Remove(OnPlayerTurnActivated)
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
            if not unit_gone(unit) then
                table.insert(rows, string.format("u%d:%d|%d|%d|%d|%d|%d",
                    pid, unit:GetID(), pid, unit:GetX(), unit:GetY(),
                    math.floor(unit:GetMovesRemaining()),
                    math.floor(unit:GetDamage())))
            end
        end
        for _, city in p:GetCities():Members() do
            table.insert(rows, string.format("c%d:%d|%d|%d",
                pid, city:GetID(), pid, math.floor(city:GetPopulation())))
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

-- v0.3.1: pollable hook trace (D3 — prints from engine callbacks are
-- unsolicited and get drained; polling is the only sound read).
function Puppeteer.Trace()
    return table.concat(PUPPETEER_TRACE, "\n")
end

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

-- Transfer local control only after the outgoing seat has no movement.
-- One GameCore chunk removes the switch-to-nonlocal / later-freeze window.
-- Never advance lease.snapshot here: release must still audit all drift.
local handoff_receipts = {}
function Puppeteer.GuardedHandoff(playerID, turn, nextPlayer)
    local key = tostring(playerID) .. ":" .. tostring(turn) .. ":" .. tostring(nextPlayer)
    local function report(status, reason)
        print("HANDOFF|" .. tostring(playerID) .. "|" .. tostring(turn)
            .. "|" .. tostring(nextPlayer) .. "|" .. status .. "|" .. reason)
        print("---END---")
    end
    local ok, status, reason = pcall(function()
        for _, value in ipairs({playerID, turn, nextPlayer}) do
            if type(value) ~= "number" or value < 0 or value ~= math.floor(value) then
                return "rejected", "invalid_identity"
            end
        end
        if handoff_receipts[key] then return "duplicate", "already_sent" end
        if lease == nil or lease.playerID ~= playerID or lease.turn ~= turn then
            return "rejected", "wrong_lease"
        end
        if Game.GetCurrentGameTurn() ~= turn then return "rejected", "wrong_turn" end
        if Game.GetLocalPlayer() ~= playerID then return "rejected", "wrong_local" end
        if nextPlayer == playerID or not PUPPET_PLAYERS[nextPlayer] then
            return "rejected", "invalid_next"
        end
        local nextAlive = false
        for _, player in ipairs(PlayerManager.GetAliveMajors()) do
            if player:GetID() == nextPlayer then nextAlive = true end
        end
        if not nextAlive then return "rejected", "next_not_alive_major" end
        local player = Players[playerID]
        if player == nil or not player:IsHuman() or not player:IsTurnActive() then
            return "rejected", "current_not_active_local_human"
        end
        for _, unit in player:GetUnits():Members() do UnitManager.FinishMoves(unit) end
        for _, unit in player:GetUnits():Members() do
            if unit:GetMovesRemaining() ~= 0 then return "failed", "freeze_incomplete" end
        end
        -- Refuse an unexpected synchronous transition; do not switch a new seat.
        if lease == nil or lease.playerID ~= playerID or lease.turn ~= turn
            or Game.GetCurrentGameTurn() ~= turn or Game.GetLocalPlayer() ~= playerID then
            return "failed", "changed_during_freeze"
        end
        PlayerManager.SetLocalPlayerAndObserver(nextPlayer)
        if Game.GetLocalPlayer() ~= nextPlayer then return "failed", "switch_not_observed" end
        handoff_receipts[key] = true
        return "accepted", "frozen_then_switched"
    end)
    if not ok then report("failed", "engine_error") else report(status, reason) end
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
