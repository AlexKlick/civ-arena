-- M18: PlayerConfigurations instance methods (not enumerable — probe names).
local pc = PlayerConfigurations[1]
local cands = {"SetLeaderType", "SetCivilizationType", "SetPlayerName",
               "GetLeaderType", "GetPlayerName", "SetHuman", "IsHuman",
               "SetSlotStatus", "GetSlotStatus", "SetTeam", "GetID",
               "SetMajorCiv", "IsMajorCiv"}
for _, name in ipairs(cands) do
    print(name .. "|" .. tostring(type(pc[name])))
end
-- SlotStatus is the classic Civ multiplayer knob: 0=Closed 1=Open(human)
-- 2=AI 3=Observer (hypothesis to verify live)
local ok, st = pcall(function() return pc:GetSlotStatus() end)
print("SlotStatus|" .. tostring(ok) .. "|" .. tostring(st))
local okl, lt = pcall(function() return pc:GetLeaderType() end)
print("LeaderType|" .. tostring(okl) .. "|" .. tostring(lt))
print("---END---")
