-- M18 sequencing probe: hotseat MODE + map shape, but NO player seating.
-- If HostGame then opens a STAGING ROOM (instead of auto-launching), the
-- real flow's order is confirmed: host FIRST, seat players in the open
-- staging session, then Network.LaunchGame().
Network.SetLocalNetworkMode(GameModeTypes.HOTSEAT)
GameConfiguration.SetGameMode(GameModeTypes.HOTSEAT)
MapConfiguration.SetMapSize(-601637951)
MapConfiguration.SetMinMajorPlayers(2)
MapConfiguration.SetMaxMajorPlayers(2)
GameConfiguration.SetParticipatingPlayerCount(2)
print("GameMode|" .. tostring(GameConfiguration.GetGameMode()))
print("IsHotseat|" .. tostring(GameConfiguration.IsHotseat()))
print("Humans|" .. tostring(GameConfiguration.GetHumanPlayerCount()))
print("AI|" .. tostring(GameConfiguration.GetAIPlayerCount()))
print("---END---")
