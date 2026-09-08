"""Spectator-omniscient world capture (M4, contract §3/§4).

Three SPECW reads (roster, owned tiles, palette) plus the OVX|2/CITIES|2
translators compose the world schema v1 — the viewer's territory layer /
roster panel / city-state badge source on BOTH carrier phases: the
hotseat ``spectator_world`` audit and the spectate ``world`` snapshot
block.

Accessor provenance (probe/accessor-matrix-20260908.md, Amendment 1):
roster enumeration, PlayerConfigurations names, GetInfluence:
GetSuzerain, the PlayersVisibility table route, Map grid/plots and every
OVX|2 yield are CONFIRMED on GameCore (read transport). UI.GetPlayerColors
is InGame-only (palette rides the write transport — excluded from the
spectate carrier, whose transport forbids writes).
``player:GetCivilizationLevelType`` exists NOWHERE probed: PLAYERROW's
``level`` stays '?' -> '' (never claimed), and ``kind`` is DERIVED from
the boolean flags. Every unconfirmed accessor is pcall-wrapped: unread ->
key absent, a missing key is never a value.

This module never widens what a SEAT sees: nothing here feeds
arena/visibility.py; the no-leak boundary is unchanged.

Amendment 2: the world doc's parsed-row key sets (players/cities) are
CANONICAL — Lane V's consumer is pinned to them; ``package()`` projects
exactly those keys, and palette ints are normalized to UNSIGNED
[0, 2**32-1] at parse time (the wire keeps the engine's raw signed form).
"""

from __future__ import annotations

import json
import time
from typing import Any

from civ_arena.game.civ6 import lua_translator, response_parser
from civ_arena.game.civ6.spectate_capture import MAX_SNAPSHOT_BYTES

# schema bounds (contract §3)
MAX_ROSTER = 64
MAX_WORLD_CITIES = 256
MAX_OWNED_TILES = 4096
MAX_DISAGREE_COORDS = 64


def roster_read() -> str:
    """SPECW|1|roster — every ALIVE player (majors, city-states, the
    barbarian) with identity + suzerain. GameCore-confirmed accessors
    only; level '?' (missing everywhere probed); kind DERIVED."""
    return """
print("SPECW|1|roster")
local printed = 0
local skipped = 0
for _, p in ipairs(PlayerManager.GetAlive()) do
    local pid = -1
    pcall(function() pid = p:GetID() end)
    local major, barb, alive = "?", "?", "?"
    pcall(function() major = tostring(p:IsMajor()) end)
    pcall(function() barb = tostring(p:IsBarbarian()) end)
    pcall(function() alive = tostring(p:IsAlive()) end)
    local civ, leader = "", ""
    pcall(function()
        if PlayerConfigurations ~= nil and PlayerConfigurations[pid] ~= nil then
            civ = PlayerConfigurations[pid]:GetCivilizationTypeName() or ""
            leader = PlayerConfigurations[pid]:GetLeaderTypeName() or ""
        end
    end)
    civ = string.gsub(civ, "|", "-"); civ = string.gsub(civ, "%c", " ")
    leader = string.gsub(leader, "|", "-"); leader = string.gsub(leader, "%c", " ")
    local suzerain = "?"
    pcall(function()
        if p.GetInfluence ~= nil then
            suzerain = tostring(math.floor(p:GetInfluence():GetSuzerain()))
        end
    end)
    local kind = "city_state"
    if barb == "true" then kind = "barbarian"
    elseif major == "true" then kind = "major" end
    if printed < """ + str(MAX_ROSTER) + """ then
        printed = printed + 1
        print("PLAYERROW|" .. pid .. "|" .. civ .. "|" .. leader
            .. "|" .. major .. "|" .. barb .. "|" .. alive
            .. "|?|" .. kind .. "|" .. suzerain)
    else
        skipped = skipped + 1
    end
end
if skipped > 0 then print("ROSTER_TRUNCATED|" .. skipped) end
print("---END---")
"""


