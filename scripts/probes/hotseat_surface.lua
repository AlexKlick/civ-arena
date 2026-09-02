-- M18: the hotseat setup surface. Run in StagingRoom at the menu.
print("PlayerConfigurations|" .. type(PlayerConfigurations))
local ok, pc = pcall(function() return PlayerConfigurations[0] end)
print("PC0|" .. tostring(ok) .. "|" .. tostring(pc ~= nil))
if pc ~= nil then
    local names = {}
    pcall(function()
        for k in pairs(pc) do table.insert(names, tostring(k)) end
    end)
    table.sort(names)
    print("PC0_KEYS|" .. table.concat(names, ","))
end
print("GameMode|" .. tostring(GameConfiguration.GetGameMode()))
local okh, hs = pcall(function() return GameModeTypes.HOTSEAT end)
print("HOTSEAT|" .. tostring(okh) .. "|" .. tostring(hs))
-- participating count and humans under the CURRENT (SP) mode
print("Participating|" .. tostring(GameConfiguration.GetParticipatingPlayerCount()))
print("Humans|" .. tostring(GameConfiguration.GetHumanPlayerCount()))
print("AI|" .. tostring(GameConfiguration.GetAIPlayerCount()))
print("---END---")
