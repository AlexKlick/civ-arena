"""Pure Lua-output→semantic parsing (pipe-delimited KEY|value lines)."""

from __future__ import annotations

import re
from typing import Any

from civ_arena.game.civ6 import entity_ids, productive_native
from civ_arena.game.terrain_metadata import native_terrain

# canonical ints only: a token that LOOKS numeric by any other spelling
# (12.5, .5, 1., 1e3, nan, inf, +7) fails closed (Codex P2-9)
_PLAIN_INT = re.compile(r"^-?\d+$")
_NUMERICISH = re.compile(r"^[-+0-9.eE_]+$")


def _split_lines(lines: list[str]) -> list[str]:
    """One print() of a multi-line string arrives as ONE payload with
    embedded newlines (live-learned on the first attach) — flatten every
    incoming element before parsing."""
    out: list[str] = []
    for raw in lines:
        out.extend(raw.splitlines())
    return out


def parse_kv_lines(lines: list[str]) -> dict[str, Any]:
    """Parse ``KEY|value`` lines into a dict (last write wins).

    ``KEY|a|b`` rows (e.g. ``PLAYER|0|CIVILIZATION_ROME``) are collected
    under ``KEY`` as a list of tuples-as-lists.
    """
    out: dict[str, Any] = {}
    for line in _split_lines(lines):
        line = line.strip()
        if not line or line == "---END---":
            continue
        parts = line.split("|")
        if len(parts) == 2:
            out[parts[0]] = _coerce(parts[1])
        elif len(parts) > 2:
            out.setdefault(parts[0], []).append(parts[1:])
    return out


def _coerce(value: str) -> Any:
    if value == "true":
        return True
    if value == "false":
        return False
    try:
        return int(value)
    except ValueError:
        return value


def parse_handshake(lines: list[str]) -> dict[str, Any]:
    """Typed PuppeteerMod handshake doc, FAIL-CLOSED: a missing or false
    capability is False, never defaulted True — the adapter refuses to
    drive a live match unless freeze AND ledger are explicitly true."""
    parsed = parse_kv_lines(lines)
    present = parsed.get("MOD_PRESENT") is True
    return {
        "present": present,
        "mod_version": parsed.get("MOD_VERSION") if present else None,
        "supports_freeze": present and parsed.get("SUPPORTS_FREEZE") is True,
        "supports_ledger": present and parsed.get("SUPPORTS_LEDGER") is True,
        "supports_digest": present and parsed.get("SUPPORTS_DIGEST") is True,
        # mod >= 0.3: without the rolling DiffSinceLast seam, act() cannot
        # reconcile commanded effects and the live driver refuses to dispatch
        "supports_reward_receipts": present
        and parsed.get("SUPPORTS_REWARD_RECEIPTS") is True,
        "supports_guarded_handoff": present
        and parsed.get("SUPPORTS_GUARDED_HANDOFF") is True,
        "supports_command_diff": present
        and parsed.get("SUPPORTS_COMMAND_DIFF") is True,
    }


def _coerce_strict(value: str) -> Any:
    """Canonical-int tripwire: any numeric-looking token that is not a
    plain integer fails LOUDLY here, before it can ride an event log
    canonical() would reject — or worse, slip through as a string."""
    if _PLAIN_INT.match(value):
        return int(value)
    if _NUMERICISH.match(value) or value.lower() in ("nan", "inf", "-inf"):
        raise ValueError(f"non-canonical number on the wire: {value!r}")
    return _coerce(value)


def parse_ledger_lines(lines: list[str], *, qualified: bool = False) -> list[dict[str, Any]]:
    """LEDGER|/AMBIENT| rows -> MutationRecord docs.

    Row shape (both ledgers): ``<PREFIX>|kind|entity_type|entity_id|attr|
    before|after``; the prefix sets ``origin`` ("ledger"=undeclared actual,
    "ambient"=declared manifest). Malformed rows raise — a torn row must
    never silently shrink the watchdog's actual multiset.
    """
    docs: list[dict[str, Any]] = []
    for line in _split_lines(lines):
        line = line.strip()
        if not line or line.startswith("---END---"):
            continue
        prefix, _, rest = line.partition("|")
        if prefix not in ("LEDGER", "AMBIENT"):
            raise ValueError(f"non-ledger row in ledger dump: {line!r}")
        parts = rest.split("|")
        if len(parts) != 6:
            raise ValueError(f"malformed {prefix} row (want 6 fields): {line!r}")
        kind, entity_type, entity_id, attr, before, after = parts
        if qualified and entity_type in ("unit", "city"):
            entity_ids.decode(entity_id, "u" if entity_type == "unit" else "c")
        docs.append({
            "kind": kind,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "attr": attr,
            "before": _coerce_strict(before),
            "after": _coerce_strict(after),
            "origin": prefix.lower(),
        })
    return docs