def owned_tiles_read() -> str:
    """SPECW|1|tiles — every OWNED plot on the board (spectator scope:
    omniscient; the raw resource rides WITHOUT the seat tech gate — the
    spectator is not a seat). Framing: GRID, then bounded OWNEDROW rows,
    optional TILES_TRUNCATED, mandatory TILES_END whose count must equal
    the printed rows."""
    return """
print("SPECW|1|tiles")
local w, h = 0, 0
pcall(function() w, h = Map.GetGridSize() end)
if type(w) ~= "number" then w = 0 end
if type(h) ~= "number" then h = 0 end
local n = 0
pcall(function() n = Map.GetPlotCount() end)
if type(n) ~= "number" then n = 0 end
print("GRID|" .. math.floor(w) .. "|" .. math.floor(h) .. "|" .. math.floor(n))
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
local printed = 0
local truncated = 0
for i = 0, math.floor(n) - 1 do
    local plot = nil
    pcall(function() plot = Map.GetPlotByIndex(i) end)
    if plot ~= nil then
        local owner = -1
        pcall(function() owner = plot:GetOwner() end)
        if type(owner) == "number" and owner >= 0 then
            if printed < """ + str(MAX_OWNED_TILES) + """ then
                printed = printed + 1
                local x, y = 0, 0
                pcall(function() x = plot:GetX() y = plot:GetY() end)
                local terrain = "?"
                pcall(function()
                    local t = GameInfo.Terrains[plot:GetTerrainType()]
                    if t ~= nil and t.TerrainType ~= nil then terrain = t.TerrainType end
                end)
                terrain = string.gsub(terrain, "|", "-")
                terrain = string.gsub(terrain, "%c", " ")
                local feature = "?"
                pcall(function()
                    local f = plot:GetFeatureType()
                    if type(f) == "number" then
                        if f < 0 then feature = "-"
                        else feature = named("Features", f) end
                    end
                end)
                local resource = "?"
                pcall(function()
                    local ridx = plot:GetResourceType()
                    if type(ridx) == "number" then
                        if ridx < 0 then resource = "-"
                        else resource = named("Resources", ridx) end
                    end
                end)
                local improvement = "?"
                pcall(function()
                    local iidx = plot:GetImprovementType()
                    if type(iidx) == "number" then
                        if iidx < 0 then improvement = "-"
                        else improvement = named("Improvements", iidx) end
                    end
                end)
                local district = "?"
                pcall(function()
                    local didx = plot:GetDistrictType()
                    if type(didx) == "number" then
                        if didx < 0 then district = "-"
                        else district = named("Districts", didx) end
                    end
                end)
                local river = "?"
                pcall(function()
                    if plot.IsRiver ~= nil then river = tostring(plot:IsRiver()) end
                end)
                local q = x - math.floor(y / 2)
                print("OWNEDROW|" .. q .. "|" .. y .. "|" .. math.floor(owner)
                    .. "|" .. terrain .. "|" .. feature .. "|" .. resource
                    .. "|" .. improvement .. "|" .. district .. "|" .. river
                    .. "|-1")
            else
                truncated = truncated + 1
            end
        end
    end
end
if truncated > 0 then print("TILES_TRUNCATED|" .. truncated) end
print("TILES_END|" .. printed)
print("---END---")
"""


def palette_read() -> str:
    """SPECW|1|palette — UI.GetPlayerColors(pid) raw ints (InGame-only).
    Packing (ABGR vs ARGB) UNVERIFIED live: COLORROW carries the raw ints
    and the viewer keeps the M1 owner-class fallback (Amendment 1.4)."""
    return """
print("SPECW|1|palette")
if UI == nil or UI.GetPlayerColors == nil then
    print("---END---")
    return
end
for _, p in ipairs(PlayerManager.GetAlive()) do
    local pid = -1
    pcall(function() pid = p:GetID() end)
    pcall(function()
        local primary, secondary = UI.GetPlayerColors(pid)
        if type(primary) == "number" and type(secondary) == "number" then
            print("COLORROW|" .. pid .. "|" .. math.floor(primary)
                .. "|" .. math.floor(secondary))
        end
    end)
end
print("---END---")
"""


# -- strict parsers ------------------------------------------------------------


