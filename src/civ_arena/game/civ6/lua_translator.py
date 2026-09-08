"""Pure semantic→Lua translation (the live leg's only Lua source).

Every function returns Lua source text suitable for the vendored
GameConnection's execute_read/execute_write. Output is pipe-delimited
KEY|value lines terminated by the sentinel, matching the response_parser.
Adding live support for an arena tool = one entry here + one parser entry +
one FakeTunerServer canned test — zero arena-core changes.

VM routing (live-probed 2026-08-30, docs/live-validation.md §6):
``execute_read`` lands in **GameCore** (mod commands, state reads, 1-tile
MoveUnit, research setters — but NO UI, NO CityManager Request*);
``execute_write`` lands in **InGame** (UI.RequestAction, UnitManager/
CityManager RequestOperation/RequestCommand — but NO MoveUnit, NO Techs
setters, NO Puppeteer). Every command below is SELF-CONTAINED Lua: the two
VMs share no globals, so nothing may call across.
"""

from __future__ import annotations

import re

from civ_arena.game.civ6 import productive_native
from civ_arena.game.civ6.entity_ids import decode

# ---------------------------------------------------------------------------
# Coordinates. The sim speaks axial hex (q, r); Civ VI uses odd-row offset.
# Host-verified 2026-09-05: all six neighbors at four row/column parity
# combinations have engine distance one (hex-row-frame-probe.log).
# q = x - floor(y/2), r = y. Ledger/digest positions remain engine x,y.
# ---------------------------------------------------------------------------


def _half_floor(v: int) -> int:
    """floor(v/2) matching Lua's math.floor(v/2) for negatives too."""
    return (v - (v % 2)) // 2


def xy_to_axial(x: int, y: int) -> tuple[int, int]:
    return x - _half_floor(y), y


def axial_to_xy(q: int, r: int) -> tuple[int, int]:
    return q + _half_floor(r), r


def _strip_prefix_lua(name_expr: str, prefix: str) -> str:
    """Lua prefix-strip without gmatch (the tuner lexer rejects backslash
    escapes in patterns): sub() on a literal found position."""
    return (f"if string.sub({name_expr}, 1, {len(prefix)}) == "
            f'"{prefix}" then {name_expr} = '
            f"string.sub({name_expr}, {len(prefix) + 1}) end")


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
    """Sim-OVERVIEW-shaped read, M4 wire v2 (13-field OVROW): turn + one
    row per alive major (players dict). civ_name via
    PlayerConfigurations (live-learned: GetCivilizationTypeName is not a
    Players-entry method — per-entry nil guards everywhere).

    M4 economy fields (Amendment 1.1, live-probed 2026-09-08): gold
    balance/gpt/upkeep via Treasury, science via Techs, culture/faith via
    Culture/Religion, era via GetEra — ALL confirmed on GameCore, so this
    read stays on the read transport by default. The named-but-wrong
    getters (GetGold, GetScience, treasury:GetScienceYield, …) never
    appear. The civic-progress triple is InGame-only
    (GetCulturalProgress/GetCostNextCivic missing on GameCore): the whole
    group is gated on GetCulturalProgress being callable and reads '?'
    (unread -> key absent) on the read transport — spectate snapshots
    therefore carry yields + era but no civic progress. Researched civics
    ride OVCIVICS rows (HasCivic IS GameCore-confirmed); OVERA carries the
    game era NAME (max major era through GameInfo.Eras, pcall — omitted
    when unresolvable)."""
    return """
print("OVX|2")
print("TURN|" .. Game.GetCurrentGameTurn())
local maxEra = nil
for pid, p in pairs(Players) do
    if p.IsAlive ~= nil and p.IsMajor ~= nil and p.IsBarbarian ~= nil
        and p:IsAlive() and p:IsMajor() and not p:IsBarbarian() then
        local civ = "CIV_UNKNOWN"
        pcall(function()
            if PlayerConfigurations ~= nil and PlayerConfigurations[pid] ~= nil then
                civ = PlayerConfigurations[pid]:GetCivilizationTypeName() or "CIV_UNKNOWN"
            end
        end)
        local gold = 0
        pcall(function()
            if p.GetTreasury ~= nil then
                gold = math.floor(p:GetTreasury():GetGoldBalance())
            end
        end)
        local res = "-"
        pcall(function()
            local t = p:GetTechs():GetResearchingTech()
            if t ~= nil and t ~= -1 then
                local row = GameInfo.Technologies[t]
                if row ~= nil then
                    res = row.TechnologyType
                    if string.sub(res, 1, 5) == "TECH_" then res = string.sub(res, 6) end
                end
            end
        end)
        local sci, cul, fai, gpt, upkeep, era = "?", "?", "?", "?", "?", "?"
        pcall(function() sci = tostring(math.floor(p:GetTechs():GetScienceYield())) end)
        pcall(function() cul = tostring(math.floor(p:GetCulture():GetCultureYield())) end)
        pcall(function() fai = tostring(math.floor(p:GetReligion():GetFaithYield())) end)
        pcall(function() gpt = tostring(math.floor(p:GetTreasury():GetGoldYield())) end)
        pcall(function()
            upkeep = tostring(math.floor(p:GetTreasury():GetTotalMaintenance())) end)
        pcall(function()
            local e = p:GetEra()
            if type(e) == "number" then
                -- Amendment 3 item 2: the wire carries the era NAME
                -- STRING (resolved via GameInfo.Eras), not the raw int.
                -- The OVERA row's game-era path does the same lookup
                -- for maxEra; replicate it per player.
                local idx = math.floor(e)
                if GameInfo.Eras ~= nil and GameInfo.Eras[idx] ~= nil
                        and GameInfo.Eras[idx].EraType ~= nil then
                    era = GameInfo.Eras[idx].EraType
                else
                    era = tostring(idx)
                end
                if maxEra == nil or e > maxEra then maxEra = idx end
            end
        end)
        local civic, cprog, ccost = "?", "?", "?"
        pcall(function()
            local cu = p:GetCulture()
            if cu.GetCulturalProgress == nil or cu.GetCostNextCivic == nil then
                error("civic progress unavailable on this context")
            end
            local idx = cu:GetProgressingCivic()
            if type(idx) == "number" and idx ~= -1 then
                local row = GameInfo.Civics[idx]
                if row ~= nil and row.CivicType ~= nil then
                    civic = row.CivicType
                end
            else
                civic = "-"
            end
            cprog = tostring(math.floor(cu:GetCulturalProgress()))
            ccost = tostring(math.floor(cu:GetCostNextCivic()))
        end)
        print("OVROW|" .. pid .. "|" .. civ .. "|" .. gold .. "|" .. res
            .. "|" .. sci .. "|" .. cul .. "|" .. fai .. "|" .. gpt
            .. "|" .. upkeep .. "|" .. era .. "|" .. civic .. "|" .. cprog
            .. "|" .. ccost)
        local done = {}
        pcall(function()
            for row in GameInfo.Technologies() do
                if p:GetTechs():HasTech(row.Index) then
                    local nm = row.TechnologyType
                    if string.sub(nm, 1, 5) == "TECH_" then nm = string.sub(nm, 6) end
                    table.insert(done, nm)
                end
            end
        end)
        if #done > 0 then
            table.sort(done)
            print("OVRESEARCHED|" .. pid .. "|" .. table.concat(done, ";"))
        end
        local civics = {}
        pcall(function()
            for row in GameInfo.Civics() do
                if p:GetCulture():HasCivic(row.Index) then
                    table.insert(civics, row.CivicType)
                end
            end
        end)
        if #civics > 0 then
            table.sort(civics)
            print("OVCIVICS|" .. pid .. "|" .. table.concat(civics, ";"))
        end
    end
end
if maxEra ~= nil then
    local eraName = nil
    pcall(function()
        if GameInfo.Eras ~= nil and GameInfo.Eras[maxEra] ~= nil then
            local nm = GameInfo.Eras[maxEra].EraType
            if type(nm) == "string" and nm ~= "" then eraName = nm end
        end
    end)
    if eraName ~= nil then print("OVERA|" .. eraName) end
end
print("---END---")
"""