def parse_digest(lines: list[str], *, qualified: bool = False) -> str:
    """The DIGEST| payload — the live state-hash source text."""
    for line in _split_lines(lines):
        line = line.strip()
        if line.startswith("DIGEST|"):
            value = line[len("DIGEST|"):]
            if qualified:
                seen = set()
                for row in value.split(";"):
                    parts = row.split("|")
                    if parts[0].startswith(("u", "c")):
                        if len(parts) < 2:
                            raise ValueError("digest entity lacks owner")
                        identity = entity_ids.observed(parts[0], _coerce_strict(parts[1]),
                                                       parts[0][0], qualified=True)
                        if identity in seen:
                            raise ValueError("duplicate digest entity ID")
                        seen.add(identity)
            return value
    raise ValueError(f"no DIGEST| row in {lines!r}")


# -- sim-shape observation parses (M14d: shape parity with SimulatorAdapter) --


def parse_units(lines: list[str], *, qualified: bool = False) -> list[dict[str, Any]]:
    """UNITROW|uid|pid|type|q|r|hp|moves|maxmoves|combat|ranged|fortified ->
    the omniscient UNITS doc (projection consumes q/r/owner/hp/movement/
    max_movement/strength/ranged_strength/fortified; ownership checks read
    owner). An optional twelfth field carries a strict native is_barbarian
    boolean. New fourteen-field rows append maximum HP and health-validity;
    invalid native health is explicitly unknown. Legacy eleven/twelve-field rows
    remain byte-compatible parsed observations, without inferred maximum health.
    Sorted by numeric engine id — the sim's deterministic order."""
    out: list[dict[str, Any]] = []
    for line in _split_lines(lines):
        line = line.strip()
        if not line or line.startswith(("UNITS|", "---END---")):
            continue
        prefix, _, rest = line.partition("|")
        if prefix != "UNITROW":
            raise ValueError(f"non-unit row in units read: {line!r}")
        parts = rest.split("|")
        if len(parts) not in (11, 12, 14):
            raise ValueError(f"malformed UNITROW (want 11, 12 or 14 fields): {line!r}")
        metadata = {}
        unknown_health = False
        if len(parts) == 14:
            maximum, valid = parts[-2:]
            parts = parts[:-2]
            hp_token = parts[5]
            if valid == "false" and maximum == hp_token == "unknown":
                metadata.update(max_hp=None, health_valid=False)
                unknown_health = True
            elif (valid == "true" and re.fullmatch(r"[1-9][0-9]{0,6}", maximum)
                  and re.fullmatch(r"0|[1-9][0-9]{0,6}", hp_token)
                  and 0 <= int(hp_token) <= int(maximum) <= 1_000_000):
                metadata.update(max_hp=int(maximum), health_valid=True)
            else:
                raise ValueError("malformed UNITROW native health metadata")
        if len(parts) == 12:
            barbarian = parts.pop()
            if barbarian not in ("true", "false"):
                raise ValueError("malformed UNITROW is_barbarian boolean")
            metadata["is_barbarian"] = barbarian == "true"
        (uid, pid, type_, q, r, hp, moves, maxmoves, combat, ranged,
         fortified) = parts
        out.append({
            "unit_id": entity_ids.observed(uid, _coerce_strict(pid), "u", qualified=qualified),
            "owner": _coerce_strict(pid),
            "type": type_,
            "q": _coerce_strict(q),
            "r": _coerce_strict(r),
            "hp": None if unknown_health else _coerce_strict(hp),
            "movement": _coerce_strict(moves),
            "max_movement": _coerce_strict(maxmoves),
            "strength": _coerce_strict(combat),
            "ranged_strength": _coerce_strict(ranged),
            "fortified": _coerce(fortified),
            **metadata,
        })
    if qualified and len({u["unit_id"] for u in out}) != len(out):
        raise ValueError("duplicate unit identity in observation")
    return sorted(out, key=lambda u: entity_ids.sort_key(u["unit_id"]))