def parse_roster(lines: list[str]) -> list[dict[str, Any]]:
    """PLAYERROW|pid|civ|leader|major|barbarian|alive|level|kind|suzerain
    -> roster docs. Exact framing (Amendment 3 item 8): SPECW|1|roster
    header, zero/one ROSTER_TRUNCATED sentinel, 9-field rows in
    stream order. Duplicate pids, duplicate ROSTER_TRUNCATED, or any
    non-PLAYERROW marker raise. The wire keeps level '?' — the parsed
    doc emits no level key (Amendment 3 item 2).

    Note: the ROSTER_TRUNCATED count is consumed at package() time
    (it carries into ``truncated.roster``) — the parser only validates
    the framing, it does not surface the count.
    """
    rows: list[dict[str, Any]] = []
    pids: set[int] = set()
    header_seen = False
    truncated_seen = False
    for line in response_parser._split_lines(lines):  # noqa: SLF001
        line = line.strip()
        if not line or line == "---END---":
            continue
        if not header_seen:
            if line != "SPECW|1|roster":
                raise ValueError(f"roster read lacks its header: {line!r}")
            header_seen = True
            continue
        prefix, _, rest = line.partition("|")
        if prefix == "ROSTER_TRUNCATED":
            if truncated_seen:
                raise ValueError(
                    f"duplicate ROSTER_TRUNCATED in roster read: {line!r}")
            truncated_seen = True
            value = response_parser._coerce_strict(rest)  # noqa: SLF001
            if type(value) is not int or value < 0:
                raise ValueError(f"malformed ROSTER_TRUNCATED: {line!r}")
            continue
        if prefix != "PLAYERROW":
            raise ValueError(f"non-roster row in world roster read: {line!r}")
        parts = rest.split("|")
        if len(parts) != 9:
            raise ValueError(f"malformed PLAYERROW (want 9 fields): {line!r}")
        pid, civ, leader, major, barb, alive, level, kind, suzerain = parts
        for key, token in (("is_major", major), ("is_barbarian", barb),
                           ("alive", alive)):
            if token not in ("true", "false", "?"):
                raise ValueError(f"non-boolean {key} flag: {line!r}")
        if level != "?":
            raise ValueError(f"PLAYERROW level must stay unread ('?'): {line!r}")
        if kind not in ("major", "city_state", "barbarian"):
            raise ValueError(f"unknown derived kind: {line!r}")
        pid_value = response_parser._coerce_strict(pid)  # noqa: SLF001
        if type(pid_value) is not int or pid_value < 0:
            raise ValueError(f"non-canonical roster pid: {line!r}")
        if pid_value in pids:
            raise ValueError(f"duplicate roster pid {pid_value}: {line!r}")
        pids.add(pid_value)
        doc: dict[str, Any] = {
            "player_id": pid_value,
            "civ_name": civ,
            "leader": leader,
            "kind": kind,
            "suzerain": -1,
        }
        for key, token in (("is_major", major), ("is_barbarian", barb),
                           ("alive", alive)):
            if token != "?":
                doc[key] = token == "true"
        if suzerain != "?":
            value = response_parser._coerce_strict(suzerain)  # noqa: SLF001
            if type(value) is not int:
                raise ValueError(f"non-canonical suzerain: {line!r}")
            doc["suzerain"] = value
        rows.append(doc)
    if not header_seen:
        raise ValueError("roster read is empty")
    if len(rows) > MAX_ROSTER:
        raise ValueError("roster bound exceeded")
    return rows