def units_read() -> str:
    """Sim-UNITS-shaped omniscient read (ALL alive players' units — the
    referee's ownership check needs foreign units present; the projection
    hides what the viewer cannot see). Engine UnitTypes are normalized by
    prefix-strip (UNIT_WARRIOR -> WARRIOR) so agent-facing ids match the
    scripted doctrines' vocabulary."""
    return f"""
print("UNITS|1")
for _, p in ipairs(PlayerManager.GetAlive()) do
    local barbarian = p:IsBarbarian()
    assert(type(barbarian) == "boolean", "IsBarbarian must return boolean")
    for _, unit in p:GetUnits():Members() do
        local gone = false
        pcall(function() gone = (unit:GetX() == -9999) end)
        if gone then
            -- consumed/dead units report (-9999,-9999); upstream skips them
        else
        local info = GameInfo.Units[unit:GetType()]
        local name = "UNKNOWN"
        if info ~= nil then name = info.UnitType end
{_strip_prefix_lua("name", "UNIT_")}
        -- Missing/invalid native health must not masquerade as fully healed.
        local hp, maxhp, healthValid = "unknown", "unknown", false
        pcall(function()
            local maximum, damage = unit:GetMaxDamage(), unit:GetDamage()
            if type(maximum) == "number" and type(damage) == "number"
                and maximum > 0 and maximum <= 1000000
                and maximum == math.floor(maximum) and damage == math.floor(damage)
                and damage >= 0 and damage <= maximum then
                maxhp, hp, healthValid = maximum, maximum - damage, true
            end
        end)
        local moves = 0
        pcall(function() moves = math.floor(unit:GetMovesRemaining()) end)
        local maxmoves = 0
        if info ~= nil and info.BaseMoves ~= nil then maxmoves = math.floor(info.BaseMoves) end
        local combat = 0
        if info ~= nil and info.Combat ~= nil then combat = math.floor(info.Combat) end
        local ranged = 0
        if info ~= nil and info.RangedCombat ~= nil then ranged = math.floor(info.RangedCombat) end
        local fortified = false
        pcall(function()
            if unit.GetFortifyTurns ~= nil then fortified = unit:GetFortifyTurns() > 0 end
        end)
        local x = unit:GetX() local y = unit:GetY()
        -- Explicit owner plus full engine ID: no arithmetic packing.
        print("UNITROW|u" .. p:GetID() .. ":" .. unit:GetID()
            .. "|" .. p:GetID() .. "|" .. name
            .. "|" .. (x - math.floor(y / 2)) .. "|" .. y
            .. "|" .. hp .. "|" .. moves .. "|" .. maxmoves
            .. "|" .. combat .. "|" .. ranged .. "|" .. tostring(fortified)
            .. "|" .. tostring(barbarian) .. "|" .. maxhp .. "|" .. tostring(healthValid))
        end
    end
end
print("---END---")
"""