def parse_cities(lines: list[str], *, qualified: bool = False) -> list[dict[str, Any]]:
    """CITYROW -> the omniscient CITIES doc. Legacy 7-field rows
    (cid|pid|name|q|r|population|queue) and M4 18-field extended rows
    (…|queue|major|capital|hp|maxhp|food|thr|surplus|grow|prodturns|
    buildings|districts) both parse.

    M4: the legacy PLACEHOLDER keys (hp/food_bucket/production_bucket/
    buildings) are GONE — an unread key is absent, never a fabricated
    value; hp+max_hp are both-or-neither with 0 <= hp <= max_hp <= 1e6.
    production_queue is a 0/1-length list (the sim's shape); a `?` queue
    (a minor whose build-queue accessor is pcall-guarded) leaves the key
    absent."""
    out: list[dict[str, Any]] = []
    for line in _split_lines(lines):
        line = line.strip()
        if not line or line.startswith(("CITIES|", "---END---")):
            continue
        prefix, _, rest = line.partition("|")
        if prefix != "CITYROW":
            raise ValueError(f"non-city row in cities read: {line!r}")
        parts = rest.split("|")
        if len(parts) not in (7, 18):
            raise ValueError(f"malformed CITYROW (want 7 or 18 fields): {line!r}")
        cid, pid, name, q, r, pop, queue = parts[:7]
        row: dict[str, Any] = {
            "city_id": entity_ids.observed(cid, _coerce_strict(pid), "c", qualified=qualified),
            "owner": _coerce_strict(pid),
            "name": name,
            "q": _coerce_strict(q),
            "r": _coerce_strict(r),
            "population": _coerce_strict(pop),
        }
        if queue == "?":
            pass  # unread (minor's pcall-guarded queue): key absent
        else:
            row["production_queue"] = [] if queue == "-" else [queue]
        if len(parts) == 18:
            (major, capital, hp, maxhp, food, thr, surplus, grow, prodturns,
             buildings, districts) = parts[7:]
            for key, token in (("is_major", major), ("is_capital", capital)):
                if token == "?":
                    continue  # unread (Amendment 1.5: unconfirmed accessors)
                if token not in ("true", "false"):
                    raise ValueError(f"non-boolean {key} flag: {line!r}")
                row[key] = token == "true"
            if hp != "?" or maxhp != "?":
                if hp == "?" or maxhp == "?":
                    raise ValueError(f"hp/max_hp must be both-or-neither: {line!r}")
                hp_v, max_v = _coerce_strict(hp), _coerce_strict(maxhp)
                if (type(hp_v) is not int or type(max_v) is not int
                        or not 0 <= hp_v <= max_v <= 1_000_000):
                    raise ValueError(f"city hp outside 0..max_hp<=1e6: {line!r}")
                row["hp"], row["max_hp"] = hp_v, max_v
            for key, token in (("food_bucket", food), ("food_threshold", thr),
                               ("food_surplus", surplus), ("turns_to_growth", grow),
                               ("turns_to_production", prodturns)):
                if token != "?":
                    value = _coerce_strict(token)
                    if type(value) is not int:
                        raise ValueError(f"non-canonical {key}: {line!r}")
                    row[key] = value
            for key, token in (("buildings", buildings), ("districts", districts)):
                if token != "?":
                    # the wire carries engine TypeNames; buildings strip the
                    # BUILDING_ prefix to match the doctrine/sim vocabulary
                    # (the same convention the queue read uses)
                    names = [] if token == "-" else token.split(";")
                    row[key] = ([n.removeprefix("BUILDING_") for n in names]
                                if key == "buildings" else names)
        out.append(row)
    if qualified and len({c["city_id"] for c in out}) != len(out):
        raise ValueError("duplicate city identity in observation")
    return sorted(out, key=lambda c: entity_ids.sort_key(c["city_id"]))