def parse_owned_tiles(lines: list[str]) -> dict[str, Any]:
    """SPECW|1|tiles framing -> {grid, rows, truncated, ended}.

    Framing state machine (Amendment 3 item 8): one GRID, zero/one
    TILES_TRUNCATED, one terminal TILES_END. Anything else — duplicate
    GRID, duplicate TILES_END, duplicate TILES_TRUNCATED, OWNEDROW after
    TILES_END, foreign markers between OWNEDROWs, OWNEDROW before GRID —
    raises. The TILES_END count must equal the printed OWNEDROW count."""
    header_seen = False
    grid: tuple[int, int] | None = None
    grid_seen = False
    truncated: int | None = None
    truncated_seen = False
    rows: list[dict[str, Any]] = []
    ended: int | None = None
    ended_seen = False
    for line in response_parser._split_lines(lines):  # noqa: SLF001
        line = line.strip()
        if not line or line == "---END---":
            continue
        if not header_seen:
            if line != "SPECW|1|tiles":
                raise ValueError(f"tiles read lacks its header: {line!r}")
            header_seen = True
            continue
        if ended_seen:
            raise ValueError(
                f"rows or markers after TILES_END in tiles read: {line!r}")
        prefix, _, rest = line.partition("|")
        if prefix == "GRID":
            if grid_seen:
                raise ValueError(f"duplicate GRID row in tiles read: {line!r}")
            grid_seen = True
            parts = rest.split("|")
            if len(parts) != 3:
                raise ValueError(f"malformed GRID row: {line!r}")
            w, h, _n = (response_parser._coerce_strict(p) for p in parts)  # noqa: SLF001
            if any(type(v) is not int or v < 0 for v in (w, h, _n)):
                raise ValueError(f"non-canonical GRID row: {line!r}")
            grid = (w, h)
            continue
        if prefix == "TILES_TRUNCATED":
            if truncated_seen:
                raise ValueError(
                    f"duplicate TILES_TRUNCATED in tiles read: {line!r}")
            truncated_seen = True
            value = response_parser._coerce_strict(rest)  # noqa: SLF001
            if type(value) is not int or value < 0:
                raise ValueError(f"malformed TILES_TRUNCATED: {line!r}")
            truncated = value
            continue
        if prefix == "TILES_END":
            ended_seen = True
            value = response_parser._coerce_strict(rest)  # noqa: SLF001
            if type(value) is not int or value < 0:
                raise ValueError(f"malformed TILES_END: {line!r}")
            ended = value
            continue
        if prefix != "OWNEDROW":
            raise ValueError(f"non-tile row in world tiles read: {line!r}")
        if not grid_seen:
            raise ValueError(
                f"OWNEDROW before GRID in tiles read: {line!r}")
        parts = rest.split("|")
        if len(parts) != 10:
            raise ValueError(f"malformed OWNEDROW (want 10 fields): {line!r}")
        (q, r, owner, terrain, feature, resource, improvement, district,
         river, _city) = parts
        doc: dict[str, Any] = {
            "q": response_parser._coerce_strict(q),  # noqa: SLF001
            "r": response_parser._coerce_strict(r),  # noqa: SLF001
            "owner": response_parser._coerce_strict(owner),  # noqa: SLF001
            "terrain": terrain,
        }
        if any(type(doc[k]) is not int for k in ("q", "r", "owner")):
            raise ValueError(f"non-canonical OWNEDROW identity: {line!r}")
        for key, token in (("feature", feature), ("resource", resource),
                           ("improvement", improvement), ("district", district)):
            if token != "?":
                doc[key] = "" if token == "-" else token
        if river in ("true", "false"):
            doc["river"] = river == "true"
        elif river != "?":
            raise ValueError(f"non-boolean river flag: {line!r}")
        rows.append(doc)
    if not header_seen:
        raise ValueError("tiles read is empty")
    if grid is None:
        raise ValueError("tiles read lacks its GRID row")
    if ended is None:
        raise ValueError("tiles read lacks its TILES_END row")
    if ended != len(rows):
        raise ValueError(
            f"TILES_END count {ended} != printed rows {len(rows)}")
    return {"grid": {"w": grid[0], "h": grid[1]}, "rows": rows,
            "truncated": truncated or 0}


def parse_palette(lines: list[str]) -> dict[int, dict[str, int]]:
    """COLORROW|pid|primary|secondary -> {pid: {primary, secondary}}.

    The wire row carries the engine's RAW SIGNED ints (live-probed: Spain
    primary -15395638); the PARSED doc normalizes to the UNSIGNED
    [0, 2**32-1] form the viewer validates (Amendment 2 addendum: v < 0
    -> v + 2**32, and anything outside [-2**31, 2**32-1] is rejected
    BEFORE normalizing — a signed value shipped raw would drop the whole
    world at viewer validation over a packing formality). Amendment 3
    item 8: framing is exact — one header, COLORROWs only; duplicate
    pids or non-COLORROW markers raise."""
    header_seen = False
    out: dict[int, dict[str, int]] = {}

    def _unsigned(value: int) -> int:
        if not -2**31 <= value <= 2**32 - 1:
            raise ValueError(f"palette int outside [-2^31, 2^32-1]: {value!r}")
        return value + 2**32 if value < 0 else value

    for line in response_parser._split_lines(lines):  # noqa: SLF001
        line = line.strip()
        if not line or line == "---END---":
            continue
        if not header_seen:
            if line != "SPECW|1|palette":
                raise ValueError(f"palette read lacks its header: {line!r}")
            header_seen = True
            continue
        prefix, _, rest = line.partition("|")
        if prefix != "COLORROW":
            raise ValueError(f"non-color row in world palette read: {line!r}")
        parts = rest.split("|")
        if len(parts) != 3:
            raise ValueError(f"malformed COLORROW (want 3 fields): {line!r}")
        pid, primary, secondary = (response_parser._coerce_strict(p)  # noqa: SLF001
                                   for p in parts)
        if any(type(v) is not int for v in (pid, primary, secondary)):
            raise ValueError(f"non-canonical COLORROW: {line!r}")
        if pid < 0:
            raise ValueError(f"negative palette player id: {line!r}")
        if pid in out:
            raise ValueError(f"duplicate palette pid {pid}: {line!r}")
        out[pid] = {"primary": _unsigned(primary),
                    "secondary": _unsigned(secondary)}
    if not header_seen:
        raise ValueError("palette read is empty")
    return out