def cities_read(*, extended: bool = False) -> str:
    """Sim-CITIES-shaped omniscient read. production_queue is read back via
    GetCurrentProductionTypeHash — the accessor every shipped UI consumer
    uses (citysupport/productionpanel/productionhelper; 0 = nothing
    building). The earlier GetCurrentProductionType chain was dead weight:
    that method exists nowhere in shipped Lua, so the pcall always failed
    and the queue read '-' on every row — which made housekeeping re-fill
    production EVERY turn and overwrite the agent's own choice
    (live-tourney-glm53: MONUMENT x10 replacing SCOUT/SETTLER).

    M4 (contract §2, CITIES|2): ``extended=True`` enumerates
    PlayerManager.GetAlive() — city-states included (CAP-R1 finding 8 in
    substance) — with a boolean-asserted IsBarbarian skip and IsMajor on
    the row. Majors keep the fail-loud queue contract; a MINOR's queue is
    pcall-guarded ('?' -> key absent). Every extended city accessor beyond
    the confirmed seven is pcall-wrapped with a '?' fallback: the 2026-09-08
    parked-game probe (Amendment 1.5) proved cities are only enumerable on
    a driver-attached game, so the growth/HP/building/district getters stay
    unconfirmed live until scripts/live_capture_check.py runs against the
    attached chain — texlua stub tests pin the shapes meanwhile. The legacy
    hp/food_bucket/production_bucket/buildings PLACEHOLDER constants are
    gone from the parser: unread now means absent, never a fabricated 100."""
    if not extended:
        return _cities_read_lite()
    return """
print("CITIES|2")
for _, p in ipairs(PlayerManager.GetAlive()) do
    local barbarian = p:IsBarbarian()
    assert(type(barbarian) == "boolean", "IsBarbarian must return boolean")
    if not barbarian then
    local major = "?"
    pcall(function() major = tostring(p:IsMajor()) end)
    for _, city in p:GetCities():Members() do
        local name = ""
        pcall(function()
            if Locale ~= nil then name = Locale.Lookup(city:GetName()) end
        end)
        if name == nil or name == "" then name = "c" .. city:GetID() end
        name = string.gsub(name, "|", "-")
        name = string.gsub(name, "%c", " ")
        local queue = "?"
        -- Amendment 3 item 6 (Codex r2 finding 3): the ENTIRE minor
        -- build-queue read is pcall-wrapped; majors keep the fail-loud
        -- contract. The hash is cached in `bq_h` so neither path
        -- re-calls GetCurrentProductionTypeHash outside pcall, and
        -- `resolved` flows to `queue` consistently so a Scout (or any
        -- resolved unit) actually reaches the wire instead of "?" —
        -- the previous rewrite was a no-op for nonzero hashes.
        local bq_h = nil
        local bq = nil  -- retained for the prodturns read below; nil on minors
        if major == "true" then
            -- majors: no swallow. Original fail-loud contract.
            bq = city:GetBuildQueue()
            if bq == nil or bq.GetCurrentProductionTypeHash == nil then
                error("city production queue unavailable")
            end
            local h = bq:GetCurrentProductionTypeHash()
            if type(h) ~= "number" then
                error("city production hash unavailable")
            end
            bq_h = h
        else
            -- minors: every accessor below is pcall-wrapped. A nil
            -- accessor or a non-numeric hash leaves queue = "?".
            -- Codex r3 finding 2: retain `bq` so the prodturns read
            -- below can call GetTurnsLeft on it (the production hash
            -- is cached in `bq_h` so the second GetCurrentProduction
            -- call is avoided).
            pcall(function()
                local q = city:GetBuildQueue()
                if q ~= nil and q.GetCurrentProductionTypeHash ~= nil then
                    local h = q:GetCurrentProductionTypeHash()
                    if type(h) == "number" then
                        bq = q
                        bq_h = h
                    end
                end
            end)
        end
        if bq_h ~= nil then
            local h = bq_h
            if h == 0 then
                queue = "-"
            else
                local resolved = "-"
                for row in GameInfo.Units() do
                    if row.Hash == h then resolved = row.UnitType break end
                end
                if resolved == "-" then
                    for row in GameInfo.Buildings() do
                        if row.Hash == h then resolved = row.BuildingType break end
                    end
                end
                if string.match(resolved, '^UNIT_DISTRICT_')
                    or string.match(resolved, '^UNIT_PROJECT_')
                    or string.match(resolved, '^BUILDING_DISTRICT_')
                    or string.match(resolved, '^BUILDING_PROJECT_') then
                    error('legacy queue collides with reserved productive namespace')
                end
                -- Districts/Projects fallback: fire only when Units AND
                -- Buildings both failed to resolve. The previous
                -- rewrite wrote to `queue` (still "-") instead of
                -- `resolved`, so the lookup never fired AND the
                -- resolved value never reached the wire.
                if resolved == "-" and GameInfo.Districts ~= nil then
                    for row in GameInfo.Districts() do
                        if row.Hash == h then resolved = row.DistrictType break end
                    end
                end
                if resolved == "-" and GameInfo.Projects ~= nil then
                    for row in GameInfo.Projects() do
                        if row.Hash == h then resolved = row.ProjectType break end
                    end
                end
                if resolved == "-" then
                    resolved = "UNKNOWN_PRODUCTION_" .. tostring(h)
                end
                if string.sub(resolved, 1, 5) == "UNIT_" then
                    resolved = string.sub(resolved, 6)
                elseif string.sub(resolved, 1, 9) == "BUILDING_" then
                    resolved = string.sub(resolved, 10)
                end
                queue = resolved
            end
        end
        local pop = 1
        pcall(function() pop = math.floor(city:GetPopulation()) end)
        local capital = "?"
        pcall(function()
            if city.IsCapital ~= nil then capital = tostring(city:IsCapital()) end
        end)
        local hp, maxhp = "?", "?"
        pcall(function()
            local maximum = city:GetMaxDefenseHitPoints()
            local damage = city:GetDefenseDamage()
            if type(maximum) == "number" and type(damage) == "number"
                and maximum > 0 and maximum <= 1000000
                and maximum == math.floor(maximum) and damage == math.floor(damage)
                and damage >= 0 and damage <= maximum then
                maxhp, hp = math.floor(maximum),
                    math.floor(maximum - damage)
            end
        end)
        local food, thr, surplus, grow = "?", "?", "?", "?"
        pcall(function()
            food = tostring(math.floor(city:GetFood()))
            thr = tostring(math.floor(city:GetGrowthThreshold()))
            surplus = tostring(math.floor(city:GetFoodSurplusPerTurn()))
            grow = tostring(math.floor(city:GetTurnsUntilGrowth()))
        end)
        local prodturns = "?"
        pcall(function()
            -- Codex r3 finding 2: reuse the cached `bq_h` rather than
            -- calling GetCurrentProductionTypeHash a second time.
            if bq ~= nil and bq.GetTurnsLeft ~= nil and bq_h ~= nil
                    and bq_h ~= 0 then
                prodturns = tostring(math.floor(bq:GetTurnsLeft(bq_h)))
            end
        end)
        local buildings = "?"
        pcall(function()
            local done = {}
            for row in GameInfo.Buildings() do
                if city:HasBuilding(row.Index) then
                    table.insert(done, row.BuildingType)
                end
            end
            table.sort(done)
            if #done == 0 then buildings = "-" else
                buildings = table.concat(done, ";") end
        end)
        local districts = "?"
        pcall(function()
            local done = {}
            for _, d in city:GetDistricts():Members() do
                local dt = GameInfo.Districts[d:GetDistrictType()]
                if dt ~= nil then table.insert(done, dt.DistrictType) end
            end
            table.sort(done)
            if #done == 0 then districts = "-" else
                districts = table.concat(done, ";") end
        end)
        local x = city:GetX() local y = city:GetY()
        print("CITYROW|c" .. p:GetID() .. ":" .. city:GetID()
            .. "|" .. p:GetID() .. "|" .. name
            .. "|" .. (x - math.floor(y / 2)) .. "|" .. y
            .. "|" .. pop .. "|" .. queue .. "|" .. major .. "|" .. capital
            .. "|" .. hp .. "|" .. maxhp .. "|" .. food .. "|" .. thr
            .. "|" .. surplus .. "|" .. grow .. "|" .. prodturns
            .. "|" .. buildings .. "|" .. districts)
    end
    end
end
print("---END---")
"""


def _cities_read_lite() -> str:
    """The legacy CITIES|1 read — majors only, fail-loud queue (byte-stable)."""
    return """
print("CITIES|1")
for _, p in ipairs(PlayerManager.GetAliveMajors()) do
    for _, city in p:GetCities():Members() do
        local name = ""
        pcall(function()
            if Locale ~= nil then name = Locale.Lookup(city:GetName()) end
        end)
        if name == nil or name == "" then name = "c" .. city:GetID() end
        -- Codex P2-5: a renamed city carrying | or a newline would tear
        -- the pipe-row parse. | is a plain gsub; control chars go via the
        -- %c class — NO backslash escapes (the tuner lexer rejects them
        -- in command chunks, live-learned run 009)
        name = string.gsub(name, "|", "-")
        name = string.gsub(name, "%c", " ")
        -- InGame queue is authoritative. Missing accessors must not look idle.
        local bq = city:GetBuildQueue()
        if bq == nil or bq.GetCurrentProductionTypeHash == nil then
            error("city production queue unavailable")
        end
        local h = bq:GetCurrentProductionTypeHash()
        if type(h) ~= "number" then error("city production hash unavailable") end
        local queue = "-"
        if h ~= 0 then
            for row in GameInfo.Units() do
                if row.Hash == h then queue = row.UnitType break end
            end
            if queue == "-" then
                for row in GameInfo.Buildings() do
                    if row.Hash == h then queue = row.BuildingType break end
                end
            end
            if string.match(queue, '^UNIT_DISTRICT_') or string.match(queue, '^UNIT_PROJECT_')
                or string.match(queue, '^BUILDING_DISTRICT_')
                or string.match(queue, '^BUILDING_PROJECT_') then
                error('legacy queue collides with reserved productive namespace')
            end
            if queue == "-" and GameInfo.Districts ~= nil then
                for row in GameInfo.Districts() do
                    if row.Hash == h then queue = row.DistrictType break end
                end
            end
            if queue == "-" and GameInfo.Projects ~= nil then
                for row in GameInfo.Projects() do
                    if row.Hash == h then queue = row.ProjectType break end
                end
            end
            if queue == "-" then
                queue = "UNKNOWN_PRODUCTION_" .. tostring(h)
            elseif string.sub(queue, 1, 5) == "UNIT_" then
                queue = string.sub(queue, 6)
            elseif string.sub(queue, 1, 9) == "BUILDING_" then
                queue = string.sub(queue, 10)
            end
        end
        local pop = 1
        pcall(function() pop = math.floor(city:GetPopulation()) end)
        local x = city:GetX() local y = city:GetY()
        print("CITYROW|c" .. p:GetID() .. ":" .. city:GetID()
            .. "|" .. p:GetID() .. "|" .. name
            .. "|" .. (x - math.floor(y / 2)) .. "|" .. y
            .. "|" .. pop .. "|" .. queue)
    end
end
print("---END---")
"""