def _era_name(value) -> str:
    """Amendment 3 item 2: the world doc carries era as a NAME STRING,
    never an int. The wire may carry either:
      - a name string (the live probe path: lua_translator resolves
        via ``GameInfo.Eras[idx].EraType``, emitting e.g. 'ERA_ANCIENT')
      - a canonical int (the rehearsal / FakeMod path)
    Both forms are accepted here — the parser NEVER raises on a
    well-formed era token, regardless of transport. The fallback for
    a string that doesn't resolve is the empty string; for an out-of-
    range int, the stringified int (the previous rehearsal-only
    fallback)."""
    if isinstance(value, str):
        return value
    if type(value) is int:
        return str(value)
    raise ValueError(f"non-canonical era token: {value!r}")


def parse_overview(lines: list[str]) -> dict[str, Any]:
    """OVX read -> the sim OVERVIEW shape the projection consumes: turn +
    players{str(pid): {player_id, civ_name, gold, researched, researching,
    alive}}. researched rides OVRESEARCHED|pid|A;B rows (';'-joined — a
    tech name never contains one).

    M4: 13-field OVROW rows
    (pid|civ|gold|res|sci|cul|fai|gpt|upkeep|era|civic|cprog|ccost) add
    the per-player economy (yields, per-turn gold, upkeep, era) and civic
    progress. ``?`` fields are UNREAD -> key absent (the civic-progress
    triple is InGame-only; read-transport rows carry `?` there). An
    ``OVERA|<name>`` row becomes doc ``game_era``; ``OVCIVICS|pid|A;B``
    (researched civics, ';'-joined) must FOLLOW its player's OVROW."""
    turn: int | None = None
    game_era: str | None = None
    rows: dict[int, dict[str, Any]] = {}
    for line in _split_lines(lines):
        line = line.strip()
        if not line or line.startswith(("OVX|", "---END---")):
            continue
        prefix, _, rest = line.partition("|")
        if prefix == "TURN" and turn is None:
            turn = int(_coerce_strict(rest))
        elif prefix == "OVERA" and game_era is None:
            if not rest or rest == "?" or "|" in rest:
                raise ValueError(f"malformed OVERA row: {line!r}")
            game_era = rest
        elif prefix == "OVROW":
            parts = rest.split("|")
            if len(parts) not in (4, 13):
                raise ValueError(f"malformed OVROW (want 4 or 13 fields): {line!r}")
            pid, civ, gold, res = parts[:4]
            row: dict[str, Any] = {
                "player_id": int(pid),
                "civ_name": civ,
                "gold": _coerce_strict(gold),
                "researched": [],
                "researching": None if res == "-" else res,
                "alive": True,
            }
            if len(parts) == 13:
                sci, cul, fai, gpt, upkeep, era, civic, cprog, ccost = parts[4:]
                for key, token in (("science", sci), ("culture", cul),
                                   ("faith", fai), ("gold_per_turn", gpt),
                                   ("upkeep", upkeep),
                                   ("civic_progress", cprog),
                                   ("civic_cost", ccost)):
                    if token != "?":
                        value = _coerce_strict(token)
                        if type(value) is not int:
                            raise ValueError(f"non-canonical {key}: {line!r}")
                        row[key] = value
                # Amendment 3 item 2 (Codex r2 finding 2): per-player
                # era is a NAME STRING at parse time. The wire may
                # carry either a name string (the live probe path:
                # lua_translator resolves via GameInfo.Eras) or a
                # canonical int (FakeMod / rehearsal). `_era_name`
                # accepts both forms and normalizes to a string. The
                # `?` token keeps the field absent.
                if era != "?":
                    # Try int first (FakeMod / rehearsal emit numeric
                    # eras). If that fails, treat the token as a name
                    # string (the live probe path). Either path is
                    # well-formed; only an unparseable token raises.
                    try:
                        era_value = _coerce_strict(era)
                    except ValueError:
                        era_value = era
                    row["era"] = _era_name(era_value)
                if civic != "?":
                    row["progressing_civic"] = None if civic == "-" else civic
            rows[int(pid)] = row
        elif prefix == "OVRESEARCHED":
            pid_s, _, names = rest.partition("|")
            player = rows.get(int(pid_s))
            if player is None:
                raise ValueError(f"OVRESEARCHED for unknown player: {line!r}")
            player["researched"] = names.split(";") if names else []
        elif prefix == "OVCIVICS":
            pid_s, _, names = rest.partition("|")
            player = rows.get(int(pid_s))
            if player is None:
                raise ValueError(f"OVCIVICS before its OVROW: {line!r}")
            player["civics"] = names.split(";") if names else []
        else:
            raise ValueError(f"non-overview row in overview read: {line!r}")
    if turn is None:
        raise ValueError(f"no TURN row in overview read: {lines!r}")
    doc: dict[str, Any] = {"turn": turn,
                           "players": {str(pid): doc for pid, doc in sorted(rows.items())}}
    if game_era is not None:
        doc["game_era"] = game_era
    return doc