# -- packaging -----------------------------------------------------------------


def _bounded_json(doc: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Mirror spectate_capture._capped's shape (read-only dependency —
    that file's behavior is untouched): bound the doc to
    MAX_SNAPSHOT_BYTES, dropping the largest blocks first, and RECORD the
    truncation (never silent). Grid/roster/digests/counts always survive.

    Amendment 3 item 7 (drop order + postcondition): world →
    palette → cities → players. If the doc still exceeds the cap after
    players is dropped, RAISE — the capture is unsalvageable and the
    caller records ``spectator_world_failed`` rather than ship an
    unbounded doc. The caller already wraps the whole capture in an
    inner asyncio.timeout, so the raise becomes a recorded failure."""
    size = len(json.dumps(doc, sort_keys=True).encode())
    if size <= MAX_SNAPSHOT_BYTES:
        return doc, False
    doc = dict(doc)
    truncated = dict(doc.get("truncated", {}))
    if "owned_tiles_columns" in doc:
        doc.pop("owned_tiles_columns", None)
        size = len(json.dumps(doc, sort_keys=True).encode())
    if size > MAX_SNAPSHOT_BYTES and "palette" in doc:
        doc.pop("palette", None)
        size = len(json.dumps(doc, sort_keys=True).encode())
    if size > MAX_SNAPSHOT_BYTES and doc.get("cities"):
        doc["cities"] = []
        size = len(json.dumps(doc, sort_keys=True).encode())
    if size > MAX_SNAPSHOT_BYTES and doc.get("players"):
        doc["players"] = []
        size = len(json.dumps(doc, sort_keys=True).encode())
    if size > MAX_SNAPSHOT_BYTES:
        raise ValueError(
            f"world doc exceeds {MAX_SNAPSHOT_BYTES} bytes after drop order "
            f"(remaining {size} bytes)")
    truncated["world"] = True
    doc["truncated"] = truncated
    return doc, True


def package(*, roster: list[dict[str, Any]], players: list[dict[str, Any]],
            cities: list[dict[str, Any]], tiles: dict[str, Any],
            palette: dict[int, dict[str, int]] | None,
            after_seat: int, game_era: str | None, read_ms: float) -> dict[str, Any]:
    """Assemble the world schema v1 doc (contract §3 + Amendment 2: the
    parsed-row key sets below are CANONICAL — Lane V's consumer is pinned
    to them; unread fields are ABSENT, never null). Bounds applied with
    RECORDED truncation; blank = unsupplied."""
    # Amendment 2 closed sets — everything else the parsers produce stops
    # here (alive/civic-progress never ride the world doc).
    world_players = [
        {k: v for k, v in row.items() if k in (
            "player_id", "civ_name", "gold", "gold_per_turn", "science",
            "culture", "faith", "upkeep", "era", "researching", "researched",
            "civics")}
        for row in players
    ]
    world_cities = [
        {k: v for k, v in row.items() if k in (
            "city_id", "owner", "q", "r", "name", "population", "is_capital",
            "is_major", "hp", "max_hp", "production_queue", "buildings",
            "districts")}
        for row in cities
    ]
    truncated: dict[str, Any] = {"tiles": bool(tiles.get("truncated")),
                                 "world": False}
    kept_roster = roster[:MAX_ROSTER]
    if len(roster) > MAX_ROSTER:
        truncated["roster"] = len(roster) - MAX_ROSTER
    kept_cities = world_cities[:MAX_WORLD_CITIES]
    if len(world_cities) > MAX_WORLD_CITIES:
        truncated["cities"] = len(world_cities) - MAX_WORLD_CITIES
    columns: dict[str, list[dict[str, Any]]] = {}
    city_at: dict[tuple[int, int], int] = {}
    for c in kept_cities:
        if "q" in c and "r" in c:
            raw = c["city_id"].split(":")[-1]
            if raw.isdigit():
                city_at[(c["q"], c["r"])] = int(raw)
    for total, row in enumerate(tiles.get("rows", [])):
        if total >= MAX_OWNED_TILES:
            truncated["tiles"] = True
            break
        entry: dict[str, Any] = {"q": row["q"], "r": row["r"],
                                 "terrain": row["terrain"]}
        for key in ("feature", "resource", "improvement", "district"):
            if key in row:
                entry[key] = row[key]
        if "river" in row:
            entry["river"] = row["river"]
        entry["city"] = city_at.get((row["q"], row["r"]), -1)
        columns.setdefault(str(row["owner"]), []).append(entry)
    doc = {
        "schema": 1,
        "after_seat": after_seat,
        "contexts": {
            "roster": "gamecore",
            "tiles": "gamecore",
            "palette": "ingame" if palette is not None else "absent",
        },
        "grid": dict(tiles["grid"]),
        "roster": kept_roster,
        "players": world_players,
        "cities": kept_cities,
        "owned_tiles_columns": columns,
        "fog_audit": {"requested": 0, "engine_visible": 0,
                      "engine_not_visible": 0, "unavailable": 0,
                      "disagree_coords": []},
        # Amendment 3 item 9: the world doc carries
        # `palette_confirmed: false` — the viewer trusts the engine
        # palette ints ONLY when this flips to true (separate follow-up
        # commit after live_capture_check verifies the packing against a
        # known leader colour). M4 lands with it false.
        "palette_confirmed": False,
        "truncated": truncated,
        "read_ms": round(read_ms, 3),
    }
    if palette is not None:
        doc["palette"] = {str(pid): dict(colors)
                          for pid, colors in sorted(palette.items())}
    if game_era is not None:
        doc["game_era"] = game_era
    bounded, world_truncated = _bounded_json(doc)
    if world_truncated:
        bounded["truncated"]["world"] = True
    return bounded


async def capture(adapter: Any, *, turn: int, after_seat: int,
                  include_palette: bool) -> dict[str, Any]:
    """One full spectator world (the HOTSEAT carrier, contract §4a):
    SPECW roster + tiles and OVX|2 on the READ transport, CITIES|2 and the
    InGame-only palette on the WRITE transport. Uses read_raw/write_raw +
    the existing translators/parsers — never observe() — so every command
    rides the SAME GameConnection._lock (no second tuner client)."""
    started = time.monotonic()
    roster = parse_roster(await adapter.read_raw(roster_read()))
    tiles = parse_owned_tiles(await adapter.read_raw(owned_tiles_read()))
    overview = response_parser.parse_overview(
        await adapter.read_raw(lua_translator.overview_read()))
    cities = response_parser.parse_cities(
        await adapter.write_raw(lua_translator.cities_read(extended=True)),
        qualified=True)
    palette = None
    if include_palette:
        palette = parse_palette(await adapter.write_raw(palette_read()))
    players = [dict(row) for row in overview.get("players", {}).values()
               if row.get("alive")]
    _ = turn  # the carrier stamps turn on the audit envelope, not the doc
    return package(roster=roster, players=players, cities=cities, tiles=tiles,
                   palette=palette, after_seat=after_seat,
                   game_era=overview.get("game_era"),
                   read_ms=(time.monotonic() - started) * 1000.0)


async def spectate_world(adapter: Any, *, turn: int) -> dict[str, Any]:
    """The SPECTATE carrier's world block (contract §4b): roster + owned
    tiles through the READ transport ONLY — palette (write) and the
    extended cities read are excluded by construction, so this is safe to
    call through SpectateTransport. Economy/cities live in the snapshot's
    own census fields; this block carries what only SPECW can see."""
    started = time.monotonic()
    roster = parse_roster(await adapter.read_raw(roster_read()))
    tiles = parse_owned_tiles(await adapter.read_raw(owned_tiles_read()))
    _ = turn  # the snapshot envelope stamps turn; the doc itself is turn-free
    return package(roster=roster, players=[], cities=[], tiles=tiles,
                   palette=None, after_seat=-1, game_era=None,
                   read_ms=(time.monotonic() - started) * 1000.0)