def visible_map_read(player_id: int, tiles: list[tuple[int, int]]) -> str:
    """M17c targeted terrain read, M4 wire v4 (13-field TILEROW). The
    adapter's DERIVED visibility still decides which coordinates are asked
    about (leak-safe by construction: no coordinate outside the player's
    own-entity sight is ever requested), and the 13th field `engvis` now
    carries the ENGINE's own answer via the PlayersVisibility TABLE route
    — live-probed 2026-09-08 (probe/accessor-matrix-20260908.md): the
    table route works on GameCore (the 2026-08-31 negative finding applied
    to the PLOT-object route), so fog is reachable on the read transport
    and `live.visible_map_context: ingame` is an opt-in experiment, not a
    requirement.

    Token conventions (contract §2): '?' = unread (pcall fallback — the
    parser drops the key), '-' = observed none. Resources are tech-gated
    seatResource()-style per the game's own UI mirror
    (GameInfo.Resources[idx].PrereqTech + techs:HasTech): an unrevealed
    resource reads as '-' on the wire — the seat cannot see it. The Lua
    avoids pattern iteration and escape sequences entirely (the tuner
    lexer rejects both — live-learned run 009)."""
    coords = ", ".join(f"{{{x},{y}}}" for x, y in (axial_to_xy(q, r) for q, r in tiles))
    return f"""
print("VMAP|4")
print("TURN|" .. Game.GetCurrentGameTurn())
local coords = {{ {coords} }}
local techs = nil
pcall(function()
    if Players ~= nil and Players[{player_id}] ~= nil then
        techs = Players[{player_id}]:GetTechs()
    end
end)
local function seatResource(idx)
    local row = nil
    pcall(function() row = GameInfo.Resources[idx] end)
    if row == nil then return "?" end
    local nm = ""
    pcall(function() nm = row.ResourceType or "" end)
    if nm == "" then return "?" end
    local tech = nil
    pcall(function() tech = row.PrereqTech end)
    if tech ~= nil and tech ~= -1 then
        if techs == nil or techs.HasTech == nil then return "?" end
        local ok = false
        pcall(function() ok = techs:HasTech(tech) end)
        if ok ~= true then return "-" end
    end
    return nm
end
local function named(kind, idx)
    local name = "?"
    pcall(function()
        if type(idx) ~= "number" then return end
        if idx < 0 then name = "-" return end
        local cat = GameInfo[kind]
        if cat == nil or cat[idx] == nil then return end
        local v = cat[idx][string.sub(kind, 1, -2) .. "Type"]
        if type(v) == "string" and v ~= "" then
            v = string.gsub(v, "|", "-")
            v = string.gsub(v, "%c", " ")
            name = v
        end
    end)
    return name
end
local function rawIndex(getter)
    local idx = "?"
    pcall(function()
        local v = getter()
        if type(v) == "number" then idx = math.floor(v) end
    end)
    return idx
end
for _, c in ipairs(coords) do
    local plot = nil
    pcall(function() plot = Map.GetPlot(c[1], c[2]) end)
    if plot ~= nil then
        local terrain = "?"
        pcall(function()
            local t = GameInfo.Terrains[plot:GetTerrainType()]
            if t ~= nil then terrain = t.TerrainType end
        end)
        terrain = string.gsub(terrain, "|", "-")
        terrain = string.gsub(terrain, "%c", " ")
        local owner = -1
        pcall(function() owner = plot:GetOwner() end)
        local feature = named("Features", rawIndex(function() return plot:GetFeatureType() end))
        local resource = "?"
        pcall(function()
            local i = plot:GetResourceType()
            if type(i) == "number" then
                if math.floor(i) == -1 then resource = "-" else resource = seatResource(i) end
            end
        end)
        local improvement = named("Improvements",
            rawIndex(function() return plot:GetImprovementType() end))
        local district = named("Districts",
            rawIndex(function() return plot:GetDistrictType() end))
        local river = "?"
        pcall(function()
            if plot.IsRiver ~= nil then river = tostring(plot:IsRiver()) end
        end)
        local appeal = "?"
        pcall(function() appeal = tostring(math.floor(plot:GetAppeal())) end)
        local engvis = "?"
        pcall(function()
            if PlayersVisibility ~= nil and PlayersVisibility[{player_id}] ~= nil then
                engvis = tostring(PlayersVisibility[{player_id}]:IsVisible(plot))
            end
        end)
        local q = c[1] - math.floor(c[2] / 2) local r = c[2]
        print("TILEROW|" .. q .. "|" .. r .. "|" .. terrain
            .. "|true|" .. owner .. "|" .. "|" .. feature .. "|" .. resource
            .. "|" .. improvement .. "|" .. district .. "|" .. river
            .. "|" .. appeal .. "|" .. engvis)
    end
end
print("---END---")
"""


def available_research_read(player_id: int) -> str:
    """Sim-AVAILABLE_RESEARCH-shaped read (GameCore). tech_id is the engine
    TechnologyType minus TECH_ — the same vocabulary the doctrines use."""
    return f"""
print("AVRES|1")
local p = Players[{player_id}]
if p == nil or p.GetTechs == nil then print("---END---") return end
local techs = p:GetTechs()
for row in GameInfo.Technologies() do
    local done = true
    pcall(function() done = techs:HasTech(row.Index) end)
    local can = false
    pcall(function()
        if techs.CanResearch ~= nil then can = techs:CanResearch(row.Index) end
    end)
    if not done and can then
        local nm = row.TechnologyType
        if string.sub(nm, 1, 5) == "TECH_" then nm = string.sub(nm, 6) end
        print("TECHROW|" .. nm .. "|" .. math.floor(row.Cost or 0))
    end
end
print("---END---")
"""