def parse_available_research(lines: list[str]) -> list[dict[str, Any]]:
    """TECHROW|tech_id|cost -> [{tech_id, cost}] sorted by tech_id."""
    out: list[dict[str, Any]] = []
    for line in _split_lines(lines):
        line = line.strip()
        if not line or line.startswith(("AVRES|", "---END---")):
            continue
        prefix, _, rest = line.partition("|")
        if prefix != "TECHROW":
            raise ValueError(f"non-tech row in research read: {line!r}")
        parts = rest.split("|")
        if len(parts) != 2:
            raise ValueError(f"malformed TECHROW (want 2 fields): {line!r}")
        out.append({"tech_id": parts[0], "cost": _coerce_strict(parts[1])})
    return sorted(out, key=lambda t: t["tech_id"])


def parse_available_production(lines: list[str], *,
                               require_productive: bool = False) -> list[dict[str, Any]]:
    """ITEMROW|kind|item_id|cost|turns -> [{item_id, cost, turns, kind}],
    units before buildings (the sim's grouping), id-sorted within each."""
    out: list[dict[str, Any]] = []
    flattened = _split_lines(lines)
    if flattened and flattened[-1] == '---END---':
        flattened.pop()
    productive = any(line.startswith('PRODUCTIVE_OPTIONS_END|') for line in flattened)
    if require_productive and not productive:
        raise ValueError('productive options completeness marker unavailable')
    if productive and (flattened[0] != 'AVPROD|1' or flattened.count('AVPROD|1') != 1
                       or '---END---' in flattened
                       or flattened.count('PRODUCTIVE_OPTIONS_END|1') != 1
                       or flattened[-1:] != ['PRODUCTIVE_OPTIONS_END|1']):
        raise ValueError('productive options framing unavailable')
    seen = set()
    placements_count = 0
    for line in _split_lines(lines):
        line = line.strip()
        if (not line or line.startswith(("AVPROD|", "---END---"))
                or line == "PRODUCTIVE_OPTIONS_END|1"):
            continue
        prefix, _, rest = line.partition("|")
        if prefix != "ITEMROW":
            raise ValueError(f"non-item row in production read: {line!r}")
        parts = rest.split("|")
        if len(parts) not in (4, 5, 9):
            raise ValueError(f"malformed ITEMROW (want 4 or 9 fields): {line!r}")
        kind, item_id, cost, turns = parts[:4]
        if kind not in ("unit", "building", "district", "project"):
            raise ValueError(f"unknown production kind: {line!r}")
        metadata = {}
        if kind in {'district', 'project'}:
            if not productive or productive_native.kind(item_id) != kind:
                raise ValueError('productive identity or completeness marker unavailable')
            if (kind == 'project' and len(parts) != 4) or (kind == 'district' and len(parts) != 5):
                raise ValueError('productive option field count invalid')
            c, t = _coerce_strict(cost), _coerce_strict(turns)
            if (type(c) is not int or not 0 <= c <= 1000000000
                    or type(t) is not int or not -1 <= t <= 1000000):
                raise ValueError('productive cost or turns unavailable')
            if kind == 'district':
                places = parts[4].split(';')
                if (not places or any(not productive_native.COORD.fullmatch(p) for p in places)
                        or len(places) != len(set(places))):
                    raise ValueError('invalid district placement list')
                placements_count += len(places)
                if placements_count > 256:
                    raise ValueError('productive placement bound exceeded')
                metadata['placements'] = sorted(places)
        elif item_id.startswith(('PROJECT_', 'DISTRICT_')):
            raise ValueError('legacy item collides with reserved productive namespace')
        elif len(parts) == 5:
            raise ValueError('placement fields on legacy item')
        if item_id in seen or len(out) >= 1024:
            raise ValueError('duplicate or excessive production options')
        seen.add(item_id)
        if len(parts) == 9:
            if kind != "unit":
                raise ValueError("unit capability fields on non-unit production")
            combat, ranged, domain, found, charges = parts[4:]
            values = {}
            for key, value in (("combat", combat), ("ranged", ranged),
                               ("build_charges", charges)):
                if value == "?":
                    values[key] = None
                else:
                    number = _coerce_strict(value)
                    if type(number) is not int or not 0 <= number <= 10000:
                        raise ValueError("invalid unit capability number")
                    values[key] = number
            if domain not in ("?", "DOMAIN_LAND", "DOMAIN_SEA", "DOMAIN_AIR"):
                raise ValueError("invalid unit capability domain")
            if found not in ("?", "true", "false"):
                raise ValueError("invalid unit capability founding flag")
            metadata["unit_capabilities"] = {**values,
                "domain": None if domain == "?" else domain,
                "found_city": None if found == "?" else found == "true"}
        out.append({"item_id": item_id, "cost": _coerce_strict(cost),
                    "turns": _coerce_strict(turns), "kind": kind, **metadata})
    order = {"unit": 0, "building": 1, "district": 2, "project": 3}
    return sorted(out, key=lambda i: (order[i["kind"]], i["item_id"]))


