-- M4 step 0: read-only spectator accessor probe (2026-09-08).
-- Works in BOTH the GameCore_Tuner context and the InGame context: the
-- context decides which globals exist; a missing global or method yields a
-- `missing` row, NEVER an error (every accessor call is wrapped in pcall).
--
-- Row vocabulary (one row per accessor call, plus one .raw companion):
--   PROBE|<name>|<type>|<sample>
--   PROBE|<name>.raw|<type>|<v1>,<v2>,...   (up to six returned values)
-- <type> is the Lua type of the first return (number/boolean/string/table/
-- userdata/nil) or one of missing / error / enum. The .raw row carries the
-- comma-joined tostring of EVERY returned value so packing, multi-return
-- and ABGR questions are answerable offline. Table/userdata samples are
-- placeholders (<table>/<userdata>) so repeated runs are byte-identical.
-- Samples have pipes and control characters scrubbed; the source uses no
-- escape sequences and no pattern-walk builtins (the tuner lexer rejects
-- them in command chunks).
--
-- Anchors: player 0, the first NON-major non-barbarian player, the first
-- city found (player 0's, else any), that city's minor counterpart, and
-- the city-centre plot plus its +1 neighbour. A missing anchor emits
-- missing rows for its whole battery and the probe continues.
--
-- This script issues NO game mutation: no Request*, no Set*, no Broadcast,
-- no UI action — reads only. It ends with PROBE_END|<n> then ---END---.

local N = 0

local function samp(v)
  local t = type(v)
  if t == "table" or t == "userdata" or t == "function" or t == "thread" then
    return "<" .. t .. ">"
  end
  return tostring(v)
end

local function clean(v)
  local s = tostring(v)
  s = string.gsub(s, "|", "-")
  s = string.gsub(s, "%c", " ")
  if string.len(s) > 120 then s = string.sub(s, 1, 120) end
  return s
end

local function row(name, typ, sample)
  N = N + 1
  print("PROBE|" .. tostring(name) .. "|" .. tostring(typ) .. "|" .. clean(samp(sample)))
end

-- Call f under pcall; emit the value row plus a .raw row for every return.
local function call(name, f)
  local ok, r1, r2, r3, r4, r5, r6 = pcall(f)
  if not ok then
    row(name, "error", r1)
    return nil
  end
  local t = type(r1)
  row(name, t, r1)
  row(name .. ".raw", t, samp(r1) .. "," .. samp(r2) .. "," .. samp(r3)
    .. "," .. samp(r4) .. "," .. samp(r5) .. "," .. samp(r6))
  return r1, r2, r3
end

-- Probe method `meth` on object `obj` via closure `f`. A nil object or a
-- nil/absent method is `missing`, never an error.
local function m(name, obj, meth, f)
  if obj == nil then
    row(name, "missing", "")
    return nil
  end
  local okf, fn = pcall(function() return obj[meth] end)
  if not okf or fn == nil then
    row(name, "missing", "")
    return nil
  end
  return call(name, f)
end

-- Probe a global-ish value via a getter closure; absent or failing is `missing`.
local function g(name, getter)
  local ok, v = pcall(getter)
  if not ok or v == nil then
    row(name, "missing", "")
    return nil
  end
  row(name, type(v), v)
  return v
end

-- ------------------------------------------------------------------ anchors

local PLAYER_CAP = 12
local DISTRICT_CAP = 8

local enum_list = {}
local p0_anchor, minor_anchor, first_nonmajor, first_major = nil, nil, nil, nil

do
  local list = nil
  pcall(function() list = Game.GetPlayers() end)
  if list == nil then
    row("players.enum", "missing", "")
    row("players.enum.count", "missing", "")
  else
    local n = 0
    for _, p in ipairs(list) do
      if n >= PLAYER_CAP then break end
      n = n + 1
      enum_list[n] = p
      local pid = -1
      pcall(function() pid = p:GetID() end)
      local maj, alive, barb = "?", "?", "?"
      pcall(function()
        if p.IsMajor ~= nil then maj = tostring(p:IsMajor()) end
        if p.IsAlive ~= nil then alive = tostring(p:IsAlive()) end
        if p.IsBarbarian ~= nil then barb = tostring(p:IsBarbarian()) end
      end)
      row("players.enum", "enum", "pid=" .. tostring(pid) .. ",major=" .. maj
        .. ",alive=" .. alive .. ",barb=" .. barb)
      if p0_anchor == nil and pid == 0 then p0_anchor = p end
      if first_major == nil and maj == "true" then first_major = p end
      if first_nonmajor == nil and maj == "false" then first_nonmajor = p end
      if minor_anchor == nil and maj == "false" and barb == "false" then
        minor_anchor = p
      end
    end
    row("players.enum.count", "enum", tostring(n))
  end
end

local function first_city(p)
  if p == nil then return nil end
  local found = nil
  pcall(function()
    local cs = p:GetCities()
    for _, c in cs:Members() do
      found = c
      break
    end
  end)
  return found
end

-- City anchors, mirroring cities_read()'s LIVE-PROVEN enumeration exactly
-- (PlayerManager.GetAliveMajors()/GetAlive() then p:GetCities():Members();
-- the 2026-09-08 live run found no city through the Game.GetPlayers()
-- route, so PlayerManager is primary and each route records what it saw).
local function scan_cities(list, tag, cap)
  local first_any, first_minor, n = nil, nil, 0
  if list == nil then
    row("city.enum." .. tag, "missing", "")
    return nil, nil
  end
  for _, p in ipairs(list) do
    if n >= cap then break end
    n = n + 1
    local pid, maj, barb = -1, "?", "?"
    pcall(function() pid = p:GetID() end)
    pcall(function()
      if p.IsMajor ~= nil then maj = tostring(p:IsMajor()) end
      if p.IsBarbarian ~= nil then barb = tostring(p:IsBarbarian()) end
    end)
    local c, cnt = nil, 0
    pcall(function()
      local cs = p:GetCities()
      local seen = 0
      for _i2, cc in cs:Members() do
        seen = seen + 1
        if seen > 4 then break end
        if c == nil then c = cc end
      end
      cnt = seen
    end)
    row("city.enum." .. tag, "enum", "pid=" .. tostring(pid) .. ",major=" .. maj
      .. ",barb=" .. barb .. ",cities=" .. tostring(cnt))
    if first_any == nil and c ~= nil then first_any = c end
    if first_minor == nil and c ~= nil and maj == "false" and barb == "false" then
      first_minor = c
    end
  end
  row("city.enum." .. tag .. ".count", "enum", tostring(n))
  return first_any, first_minor
end

local pm_majors, pm_alive = nil, nil
pcall(function() pm_majors = PlayerManager.GetAliveMajors() end)
pcall(function() pm_alive = PlayerManager.GetAlive() end)
local city_a, minor_a = scan_cities(pm_majors, "majors", 12)
local city_b, minor_b = scan_cities(pm_alive, "alive", 12)
local city_c = nil
for _, p in ipairs(enum_list) do
  local c = first_city(p)
  if c ~= nil then city_c = c break end
end
row("city.enum.players", "enum", tostring(city_c ~= nil))

local city_anchor = city_a or city_b or city_c
local minor_city = minor_b or minor_a
if minor_anchor == nil then minor_anchor = first_nonmajor end
row("city.anchor.route", "enum",
  city_a ~= nil and "playermanager.majors"
  or city_b ~= nil and "playermanager.alive"
  or city_c ~= nil and "gameplayers-scan"
  or "missing")

-- ------------------------------------------------------------ global tables

local pv_table = g("pv.table", function() return PlayersVisibility end)
local ui = g("ui.table", function() return UI end)
local map = g("map.table", function() return Map end)
local gameinfo = g("gameinfo.table", function() return GameInfo end)
local pcs = g("playerconfigurations.table", function() return PlayerConfigurations end)
g("playermanager.table", function() return PlayerManager end)
if CivilizationLevelTypes == nil then
  row("civilizationleveltypes.table", "missing", "")
else
  row("civilizationleveltypes.table", type(CivilizationLevelTypes), CivilizationLevelTypes)
end

local first_tech, first_building, monument_hash = nil, nil, nil
if gameinfo ~= nil then
  pcall(function()
    for r in gameinfo.Technologies() do first_tech = r break end
  end)
  pcall(function()
    for r in gameinfo.Buildings() do first_building = r break end
  end)
  pcall(function()
    local mr = gameinfo.Buildings["BUILDING_MONUMENT"]
    if mr ~= nil then monument_hash = mr.Hash end
  end)
end
if first_tech == nil then
  row("gameinfo.firsttech", "missing", "")
else
  row("gameinfo.firsttech", "table", "index=" .. samp(first_tech.Index))
end
if first_building == nil then
  row("gameinfo.firstbuilding", "missing", "")
else
  row("gameinfo.firstbuilding", "table", "hash=" .. samp(first_building.Hash))
end
if monument_hash == nil then
  row("gameinfo.monument", "missing", "")
else
  row("gameinfo.monument", "number", monument_hash)
end

-- ------------------------------------------------------------------- plots

local plot_center, plot_neighbor = nil, nil
plot_center = m("city.getplot", city_anchor, "GetPlot",
  function() return city_anchor:GetPlot() end)
local cx, cy = nil, nil
if city_anchor ~= nil then
  pcall(function() cx = city_anchor:GetX() cy = city_anchor:GetY() end)
end
row("city.xy", "enum", "x=" .. tostring(cx) .. ",y=" .. tostring(cy))
if plot_center == nil and map ~= nil and cx ~= nil and cy ~= nil then
  -- visible_map_read's live-proven route
  pcall(function() plot_center = map.GetPlot(cx, cy) end)
end
if plot_neighbor == nil and map ~= nil and cx ~= nil and cy ~= nil then
  pcall(function() plot_neighbor = map.GetNeighborPlot(cx, cy, 2) end)
  if plot_neighbor == nil then
    pcall(function() plot_neighbor = map.GetPlot(cx + 1, cy) end)
  end
end

local function plot_battery(suffix, plot)
  if plot == nil then
    row("plot." .. suffix .. ".xy", "missing", "")
  else
    call("plot." .. suffix .. ".xy", function() return plot:GetX(), plot:GetY() end)
  end
  m("plot." .. suffix .. ".getfeaturetype", plot, "GetFeatureType",
    function() return plot:GetFeatureType() end)
  m("plot." .. suffix .. ".getresourcetype", plot, "GetResourceType",
    function() return plot:GetResourceType() end)
  m("plot." .. suffix .. ".getimprovementtype", plot, "GetImprovementType",
    function() return plot:GetImprovementType() end)
  m("plot." .. suffix .. ".getdistricttype", plot, "GetDistrictType",
    function() return plot:GetDistrictType() end)
  m("plot." .. suffix .. ".isriver", plot, "IsRiver",
    function() return plot:IsRiver() end)
  m("plot." .. suffix .. ".getappeal", plot, "GetAppeal",
    function() return plot:GetAppeal() end)
  m("plot." .. suffix .. ".iscity", plot, "IsCity",
    function() return plot:IsCity() end)
  m("plot." .. suffix .. ".getowner", plot, "GetOwner",
    function() return plot:GetOwner() end)
  m("plot." .. suffix .. ".getterraintype", plot, "GetTerrainType",
    function() return plot:GetTerrainType() end)
end

-- --------------------------------------------------- PlayersVisibility (pv)

local pv0 = nil
if pv_table == nil then
  row("pv.p0", "missing", "")
else
  call("pv.p0", function() pv0 = pv_table[0] return pv0 end)
end
-- IsVisible/IsRevealed on the CITY-CENTRE plot and its NEIGHBOUR (not the
-- map-corner fallback plot).
if plot_center == nil then
  row("pv.p0.isvisible", "missing", "no-city-centre-plot")
  row("pv.p0.isrevealed", "missing", "no-city-centre-plot")
else
  m("pv.p0.isvisible", pv0, "IsVisible", function() return pv0:IsVisible(plot_center) end)
  m("pv.p0.isrevealed", pv0, "IsRevealed", function() return pv0:IsRevealed(plot_center) end)
end
if plot_neighbor == nil then
  row("pv.p0.isvisible.neighbor", "missing", "no-neighbour-plot")
  row("pv.p0.isrevealed.neighbor", "missing", "no-neighbour-plot")
else
  m("pv.p0.isvisible.neighbor", pv0, "IsVisible",
    function() return pv0:IsVisible(plot_neighbor) end)
  m("pv.p0.isrevealed.neighbor", pv0, "IsRevealed",
    function() return pv0:IsRevealed(plot_neighbor) end)
end

-- ------------------------------------------------------------- Plot matrix

plot_battery("center", plot_center)
plot_battery("neighbor", plot_neighbor)

-- ----------------------------------------------------------------- Map

m("map.getplotcount", map, "GetPlotCount", function() return map:GetPlotCount() end)
m("map.getgridsize", map, "GetGridSize", function() return map:GetGridSize() end)
m("map.getplotbyindex0", map, "GetPlotByIndex", function() return map:GetPlotByIndex(0) end)

-- --------------------------------------------------------------- Players

local function player_battery(prefix, p)
  if p == nil then
    row(prefix .. ".anchor", "missing", "")
  else
    row(prefix .. ".anchor", type(p), p)
  end
  local pid = -1
  if p ~= nil then pcall(function() pid = p:GetID() end) end

  local tr = nil
  m(prefix .. ".gettreasury", p, "GetTreasury", function() tr = p:GetTreasury() return tr end)
  m(prefix .. ".treasury.getgold", tr, "GetGold", function() return tr:GetGold() end)
  m(prefix .. ".treasury.getgoldbalance", tr, "GetGoldBalance",
    function() return tr:GetGoldBalance() end)
  m(prefix .. ".treasury.getscience", tr, "GetScience", function() return tr:GetScience() end)
  m(prefix .. ".treasury.getculture", tr, "GetCulture", function() return tr:GetCulture() end)
  m(prefix .. ".treasury.getfaith", tr, "GetFaith", function() return tr:GetFaith() end)
  m(prefix .. ".treasury.getgoldfromdiplomacy", tr, "GetGoldFromDiplomacy",
    function() return tr:GetGoldFromDiplomacy() end)
  -- second-pass yield candidates (2026-09-08 live run: the first-pass
  -- getters came back missing on GameCore; only GetGoldBalance works)
  m(prefix .. ".treasury.getgoldyield", tr, "GetGoldYield",
    function() return tr:GetGoldYield() end)
  m(prefix .. ".treasury.getscienceyield", tr, "GetScienceYield",
    function() return tr:GetScienceYield() end)
  m(prefix .. ".treasury.getfaithyield", tr, "GetFaithYield",
    function() return tr:GetFaithYield() end)
  m(prefix .. ".treasury.gettotalmaintenance", tr, "GetTotalMaintenance",
    function() return tr:GetTotalMaintenance() end)
  m(prefix .. ".treasury.getmaintenance", tr, "GetMaintenance",
    function() return tr:GetMaintenance() end)

  local cu = nil
  m(prefix .. ".getculture", p, "GetCulture", function() cu = p:GetCulture() return cu end)
  local pciv = nil
  m(prefix .. ".culture.getprogressingcivic", cu, "GetProgressingCivic",
    function() pciv = cu:GetProgressingCivic() return pciv end)
  m(prefix .. ".culture.getculturalprogress", cu, "GetCulturalProgress",
    function() return cu:GetCulturalProgress() end)
  m(prefix .. ".culture.getcostnextcivic", cu, "GetCostNextCivic",
    function() return cu:GetCostNextCivic() end)
  if type(pciv) == "number" then
    m(prefix .. ".culture.getculturalprogress.arg", cu, "GetCulturalProgress",
      function() return cu:GetCulturalProgress(pciv) end)
    m(prefix .. ".culture.getcostnextcivic.arg", cu, "GetCostNextCivic",
      function() return cu:GetCostNextCivic(pciv) end)
    m(prefix .. ".culture.hascivic.arg", cu, "HasCivic",
      function() return cu:HasCivic(pciv) end)
  else
    row(prefix .. ".culture.getculturalprogress.arg", "missing", "no-progressing-civic")
    row(prefix .. ".culture.getcostnextcivic.arg", "missing", "no-progressing-civic")
    row(prefix .. ".culture.hascivic.arg", "missing", "no-progressing-civic")
  end
  m(prefix .. ".culture.getcultureyield", cu, "GetCultureYield",
    function() return cu:GetCultureYield() end)

  local tc = nil
  m(prefix .. ".gettechs", p, "GetTechs", function() tc = p:GetTechs() return tc end)
  m(prefix .. ".techs.getscience", tc, "GetScience", function() return tc:GetScience() end)
  m(prefix .. ".techs.hastech.zero", tc, "HasTech", function() return tc:HasTech(0) end)
  if first_tech == nil then
    row(prefix .. ".techs.hastech.first", "missing", "")
  else
    m(prefix .. ".techs.hastech.first", tc, "HasTech",
      function() return tc:HasTech(first_tech.Index) end)
  end

  m(prefix .. ".getera", p, "GetEra", function() return p:GetEra() end)
  m(prefix .. ".getcivilizationleveltype", p, "GetCivilizationLevelType",
    function() return p:GetCivilizationLevelType() end)

  local inf = nil
  m(prefix .. ".getinfluence", p, "GetInfluence",
    function() inf = p:GetInfluence() return inf end)
  m(prefix .. ".influence.getsuzerain", inf, "GetSuzerain",
    function() return inf:GetSuzerain() end)

  local rel = nil
  m(prefix .. ".getreligion", p, "GetReligion", function() rel = p:GetReligion() return rel end)
  m(prefix .. ".religion.getfaithyield", rel, "GetFaithYield",
    function() return rel:GetFaithYield() end)

  local cfg = nil
  if pcs == nil then
    row("playerconfig." .. prefix, "missing", "")
  else
    call("playerconfig." .. prefix, function() cfg = pcs[pid] return cfg end)
  end
  m("playerconfig." .. prefix .. ".getleadertypename", cfg, "GetLeaderTypeName",
    function() return cfg:GetLeaderTypeName() end)
  m("playerconfig." .. prefix .. ".getcivilizationtypename", cfg, "GetCivilizationTypeName",
    function() return cfg:GetCivilizationTypeName() end)
  m("playerconfig." .. prefix .. ".getdifficultytype", cfg, "GetDifficultyType",
    function() return cfg:GetDifficultyType() end)
end

player_battery("p0", p0_anchor)
player_battery("pm", minor_anchor)

local eras = g("game.geteras", function() return Game.GetEras() end)
m("game.geteras.getcurrentera", eras, "GetCurrentEra",
  function() return eras:GetCurrentEra() end)
g("gameinfo.leaders", function() return GameInfo.Leaders end)

-- ----------------------------------------------------------------- Cities

local function city_battery(prefix, c)
  if c == nil then
    row(prefix .. ".anchor", "missing", "")
  else
    row(prefix .. ".anchor", type(c), c)
  end

  m(prefix .. ".iscapital", c, "IsCapital", function() return c:IsCapital() end)

  if first_building == nil then
    row(prefix .. ".hasbuilding.first", "missing", "")
  else
    m(prefix .. ".hasbuilding.first", c, "HasBuilding",
      function() return c:HasBuilding(first_building.Hash) end)
  end
  if monument_hash == nil then
    row(prefix .. ".hasbuilding.monument", "missing", "")
  else
    m(prefix .. ".hasbuilding.monument", c, "HasBuilding",
      function() return c:HasBuilding(monument_hash) end)
  end

  m(prefix .. ".population", c, "GetPopulation", function() return c:GetPopulation() end)

  local gr = nil
  m(prefix .. ".growth", c, "GetGrowth", function() gr = c:GetGrowth() return gr end)
  m(prefix .. ".growth.getfood", gr, "GetFood", function() return gr:GetFood() end)
  m(prefix .. ".growth.getfoodthreshold", gr, "GetFoodThreshold",
    function() return gr:GetFoodThreshold() end)
  m(prefix .. ".growth.getgrowththreshold", gr, "GetGrowthThreshold",
    function() return gr:GetGrowthThreshold() end)
  m(prefix .. ".growth.getfoodsurplus", gr, "GetFoodSurplus",
    function() return gr:GetFoodSurplus() end)
  m(prefix .. ".growth.getturnsleft", gr, "GetTurnsLeft", function() return gr:GetTurnsLeft() end)
  m(prefix .. ".getfood", c, "GetFood", function() return c:GetFood() end)
  m(prefix .. ".getturnsleft", c, "GetTurnsLeft", function() return c:GetTurnsLeft() end)

  local districts = nil
  m(prefix .. ".districts", c, "GetDistricts",
    function() districts = c:GetDistricts() return districts end)
  local first_dtype, dseen = nil, 0
  if districts ~= nil then
    pcall(function()
      if districts.Members == nil then return end
      for _, d in districts:Members() do
        if dseen >= DISTRICT_CAP then break end
        dseen = dseen + 1
        local dt = nil
        pcall(function() dt = d:GetType() end)
        if first_dtype == nil then first_dtype = dt end
        call(prefix .. ".districts.d" .. dseen .. ".gettype",
          function() return d:GetType() end)
        m(prefix .. ".districts.d" .. dseen .. ".iscomplete", d, "IsComplete",
          function() return d:IsComplete() end)
      end
    end)
    row(prefix .. ".districts.count", "enum", tostring(dseen))
  else
    row(prefix .. ".districts.count", "missing", "")
  end

  -- garrison HP via FindID on the first member's district type
  local d0 = nil
  if districts ~= nil and first_dtype ~= nil then
    m(prefix .. ".districts.findid", districts, "FindID",
      function() d0 = districts:FindID(first_dtype) return d0 end)
  else
    row(prefix .. ".districts.findid", "missing", "")
  end
  m(prefix .. ".districts.garrison.getdamage", d0, "GetDamage",
    function() return d0:GetDamage() end)
  m(prefix .. ".districts.garrison.getmaxdamage", d0, "GetMaxDamage",
    function() return d0:GetMaxDamage() end)
  m(prefix .. ".districts.garrison.getcurrenthitpoints", d0, "GetCurrentHitPoints",
    function() return d0:GetCurrentHitPoints() end)

  -- build queue: THIS battery runs for the MAJOR anchor AND the MINOR
  -- anchor — the minor queue may be nil or hash-less.
  local bq = nil
  m(prefix .. ".buildqueue", c, "GetBuildQueue", function() bq = c:GetBuildQueue() return bq end)
  local curh = nil
  m(prefix .. ".buildqueue.getcurrentproductiontypehash", bq, "GetCurrentProductionTypeHash",
    function() curh = bq:GetCurrentProductionTypeHash() return curh end)
  m(prefix .. ".buildqueue.getproduction", bq, "GetProduction",
    function() return bq:GetProduction() end)
  m(prefix .. ".buildqueue.getproductionprogress", bq, "GetProductionProgress",
    function() return bq:GetProductionProgress() end)
  m(prefix .. ".buildqueue.getdomainfreeproduction", bq, "GetDomainFreeProduction",
    function() return bq:GetDomainFreeProduction() end)
  if type(curh) == "number" then
    m(prefix .. ".buildqueue.getproductionprogress.arg", bq, "GetProductionProgress",
      function() return bq:GetProductionProgress(curh) end)
  else
    row(prefix .. ".buildqueue.getproductionprogress.arg", "missing", "no-current-hash")
  end
end

city_battery("city", city_anchor)
city_battery("minorcity", minor_city)

-- --------------------------------------------------------------------- UI

m("ui.getplayercolors", ui, "GetPlayerColors", function() return ui:GetPlayerColors(0) end)
m("ui.getheadselectedcity", ui, "GetHeadSelectedCity",
  function() return ui:GetHeadSelectedCity() end)
m("ui.queryplayerlinecolor", ui, "QueryPlayerLineColor",
  function() return ui:QueryPlayerLineColor(0) end)

print("PROBE_END|" .. N)
print("---END---")
