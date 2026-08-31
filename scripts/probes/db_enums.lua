-- M17b zero-touch: DB-backed hashes for the host sequence. Run in
-- StagingRoom. GameInfo rows carry the same hashed enums SetValue takes.
local function rows(name)
  local t = GameInfo[name]
  if t == nil then print(name .. "|MISSING") return end
  for row in t:Iterator() do
    local parts = {}
    for k, v in pairs(row) do
      if type(v) == "number" or type(v) == "string" then
        table.insert(parts, k .. "=" .. tostring(v))
      end
    end
    table.sort(parts)
    print(name .. "|" .. table.concat(parts, ";"))
  end
end
rows("MapSizes")
rows("GameSpeeds")
rows("HandicapInfos")
print("---END---")