# Engine TerrainType -> sim terrain. The sim's TERRAIN table is the
# movement-cost authority (reachable_dests indexes it directly), so an
# unmapped engine name would KeyError the planner's compilers — unknown
# names map to PLAINS and are COUNTED, never silently dropped.
_TERRAIN_MAP: dict[str, str] = {
    "GRASS": "GRASSLAND", "GRASS_HILLS": "HILL",
    "PLAINS": "PLAINS", "PLAINS_HILLS": "HILL",
    "DESERT": "DESERT", "DESERT_HILLS": "HILL",
    "TUNDRA": "PLAINS", "TUNDRA_HILLS": "HILL",
    "SNOW": "DESERT", "SNOW_HILLS": "HILL",
    # Exact base-game names. A known mountain must not fall back to walkable plains.
    "GRASS_MOUNTAIN": "MOUNTAIN", "PLAINS_MOUNTAIN": "MOUNTAIN",
    "DESERT_MOUNTAIN": "MOUNTAIN", "TUNDRA_MOUNTAIN": "MOUNTAIN",
    "SNOW_MOUNTAIN": "MOUNTAIN",
    "COAST": "COAST", "OCEAN": "OCEAN",
}


def parse_visible_map(lines: list[str]) -> dict[str, Any]:
    """VMAP read -> the sim visible-map doc plus the visibility split the
    adapter caches. Legacy 6-field TILEROW|q|r|terrain|visible|owner|city
    and M4 13-field rows (…|city|feature|resource|improvement|district|
    river|appeal|engvis) both parse: the Lua reads owner/city ONLY for
    currently-visible plots, so the doc never carries fog ownership (the
    leak-safe side of the projection contract — the projection gates again
    on the visible set, double-gated by design).

    M4 field scopes (the owner rule): the STATIC keys (`feature`, `river`)
    are set regardless of the visibility flag — a remembered tile keeps
    them like its terrain; the DYNAMIC keys (`resource`, `improvement`,
    `district`, `appeal`, `engine_visible`) exist ONLY on `true` rows.
    ``?`` = unread -> key absent; ``-`` = observed none -> ''/False.

    Returns {turn, tiles, visible, unknown_terrain}: tiles is keyed by the
    sim tile_key; ``visible`` is the frozenset of currently-seen keys (the
    adapter splits remembered = revealed - visible).
    """
    turn: int | None = None
    tiles: dict[str, dict[str, Any]] = {}
    visible: set[str] = set()
    unknown = 0
    for line in _split_lines(lines):
        line = line.strip()
        if not line or line.startswith(("VMAP|", "---END---")):
            continue
        if line.startswith("TURN|"):
            turn = _coerce_strict(line.partition("|")[2])
            continue
        prefix, _, rest = line.partition("|")
        if prefix != "TILEROW":
            raise ValueError(f"non-tile row in map read: {line!r}")
        parts = rest.split("|")
        if len(parts) not in (6, 13):
            raise ValueError(f"malformed TILEROW (want 6 or 13 fields): {line!r}")
        q, r, terrain, vis_flag, owner, city = parts[:6]
        key = f"{_coerce_strict(q)},{_coerce_strict(r)}"
        sim_terrain = _TERRAIN_MAP.get(terrain.removeprefix("TERRAIN_"))
        if sim_terrain is None:
            sim_terrain = "PLAINS"
            unknown += 1
        entry: dict[str, Any] = {"terrain": sim_terrain,
                                 "native_terrain": native_terrain(terrain)}
        if vis_flag == "true":
            visible.add(key)
            entry["owner"] = _coerce_strict(owner)
            entry["city"] = city
        elif vis_flag != "false":
            raise ValueError(f"non-boolean visibility flag: {line!r}")
        if len(parts) == 13:
            _feature, _resource, _improvement, _district, river, appeal, engvis \
                = parts[6:]
            # static keys ride every 13-field row
            if _feature != "?":
                entry["feature"] = "" if _feature == "-" else _feature
            if river in ("true", "false"):
                entry["river"] = river == "true"
            elif river != "?":
                raise ValueError(f"non-boolean river flag: {line!r}")
            # dynamic keys only on currently-visible rows
            if vis_flag == "true":
                for token, doc_key in ((_resource, "resource"),
                                       (_improvement, "improvement"),
                                       (_district, "district")):
                    if token != "?":
                        entry[doc_key] = "" if token == "-" else token
                if appeal != "?":
                    value = _coerce_strict(appeal)
                    if type(value) is not int:
                        raise ValueError(f"non-canonical appeal: {line!r}")
                    entry["appeal"] = value
                if engvis in ("true", "false"):
                    entry["engine_visible"] = engvis == "true"
                elif engvis != "?":
                    raise ValueError(f"non-boolean engine visibility: {line!r}")
        if key in tiles:
            raise ValueError(f"duplicate tile row for {key}")
        tiles[key] = entry
    if turn is None:
        raise ValueError("no TURN row in map read")
    return {"turn": turn, "tiles": tiles, "visible": frozenset(visible),
            "unknown_terrain": unknown}


