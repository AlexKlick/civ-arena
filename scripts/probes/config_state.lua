-- M17b zero-touch: current game-config values + the enums the host
-- sequence needs. Run in StagingRoom at the main menu.
print("RuleSet|" .. tostring(GameConfiguration.GetRuleSet()))
print("GameMode|" .. tostring(GameConfiguration.GetGameMode()))
print("GameSpeed|" .. tostring(GameConfiguration.GetGameSpeedType()))
print("Handicap|" .. tostring(GameConfiguration.GetHandicapType()))
print("StartEra|" .. tostring(GameConfiguration.GetStartEra()))
print("MapSize|" .. tostring(MapConfiguration.GetMapSize()))
print("MapScript|" .. tostring(MapConfiguration.GetScript()))
print("MaxMajor|" .. tostring(MapConfiguration.GetMaxMajorPlayers()))
print("MinMajor|" .. tostring(MapConfiguration.GetMinMajorPlayers()))
print("Participating|" .. tostring(GameConfiguration.GetParticipatingPlayerCount()))
print("AvailablePlayers|" .. tostring(GameConfiguration.GetAvailablePlayerCount()))
print("InUsePlayers|" .. tostring(GameConfiguration.GetInUsePlayerCount()))
print("HumanPlayers|" .. tostring(GameConfiguration.GetHumanPlayerCount()))
print("AIPlayers|" .. tostring(GameConfiguration.GetAIPlayerCount()))

local function dump(name, t)
  if t == nil then print(name .. "|MISSING") return end
  local names = {}
  for k, v in pairs(t) do table.insert(names, tostring(k) .. "=" .. tostring(v)) end
  table.sort(names)
  for _, n in ipairs(names) do print(name .. "." .. n) end
end
dump("MapSizeTypes", MapSizeTypes)
dump("GameSpeedTypes", GameSpeedTypes)
dump("HandicapTypes", HandicapTypes)
dump("EraTypes", EraTypes)
print("---END---")