def available_production_read(city_id: str) -> str:
    """Sim-AVAILABLE_PRODUCTION-shaped read (InGame — CanStartOperation
    lives there). item_id is the engine Type minus UNIT_/BUILDING_."""
    owner, raw = decode(city_id, "c")
    return f"""
print("AVPROD|1")
local me = Game.GetLocalPlayer()
if me ~= {owner} then error("production observation owner mismatch") end
local pCity = CityManager.GetCity(me, {raw})
if pCity == nil or pCity:GetID() ~= {raw} or pCity:GetOwner() ~= me then
    error("production observation city unavailable") end
local bq = pCity:GetBuildQueue()
local function cap_number(v)
    if type(v) == "number" and v == v and v >= 0 and v <= 10000
        and v == math.floor(v) then return tostring(math.floor(v)) end
    return "?"
end
local function cap_boolean(v)
    if v == true or v == 1 then return "true" end
    if v == false or v == 0 then return "false" end
    return "?"
end
local function cap_domain(v)
    if v == "DOMAIN_LAND" or v == "DOMAIN_SEA" or v == "DOMAIN_AIR" then return v end
    return "?"
end
for row in GameInfo.Units() do
    local ok = false
    pcall(function() ok = bq:CanProduce(row.Hash, true) end)
    if ok then
        local chk = {{}}
        chk[CityOperationTypes.PARAM_UNIT_TYPE] = row.Hash
        local can = false
        pcall(function()
            can = CityManager.CanStartOperation(pCity, CityOperationTypes.BUILD, chk, true)
        end)
        if can then
            local nm = row.UnitType
            if string.sub(nm, 1, 5) == "UNIT_" then nm = string.sub(nm, 6) end
            local t = -1
            pcall(function() t = math.floor(bq:GetTurnsLeft(row.Hash)) end)
            print("ITEMROW|unit|" .. nm .. "|" .. math.floor(row.Cost or 0) .. "|" .. t
                .. "|" .. cap_number(row.Combat) .. "|" .. cap_number(row.RangedCombat)
                .. "|" .. cap_domain(row.Domain) .. "|" .. cap_boolean(row.FoundCity)
                .. "|" .. cap_number(row.BuildCharges))
        end
    end
end
for row in GameInfo.Buildings() do
    local ok = false
    pcall(function() ok = bq:CanProduce(row.Hash, true) end)
    if ok then
        local chk = {{}}
        chk[CityOperationTypes.PARAM_BUILDING_TYPE] = row.Hash
        local can = false
        pcall(function()
            can = CityManager.CanStartOperation(pCity, CityOperationTypes.BUILD, chk, true)
        end)
        if can then
            local nm = row.BuildingType
            if string.sub(nm, 1, 9) == "BUILDING_" then nm = string.sub(nm, 10) end
            local t = -1
            pcall(function() t = math.floor(bq:GetTurnsLeft(row.Hash)) end)
            print("ITEMROW|building|" .. nm .. "|" .. math.floor(row.Cost or 0) .. "|" .. t)
        end
    end
end
{productive_native.options_lua()}
print("---END---")
"""


# -- turn-blocker housekeeping (M14d; upstream civ6-mcp's notification
# surface). A fresh game's free CODE_OF_LAWS completes during the engine's
# end-of-turn processing and parks TWO ENDTURN_BLOCKING notifications on
# the local player ("Choose a Civic" + "Fill Policy Slot"); a forced
# end-turn bypasses their popups and the WHOLE turn cycle freezes
# (live-learned 2026-08-30, run 011). These run at LEASE START — inside
# our own turn, where policy changes are legal — so the cycle never
# wedges mid-game. SetCivic is NEVER emitted (forbidden: it permanently
# breaks AI civics); the civic choice is SetProgressingCivic.