def parse_act(lines: list[str]) -> dict[str, Any]:
    """ACT|tool|OK|detail | ACT|tool|ERR|REASON|detail -> the act verdict.
    Exactly one ACT row per command; missing = fail loud (a torn response
    must never read as success)."""
    row: dict[str, Any] | None = None
    for line in _split_lines(lines):
        line = line.strip()
        if line.startswith("ACT|"):
            if row is not None:
                raise ValueError(f"two ACT rows in one response: {lines!r}")
            parts = line.split("|")
            if len(parts) not in (4, 5) or parts[2] not in ("OK", "ERR"):
                raise ValueError(f"malformed ACT row: {line!r}")
            row = {"tool": parts[1], "status":
                   "accepted" if parts[2] == "OK" else "rejected"}
            if parts[2] == "OK":
                row["detail"] = parts[3]
            else:
                row["rejection"] = parts[3]
                row["detail"] = parts[4] if len(parts) == 5 else ""
    if row is None:
        raise ValueError(f"no ACT row in act response: {lines!r}")
    return row


def parse_restore_receipt(lines: list[str], unit_id: str) -> str:
    """Require one owner/full-ID-bound completion, including idempotent restore."""
    owner, raw = entity_ids.decode(unit_id, "u")
    rows = [line.strip() for line in _split_lines(lines)
            if line.strip() and line.strip() != "---END---"]
    prefix = f"RESTORE_UNIT|{owner}|{raw}|"
    if len(rows) != 1 or rows[0] not in (
            prefix + "restored", prefix + "already_restored", prefix + "unknown_entity"):
        raise RuntimeError(f"invalid restore completion receipt for {unit_id}: {rows!r}")
    return rows[0].removeprefix(prefix)


def parse_handoff_receipt(lines: list[str], player_id: int, turn: int,
                          next_player: int) -> str:
    """Require exactly one receipt bound to this transition; failures stay failures."""
    rows = [line.strip() for line in _split_lines(lines)
            if line.strip() and line.strip() != "---END---"]
    prefix = f"HANDOFF|{player_id}|{turn}|{next_player}|"
    expected = {prefix + "accepted|frozen_then_switched": "accepted",
                prefix + "duplicate|already_sent": "duplicate"}
    if len(rows) != 1 or rows[0] not in expected:
        raise RuntimeError(f"handoff completion not verified: {rows!r}")
    return expected[rows[0]]