def blocker_query() -> str:
    """InGame: one BLOCKING|<TYPE> row per distinct end-turn blocker."""
    return """
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


def resolve_civic() -> str:
    """GameCore: set the first not-yet-researched civic as progressing
    (upstream's gate; CanProgress is not a culture-object method)."""
    return """
local me = Game.GetLocalPlayer()
local cu = Players[me]:GetCulture()
for row in GameInfo.Civics() do
    local has = true
    pcall(function() has = cu:HasCivic(row.Index) end)
    if not has then
        cu:SetProgressingCivic(row.Index)
        print("CIVIC_SET|" .. row.CivicType)
        print("---END---")
        return
    end
end
print("CIVIC_FAIL|none-available")
print("---END---")
"""


def fill_policy_slots() -> str:
    """InGame: fill every EMPTY slot with the first unlocked type-matching
    policy via UNLOCK_POLICIES + RequestPolicyChanges (upstream's exact
    sequence — the clear list MUST include every touched slot)."""
    return """
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


def lua_error_probe() -> str:
    """Deliberately erroneous Lua used to verify the error path."""
    return "this is not ( valid lua"


def mod_handshake() -> str:
    """PuppeteerMod capability handshake — the phase-1 live gate.

    Live-learned (2026-08-30): the tuner's Lua lexer rejects backslash
    escapes in patterns ("unfinished string near '[^'"), so no gmatch —
    the mod's print-direct rows ARE the output, and a string-returning
    entry point gets its payload printed WHOLE (the parser splits
    embedded newlines). ``Puppeteer`` nil is an answer, not an error.
    """
    return """
if Puppeteer == nil then
    print("MOD_PRESENT|false")
else
    print("MOD_PRESENT|true")
    Puppeteer.Handshake()
end
print("---END---")
"""


def mod_status() -> str:
    """Pollable puppet/lease state (mod >= 0.2 — D3: poll, never push).

    The vendored connection drains unsolicited messages around every
    command, so hook-time prints are unreliable; Status() is the only
    sound way to observe lease state. Status() RETURNS its rows — one
    multi-line print, split newline-wise by the parser.
    """
    return """
if Puppeteer == nil or Puppeteer.Status == nil then
    print("MOD_STATUS|unavailable")
else
    local s = Puppeteer.Status()
    if s ~= nil then print(s) end
end
print("---END---")
"""


def mod_trace() -> str:
    """Puppeteer.Trace() — the mod v0.3.1 pollable hook-event ring (D3:
    prints from engine callbacks are unsolicited and drained; polling is
    the only sound read). Each line is '<turn>|<EVENT>|<args>'."""
    return """
if Puppeteer == nil or Puppeteer.Trace == nil then
    print("MOD_TRACE|unavailable")
else
    local t = Puppeteer.Trace()
    if t ~= nil and t ~= "" then print(t) end
end
print("---END---")
"""


def mod_digest() -> str:
    """Puppeteer.Digest() — the live before/after state digest rows.
    Print-direct in v0.2; a returning Digest prints its payload whole."""
    return """
if Puppeteer == nil or Puppeteer.Digest == nil then
    print("MOD_DIGEST|unavailable")
else
    local d = Puppeteer.Digest()
    if d ~= nil then print(d) end
end
print("---END---")
"""


# -- mod command surface (v0.2) ----------------------------------------------


def set_puppet(player_id: int, enabled: bool) -> str:
    return f"Puppeteer.SetPuppet({player_id}, {str(enabled).lower()})"


def attach_current_turn(player_id: int, turn: int) -> str:
    """Guarded v0.3.3 initial attachment; the driver must poll the resulting lease."""
    if type(player_id) is not int or player_id < 0 or type(turn) is not int or turn != 1:
        raise ValueError("initial attachment requires a nonnegative player and turn 1")
    return f"Puppeteer.AttachCurrentTurn({player_id}, {turn})"


def switch_local_player(player_id: int) -> str:
    """A2 (2026-09-03, live-proven in the A1 probe): on the
    LoadGame(SERVER_TYPE_NONE) path the engine treats a re-flagged human
    seat like a REMOTE player — it waits on the turn but never makes the
    seat local, so every GetLocalPlayer()-bound act builder and the
    InGame ENDTURN would act as the wrong seat. Switching the local
    player at lease engagement restores the hotseat semantics; this call
    works from the GameCore context mid-game (LOCALP 0->1 observed)."""
    return (f"PlayerManager.SetLocalPlayerAndObserver({player_id}) "
            f"print('LOCAL_SWITCHED|{player_id}|' "
            ".. tostring(Game.GetLocalPlayer())) print('---END---')")


def unpause_local() -> str:
    """A2 belt-and-braces: the wire-side core of the game's own hotseat
    PlayerChange OnOk (playerchange.lua) — clear the seat's WantsPause and
    broadcast. Idempotent when no pause exists; no hand-off panel appears
    on the NONE-load path (the engine goes straight to waiting), so this
    is stall-path insurance, not hot-path."""
    return ("local lp = Game.GetLocalPlayer() "
            "local pc = PlayerConfigurations[lp] "
            "if pc ~= nil and pc.SetWantsPause ~= nil then "
            "pc:SetWantsPause(false) end "
            "pcall(function() Network.BroadcastPlayerInfo(lp) end) "
            "print('UNPAUSED|' .. tostring(lp)) print('---END---')")


def begin_ambient_window(player_id: int) -> str:
    return f"Puppeteer.BeginAmbientWindow({player_id})"


def end_ambient_window(player_id: int) -> str:
    return f"Puppeteer.EndAmbientWindow({player_id})"


def dump_ledger() -> str:
    return "Puppeteer.DumpLedger()"


def dump_ambient() -> str:
    return "Puppeteer.DumpAmbient()"


def release(player_id: int, turn: int = -1) -> str:
    """TURN-BOUND idempotent lease release (Codex P1-8): -1 releases any
    lease for the player (diagnostics); a turn releases only that turn's
    lease — end_phase must never kill the NEXT turn's engaged lease."""
    return f"Puppeteer.Release({player_id}, {turn})"


def restore_unit(unit_id: str) -> str:
    """Restore a full engine ID only under its owner's active lease."""
    owner, raw = decode(unit_id, "u")
    return f'''local restored = Puppeteer.RestoreUnit({raw}, {owner})
print("RESTORE_UNIT|{owner}|{raw}|" .. tostring(restored))
print("---END---")'''


def freeze_unit(unit_id: str) -> str:
    """Undo restore under the same owner binding; never reinterpret IDs."""
    owner, raw = decode(unit_id, "u")
    return f"Puppeteer.FreezeUnit({raw}, {owner})"


def diff_since_last(attrs: str = "", seq: int = 0) -> str:
    """Mod v0.3: the commanded-effects seam. DiffSinceLast() books the
    player's drift since the previous call (or lease start) as LEDGER rows
    and ADVANCES the baseline — so Release's final re-diff books only what
    no command ever covered. The adapter journals these rows as BOTH the
    command's mutations (allowed) and actuals — the watchdog's multiset
    diff then matches them exactly."""
    return f"""
if Puppeteer == nil or Puppeteer.DiffSinceLast == nil then
    print("MOD_DIFF|unavailable")
else
    local d = Puppeteer.DiffSinceLast('{attrs}', {seq})
    if d ~= nil and d ~= "" then print(d) end
end
print("---END---")
"""


def guarded_handoff(player_id: int, turn: int, next_player: int) -> str:
    """One checked GameCore transaction; IDs never interpolate unchecked text."""
    for value in (player_id, turn, next_player):
        if type(value) is not int or value < 0 or value > 2**53 - 1:
            raise ValueError("invalid handoff identity")
    if player_id == next_player or turn < 1:
        raise ValueError("invalid handoff transition")
    return f"Puppeteer.GuardedHandoff({player_id}, {turn}, {next_player})"


def finish_all_moves(player_id: int) -> str:
    """D7-H2 primitive: zero remaining movement so the engine auto-completes."""
    return f"Puppeteer.FinishAllMoves({player_id})"


def request_end_turn(player_id: int) -> str:
    """D7-H1: ENDTURN for the LOCAL player via the UI action bus (InGame
    state — a separate VM from GameCore). Live-learned 2026-08-30:
    SetLocalPlayerAndObserver exists ONLY in GameCore and UI only in
    InGame, so no switch is possible here — but the local player's turn
    needs none. Non-local turn-end (the AI's) takes the H2 FinishMoves
    path in GameCore instead (docs/live-validation.md §6)."""
    return (
        "UI.RequestAction(ActionTypes.ACTION_ENDTURN) "
        f"print('ENDTURN_SENT|{player_id}') print('---END---')"
    )


# -- action surface (M14d; every entry self-contained) -----------------------
#
# The leading `-- arena:tool=<name>` comment is inert to the engine and is
# how the FakeTunerServer's FakeMod identifies which canned handler a
# command reheorses. Every interpolated value is validated Python-side
# (strict int / [A-Z0-9_] token) BEFORE reaching these builders — no
# agent-supplied string is ever spliced into Lua unescaped.


def _bail(tool: str, reason: str, detail: str) -> str:
    return (f"print('ACT|{tool}|ERR|{reason}|{detail}') "
            "print('---END---') return ")


def _owner_guard(owner: int, tool: str) -> str:
    reason = 'NOT_YOUR_CITY' if tool in ('purchase', 'set_city_production') else 'NOT_YOUR_UNIT'
    return f"if me ~= {owner} then {_bail(tool, reason, 'local-owner-mismatch')} end"


def move_unit(unit_id: str, dest: str) -> str:
    """InGame MOVE_TO (upstream's route — the same op a human click issues;
    the engine enforces movement cost/ZOC/terrain). MoveUnit exists only in
    GameCore and bypasses those rules, so it is NOT used for play."""
    owner, num = decode(unit_id, "u")
    q, r = (int(p) for p in dest.split(","))
    x, y = axial_to_xy(q, r)
    return f"""-- arena:tool=move_unit
local me = Game.GetLocalPlayer()
{_owner_guard(owner, "move_unit")}
local unit = UnitManager.GetUnit(me, {num})
if unit == nil or unit:GetID() ~= {num} then {_bail("move_unit", "UNKNOWN_ENTITY", unit_id)} end
if unit:GetMovesRemaining() <= 0 then {_bail("move_unit", "NO_MOVEMENT", unit_id)} end
if not UnitManager.CanStartOperation(unit, UnitOperationTypes.MOVE_TO,
            nil, true) then {_bail("move_unit", "ILLEGAL_MOVE", dest)} end
local params = {{}}
params[UnitOperationTypes.PARAM_X] = {x}
params[UnitOperationTypes.PARAM_Y] = {y}
UnitManager.RequestOperation(unit, UnitOperationTypes.MOVE_TO, params)
print('ACT|move_unit|OK|' .. unit:GetX() .. ',' .. unit:GetY())
print('---END---')"""


def attack(unit_id: str, target_id: str) -> str:
    """InGame ranged attack or melee MOVE_TO, with an explicit target owner."""
    owner, num = decode(unit_id, "u")
    target_owner, target_raw = decode(target_id, "u")
    _cannot = _bail("attack", "CANNOT_ATTACK", target_id)
    return f"""-- arena:tool=attack
local me = Game.GetLocalPlayer()
{_owner_guard(owner, "attack")}
local unit = UnitManager.GetUnit(me, {num})
if unit == nil or unit:GetID() ~= {num} then {_bail("attack", "UNKNOWN_ENTITY", unit_id)} end
local tOwner = {target_owner}
local target = UnitManager.GetUnit(tOwner, {target_raw})
if target == nil or target:GetID() ~= {target_raw} then
    {_bail("attack", "UNKNOWN_ENTITY", target_id)}
end
local tx = target:GetX() local ty = target:GetY()
local info = GameInfo.Units[unit:GetType()]
local ranged = (info ~= nil and info.RangedCombat ~= nil and info.RangedCombat > 0)
local dist = Map.GetPlotDistance(unit:GetX(), unit:GetY(), tx, ty)
local params = {{}}
params[UnitOperationTypes.PARAM_X] = tx
params[UnitOperationTypes.PARAM_Y] = ty
local op = UnitOperationTypes.RANGE_ATTACK
if not ranged or dist <= 1 then
    op = UnitOperationTypes.MOVE_TO
    params[UnitOperationTypes.PARAM_MODIFIERS] = UnitOperationMoveModifiers.ATTACK
end
if not UnitManager.CanStartOperation(unit, op, nil, params) then {_cannot} end
UnitManager.RequestOperation(unit, op, params)
print('ACT|attack|OK|' .. tx .. ',' .. ty)
print('---END---')"""


def fortify(unit_id: str) -> str:
    """InGame FORTIFY with upstream's SLEEP fallback for units that cannot
    fortify (e.g. embarked)."""
    owner, num = decode(unit_id, "u")
    return f"""-- arena:tool=fortify
local me = Game.GetLocalPlayer()
{_owner_guard(owner, "fortify")}
local unit = UnitManager.GetUnit(me, {num})
if unit == nil or unit:GetID() ~= {num} then {_bail("fortify", "UNKNOWN_ENTITY", unit_id)} end
if unit.GetFortifyTurns ~= nil and unit:GetFortifyTurns() > 0 then
    print('ACT|fortify|OK|already')
    print('---END---') return
end
if UnitManager.CanStartOperation(unit, UnitOperationTypes.FORTIFY, nil, true) then
    UnitManager.RequestOperation(unit, UnitOperationTypes.FORTIFY)
    print('ACT|fortify|OK|fortified')
    print('---END---') return
end
local sleepOp = GameInfo.UnitOperations['UNITOPERATION_SLEEP']
if sleepOp ~= nil and UnitManager.CanStartOperation(unit, sleepOp.Hash, nil, true) then
    UnitManager.RequestOperation(unit, sleepOp.Hash)
    print('ACT|fortify|OK|sleeping')
    print('---END---') return
end
{_bail("fortify", "ILLEGAL_MOVE", unit_id)}"""


def found_city(unit_id: str, name: str | None = None) -> str:
    """InGame FOUND_CITY at the settler's tile. The optional name is
    DECLARED-IGNORED (engine auto-names; renaming is not on this surface)."""
    _ = name
    owner, num = decode(unit_id, "u")
    _not_settler = _bail("found_city", "ILLEGAL_MOVE",
                         "not-a-settler-or-blocked")
    return f"""-- arena:tool=found_city
local me = Game.GetLocalPlayer()
{_owner_guard(owner, "found_city")}
local unit = UnitManager.GetUnit(me, {num})
if unit == nil or unit:GetID() ~= {num} then {_bail("found_city", "UNKNOWN_ENTITY", unit_id)} end
if not UnitManager.CanStartOperation(unit, UnitOperationTypes.FOUND_CITY,
            nil, true) then {_not_settler} end
local x = unit:GetX() local y = unit:GetY()
local params = {{}}
params[UnitOperationTypes.PARAM_X] = x
params[UnitOperationTypes.PARAM_Y] = y
UnitManager.RequestOperation(unit, UnitOperationTypes.FOUND_CITY, params)
print('ACT|found_city|OK|' .. x .. ',' .. y)
print('---END---')"""


def set_research(player_id: int, tech_id: str) -> str:
    """GameCore SetResearchingTech with CanResearch pre-check + readback
    (upstream's gamecore route — the InGame player-op silently no-ops)."""
    return f"""-- arena:tool=set_research
local row = GameInfo.Technologies['TECH_{tech_id}']
if row == nil then {_bail("set_research", "ARGS_INVALID", tech_id)} end
local p = Players[{player_id}]
if p == nil or p.GetTechs == nil then {_bail("set_research", "UNKNOWN_ENTITY", "player")} end
local techs = p:GetTechs()
local can = false
pcall(function()
    if techs.CanResearch ~= nil then can = techs:CanResearch(row.Index) end
end)
if not can then {_bail("set_research", "PREREQ_UNMET", tech_id)} end
techs:SetResearchingTech(row.Index)
if techs:GetResearchingTech() == row.Index then
    print('ACT|set_research|OK|{tech_id}')
else
    print('ACT|set_research|ERR|ILLEGAL_MOVE|engine-refused')
end
print('---END---')"""


def _resolve_item_lua(item_id: str, tool: str) -> str:
    """Shared item resolution: units first, then buildings (doctrine
    vocabularies have no UNIT_/BUILDING_ collisions). The param key travels
    as a NAME string — the caller indexes it into the constants table it
    needs (CityOperationTypes for BUILD, CityCommandTypes for PURCHASE)."""
    return f"""-- arena:tool={tool}
local item = GameInfo.Units['UNIT_{item_id}']
local pname = 'PARAM_UNIT_TYPE'
if item == nil then
    item = GameInfo.Buildings['BUILDING_{item_id}']
    pname = 'PARAM_BUILDING_TYPE'
end
if item == nil then {_bail(tool, "ARGS_INVALID", item_id)} end"""


def set_city_production(city_id: str, item_id: str, dest: str | None = None) -> str:
    """InGame CityManager.RequestOperation(BUILD) with upstream's
    VALUE_EXCLUSIVE insert mode — the tool's contract is REPLACE, not
    queue-alongside."""
    if item_id.startswith(("DISTRICT_", "PROJECT_")):
        return productive_native.request_lua(city_id, item_id, dest)
    if dest is not None:
        raise ValueError('placement is supported only for exact district production')
    owner, cid = decode(city_id, "c")
    return f"""{_resolve_item_lua(item_id, "set_city_production")}
local me = Game.GetLocalPlayer()
{_owner_guard(owner, "set_city_production")}
local pCity = CityManager.GetCity(me, {cid})
if pCity == nil or pCity:GetID() ~= {cid} then
    {_bail("set_city_production", "UNKNOWN_ENTITY", city_id)}
end
local bq = pCity:GetBuildQueue()
local tCheck = {{}}
tCheck[CityOperationTypes[pname]] = item.Hash
local can = false
pcall(function()
    can = CityManager.CanStartOperation(pCity, CityOperationTypes.BUILD, tCheck, true)
end)
if not can then {_bail("set_city_production", "PREREQ_UNMET", item_id)} end
local tParams = {{}}
tParams[CityOperationTypes[pname]] = item.Hash
tParams[CityOperationTypes.PARAM_INSERT_MODE] = CityOperationTypes.VALUE_EXCLUSIVE
CityManager.RequestOperation(pCity, CityOperationTypes.BUILD, tParams)
local turns = -1
pcall(function() turns = math.floor(bq:GetTurnsLeft(item.Hash)) end)
-- RequestOperation applies asynchronously. The adapter must poll this hash
-- in subsequent InGame reads before reporting accepted to the agent.
print('PRODUCTION_REQUEST|' .. tostring(item.Hash))
print('ACT|set_city_production|OK|{item_id}|' .. turns)
print('---END---')"""


def current_production_read(city_id: str) -> str:
    """B2: the in-progress production hash for the local player's city
    (0 = nothing building) — the housekeeping gate that stops the
    every-turn re-fill. InGame context (CityManager lives there)."""
    owner, cid = decode(city_id, "c")
    return f"""
local me = Game.GetLocalPlayer()
if me ~= {owner} then print("CURPROD|-1") print("---END---") return end
local pCity = CityManager.GetCity(me, {cid})
if pCity == nil or pCity:GetID() ~= {cid} then
    print('CURPROD|-1') print('---END---') return
end
local bq = pCity:GetBuildQueue()
if bq == nil or bq.GetCurrentProductionTypeHash == nil then
    error("city production queue unavailable")
end
local h = bq:GetCurrentProductionTypeHash()
if type(h) ~= "number" then error("city production hash unavailable") end
print('CURPROD|' .. tostring(h))
print('---END---')"""


def purchase(city_id: str, item_id: str) -> str:
    """InGame CityManager.RequestCommand(PURCHASE), gold only."""
    owner, cid = decode(city_id, "c")
    # hoisted: the bail detail embeds Lua string concat, which cannot sit
    # inside an f-string replacement field (3.12 tokenizer)
    # the detail must be a self-contained Lua expression with BALANCED
    # quotes: reopen the print-string at the start AND reclose at the end
    _insufficient = _bail(
        "purchase", "INSUFFICIENT_GOLD",
        "' .. tostring(cost) .. 'gt' .. tostring(balance) .. '")
    return f"""{_resolve_item_lua(item_id, "purchase")}
local me = Game.GetLocalPlayer()
{_owner_guard(owner, "purchase")}
local pCity = CityManager.GetCity(me, {cid})
if pCity == nil or pCity:GetID() ~= {cid} then {_bail("purchase", "UNKNOWN_ENTITY", city_id)} end
local yieldRow = GameInfo.Yields['YIELD_GOLD']
if yieldRow == nil then {_bail("purchase", "ILLEGAL_MOVE", "no-gold-yield")} end
local formation = MilitaryFormationTypes.STANDARD_MILITARY_FORMATION
local tParams = {{}}
tParams[CityCommandTypes[pname]] = item.Hash
tParams[CityCommandTypes.PARAM_YIELD_TYPE] = yieldRow.Index
if pname == 'PARAM_UNIT_TYPE' then
    tParams[CityCommandTypes.PARAM_MILITARY_FORMATION_TYPE] = formation
end
local cost = 0
pcall(function()
    cost = math.floor(pCity:GetGold():GetPurchaseCost(yieldRow.Index, item.Hash, formation))
end)
local balance = 0
pcall(function() balance = math.floor(Players[me]:GetTreasury():GetGoldBalance()) end)
if cost > balance then {_insufficient} end
local can = false
pcall(function()
    can = CityManager.CanStartCommand(pCity, CityCommandTypes.PURCHASE, false, tParams, true)
end)
if not can then {_bail("purchase", "ILLEGAL_MOVE", "engine-refused")} end
CityManager.RequestCommand(pCity, CityCommandTypes.PURCHASE, tParams)
-- Codex P1-4 readback: OK means the gold was CHARGED, not merely submitted
local after_bal = nil
pcall(function()
    after_bal = math.floor(Players[me]:GetTreasury():GetGoldBalance())
end)
if after_bal ~= nil and after_bal > balance - cost then
    print('ACT|purchase|ERR|ILLEGAL_MOVE|engine-did-not-charge')
    print('---END---') return
end
print('ACT|purchase|OK|{item_id}|' .. cost)
print('---END---')"""


_ = lua_error_probe  # exported for tests


def reward_command_nonce(nonce: str) -> str:
    if not isinstance(nonce, str) or re.fullmatch(r"[0-9a-f]{64}", nonce) is None:
        raise ValueError("invalid reward command nonce")
    return nonce


def begin_reward_command(player: int, turn: int, unit_id: str, nonce: str,
                         dest: str, seq: int) -> str:
    owner, raw = decode(unit_id, "u")
    if type(player) is not int or owner != player:
        raise ValueError("reward command owner mismatch")
    if any(type(v) is not int or not 0 < v < 2**53 for v in (turn, seq)):
        raise ValueError("invalid reward command turn or sequence")
    q, r = map(int, dest.split(","))
    x, y = axial_to_xy(q, r)
    if min(x, y) < 0 or max(x, y) >= 2**53:
        raise ValueError("invalid reward destination")
    return (f"Puppeteer.BeginRewardCommand({player}, {turn}, {raw}, "
            f"'{reward_command_nonce(nonce)}', {x}, {y}, {seq})")


def finish_reward_command(nonce: str, seq: int) -> str:
    if type(seq) is not int or not 0 < seq < 2**53:
        raise ValueError("invalid reward command sequence")
    return (f"Puppeteer.FinishRewardCommand('{reward_command_nonce(nonce)}', "
            f"'pos,moves,damage,exists', {seq})")


def cancel_reward_command(nonce: str) -> str:
    return f"Puppeteer.CancelRewardCommand('{reward_command_nonce(nonce)}')"