def parse_reward_begin(lines: list[str], nonce: str) -> None:
    rows = [r for r in _split_lines(lines) if r and r != "---END---"]
    if len(rows) != 1 or rows[0] not in (
        f"REWARD_BEGIN|{nonce}|accepted", f"REWARD_BEGIN|{nonce}|duplicate"
    ):
        if rows == [f"REWARD_BEGIN|{nonce}|quarantined_missing_event"]:
            raise RuntimeError("reward command quarantined: consumed village lacked native event")
        raise RuntimeError("reward command begin receipt missing or rejected")


def parse_reward_finish(lines: list[str], nonce: str, seq: int, player: int,
                        turn: int, unit_id: str) -> tuple[list[str], list[dict[str, Any]]]:
    rows = [r for r in _split_lines(lines) if r and r != "---END---"]
    headers = [r for r in rows if r.startswith("REWARD_FINISH|")]
    if headers != [f"REWARD_FINISH|{nonce}|{seq}"]:
        raise RuntimeError("reward command finish identity mismatch")
    causes = [r for r in rows if r.startswith("REWARD_CAUSE|")]
    ledger = [r for r in rows if r.startswith("LEDGER|")]
    observations = [r for r in rows if r.startswith("REWARD_OBSERVATION|")]
    if len(observations) != 1:
        raise RuntimeError("missing reward observation")
    obs = observations[0].split("|")
    categories = {"no_matching_event", "duplicate_events", "matched_add_population",
                  "unsupported_or_unmatched", "missing_consumption_event"}
    if len(obs) != 6 or obs[1] != nonce or obs[5] not in categories:
        raise RuntimeError("malformed reward observation")
    event_count, reward_type, reward_subtype = map(_coerce_strict, obs[2:5])
    if any(type(v) is not int for v in (event_count, reward_type, reward_subtype)):
        raise RuntimeError("invalid reward observation numbers")
    if not 0 <= event_count <= 1024 or max(abs(reward_type), abs(reward_subtype)) > 2**32 - 1:
        raise RuntimeError("reward observation outside bounds")
    if ((obs[5] == "matched_add_population") != bool(causes)
            or bool(causes) and event_count != 1):
        raise RuntimeError("reward observation contradicts cause")
    if len(causes) > 1 or len(rows) != len(headers) + len(causes) + len(ledger) + len(observations):
        raise RuntimeError("malformed reward command completion")
    provenance = []
    for cause in causes:
        parts = cause.split("|")
        owner, raw = entity_ids.decode(unit_id, "u")
        if (len(parts) != 11 or parts[1] != nonce or owner != player
                or parts[10] not in ("IMPROVEMENT_GOODY_HUT", "IMPROVEMENT_BARBARIAN_CAMP")
                or parts[2:7] != [str(player), str(turn), str(raw),
                                  "GOODYHUT_SURVIVORS", "GOODYHUT_ADD_POP"]):
            raise RuntimeError("reward causal identity mismatch")
        city = "c" + str(player) + ":" + parts[7]
        entity_ids.decode(city, "c")
        before, after = _coerce_strict(parts[8]), _coerce_strict(parts[9])
        if type(before) is not int or type(after) is not int or before < 1 or after != before + 1:
            raise RuntimeError("invalid reward population delta")
        exact = f"LEDGER|city.growth|city|{city}|population|{before}|{after}"
        if ledger.count(exact) != 1:
            raise RuntimeError("reward cause lacks exact mutation")
        provenance.append({"event": "GoodyHutReward", "command_nonce": nonce,
                           "nonce_origin": "controller_move_window",
                           "player_id": player, "turn": turn, "unit_id": unit_id,
                           "reward_type": "GOODYHUT_SURVIVORS",
                           "reward_subtype": "GOODYHUT_ADD_POP", "city_id": city,
                           "site_improvement": parts[10],
                           "before": before, "after": after})
    # Population must never ride the move attribute scope without a causal row.
    pop = [r for r in ledger if r.split("|")[1:3] == ["city.growth", "city"]]
    if len(pop) != len(provenance):
        raise RuntimeError("unmatched commanded population row")
    if obs[5] == "missing_consumption_event":
        raise RuntimeError("consumed village lacked native event; reward attribution quarantined")
    provenance.append({"event": "GoodyHutReward", "command_nonce": nonce,
                       "observation": obs[5], "event_count": event_count,
                       "native_reward_type": reward_type, "native_reward_subtype": reward_subtype})
    return ledger, provenance
