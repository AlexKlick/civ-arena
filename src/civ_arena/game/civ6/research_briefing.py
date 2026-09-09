"""One bounded effective-GameInfo research/building read, never action authority.

Trait/replacement ordering follows shipped techandcivicunlockables.lua. Only
City Center buildings that need no placement are represented. No base XML data
is substituted for the loaded database and no city feasibility is inferred.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re

TOKEN = re.compile(r"[A-Z][A-Z0-9_]{0,95}\Z")
FIELDS = {
    'PrereqCivic': 'CIVIC_', 'PrereqTerrain': 'TERRAIN_', 'AdjacentTerrain': 'TERRAIN_',
    'RequiresAdjacentRiver': 'bool', 'Coast': 'bool', 'RequiresReligion': 'bool',
    'EnabledByReligion': 'bool', 'Cost': 'number', 'Maintenance': 'number',
    'Housing': 'number', 'OuterDefenseHitPoints': 'number', 'OuterDefenseStrength': 'number',
}
LIMITS = {'tech': 128, 'build': 64, 'field': 768, 'yield': 512, 'prereq': 256,
          'modifier': 512, 'trait': 128}


def query(player_id: int) -> str:
    if type(player_id) is not int or not 0 <= player_id <= 1000:
        raise ValueError('research briefing requires exact player identity')
    field_lines = '\n'.join(
        f"emit('RBU_FIELD', id, '{key}', "
        f"{'scalar' if kind == 'number' else 'flag' if kind == 'bool' else 'optional'}"
        f"(row.{key}))" for key, kind in FIELDS.items())
    return f"""-- arena:research_building_briefing=1
local expected = {player_id}
local function integer(value, low, high)
    if type(value) ~= 'number' or value ~= math.floor(value) or value < low or value > high then
        error('research briefing integer unavailable') end
    return string.format('%.0f',value)
end
local function token(value)
    if type(value) ~= 'string' or #value > 96 or not string.match(value,'^[A-Z][A-Z0-9_]*$') then
        error('research briefing exact identifier unavailable') end
    return value
end
local function optional(value) if value == nil then return '?' end return token(value) end
local function flag(value)
    if value == true or value == 1 then return 'true' end
    if value == false or value == 0 then return 'false' end
    if value == nil then return '?' end
    error('research briefing boolean unavailable')
end
local function scalar(value)
    if value == nil then return '?' end
    if type(value) ~= 'number' or value ~= value or math.abs(value) > 1000000000 then
        error('research briefing scalar unavailable') end
    return tostring(value)
end
local counts = {{tech=0,build=0,field=0,yield=0,prereq=0,modifier=0,trait=0}}
local limits = {{tech=128,build=64,field=768,yield=512,prereq=256,modifier=512,trait=128}}
local function emit(prefix, ...)
    local key = string.lower(string.sub(prefix,5))
    counts[key] = counts[key]+1
    if counts[key] > limits[key] then error('research briefing output bound') end
    print(prefix .. '|' .. table.concat({{...}},'|'))
end
local function bounded(name, limit)
    if GameInfo[name] == nil then error('research briefing table unavailable: '..name) end
    local out = {{}}
    for row in GameInfo[name]() do
        if #out >= limit then error('research briefing table bound: '..name) end
        table.insert(out,row)
    end
    return out
end
if Game.GetLocalPlayer() ~= expected then error('research briefing local player mismatch') end
local player = Players[expected]
local config = PlayerConfigurations and PlayerConfigurations[expected]
if player == nil or config == nil then
    error('research briefing player configuration unavailable') end
local techs = player:GetTechs()
local leader = token(config:GetLeaderTypeName())
local civilization = token(config:GetCivilizationTypeName())
local turn = integer(Game.GetCurrentGameTurn(),0,1000000)
print('RBU|1|'..expected..'|'..turn)
print('RBU_ID|'..leader..'|'..civilization)
local traits = {{}}
for _, row in ipairs(bounded('LeaderTraits',2048)) do
    if row.LeaderType == leader then traits[token(row.TraitType)] = true end
end
for _, row in ipairs(bounded('CivilizationTraits',2048)) do
    if row.CivilizationType == civilization then traits[token(row.TraitType)] = true end
end
for trait in pairs(traits) do emit('RBU_TRAIT',trait) end
local available = {{}}
for _, row in ipairs(bounded('Technologies',1024)) do
    local done, can = techs:HasTech(row.Index), techs:CanResearch(row.Index)
    if type(done) ~= 'boolean' or type(can) ~= 'boolean' then
        error('research briefing current research state unavailable') end
    if not done and can then
        local id = token(row.TechnologyType)
        if available[id] then error('duplicate research technology') end
        available[id] = true
        emit('RBU_TECH',id,integer(row.Cost,0,1000000000))
    end
end
-- Complete player-applicable building set BEFORE replacement/direct-tech filtering.
local buildings = {{}}
for _, row in ipairs(bounded('Buildings',1024)) do
    local id = token(row.BuildingType)
    if buildings[id] ~= nil then error('duplicate research building') end
    if row.TraitType ~= 'TRAIT_BARBARIAN' and (row.TraitType == nil or traits[row.TraitType]) then
        buildings[id] = row
    end
end
local removed = {{}}
for _, row in ipairs(bounded('BuildingReplaces',1024)) do
    if buildings[row.CivUniqueBuildingType] then removed[row.ReplacesBuildingType] = true end
end
local selected = {{}}
for id, row in pairs(buildings) do
    if not removed[id] and available[row.PrereqTech]
        and row.PrereqDistrict == 'DISTRICT_CITY_CENTER'
        and flag(row.RequiresPlacement) == 'false' and flag(row.IsWonder) == 'false'
        and flag(row.MustPurchase) == 'false' and flag(row.InternalOnly) == 'false' then
        selected[id] = true
        emit('RBU_BUILD',id,token(row.PrereqTech),optional(row.TraitType))
        {field_lines}
    end
end
for _, row in ipairs(bounded('BuildingPrereqs',2048)) do
    if selected[row.Building] then emit('RBU_PREREQ',row.Building,token(row.PrereqBuilding)) end
end
for _, row in ipairs(bounded('Building_YieldChanges',4096)) do
    if selected[row.BuildingType] then
        if row.YieldChange == nil then error('research briefing yield unavailable') end
        emit('RBU_YIELD',row.BuildingType,token(row.YieldType),scalar(row.YieldChange))
    end
end
for _, row in ipairs(bounded('BuildingModifiers',4096)) do
    if selected[row.BuildingType] then
        emit('RBU_MODIFIER',row.BuildingType,token(row.ModifierId)) end
end
print('RBU_END|'..counts.tech..'|'..counts.build..'|'..counts.field..'|'..counts.yield
    ..'|'..counts.prereq..'|'..counts.modifier..'|'..counts.trait)
print('---END---')
"""


def _token(value, prefix=''):
    if (not isinstance(value, str) or not TOKEN.fullmatch(value) or not value.startswith(prefix)
            or len(value) <= len(prefix)):
        raise ValueError('research briefing exact identifier invalid')
    return value


def _int(value, maximum=1000000000):
    if not re.fullmatch(r'0|[1-9][0-9]*', value) or int(value) > maximum:
        raise ValueError('research briefing integer invalid')
    return int(value)


def _field(value, kind):
    if value == '?':
        return None
    if kind == 'bool':
        if value not in ('true', 'false'):
            raise ValueError('research briefing boolean invalid')
        return value == 'true'
    if kind == 'number':
        if not re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?', value):
            raise ValueError('research briefing numeric spelling invalid')
        number = float(value)
        if not math.isfinite(number) or abs(number) > 1000000000:
            raise ValueError('research briefing numeric bound')
        return int(number) if number.is_integer() else number
    return _token(value, kind)


def parse(lines: list[str], player_id: int) -> dict:
    from civ_arena.game.civ6.response_parser import _split_lines
    if type(player_id) is not int or not 0 <= player_id <= 1000:
        raise ValueError('research briefing requires exact expected player identity')
    if len(lines) > 4096:
        raise ValueError('research briefing wire bound')
    rows = _split_lines(lines)
    if rows and rows[-1] == '---END---':
        rows.pop()
    if not 3 <= len(rows) <= 4096 or any(len(row) > 1024 for row in rows):
        raise ValueError('research briefing frame bound')
    header, identity, end = rows[0].split('|'), rows[1].split('|'), rows[-1].split('|')
    if (len(header) != 4 or header[:2] != ['RBU', '1'] or _int(header[2], 1000) != player_id
            or len(identity) != 3 or identity[0] != 'RBU_ID'
            or len(end) != 8 or end[0] != 'RBU_END'):
        raise ValueError('research briefing identity or framing invalid')
    snapshot = {'version': 1, 'player_id': player_id, 'turn': _int(header[3], 1000000),
                'leader': _token(identity[1], 'LEADER_'),
                'civilization': _token(identity[2], 'CIVILIZATION_'),
                'traits': [], 'technologies': [], 'buildings': []}
    counts = dict.fromkeys(LIMITS, 0)
    techs, buildings, seen = {}, {}, set()
    for line in rows[2:-1]:
        parts = line.split('|')
        kind = parts[0].removeprefix('RBU_').lower()
        if not parts[0].startswith('RBU_') or kind not in LIMITS:
            raise ValueError('research briefing unknown record')
        counts[kind] += 1
        if counts[kind] > LIMITS[kind] or line in seen:
            raise ValueError('research briefing duplicate or excessive records')
        seen.add(line)
        if kind == 'trait' and len(parts) == 2:
            snapshot['traits'].append(_token(parts[1], 'TRAIT_'))
        elif kind == 'tech' and len(parts) == 3:
            tech = _token(parts[1], 'TECH_')
            if tech in techs:
                raise ValueError('duplicate research technology')
            techs[tech] = {'tech_id': tech[5:], 'cost': _int(parts[2])}
        elif kind == 'build' and len(parts) == 4:
            item, tech = _token(parts[1], 'BUILDING_'), _token(parts[2], 'TECH_')
            trait = None if parts[3] == '?' else _token(parts[3], 'TRAIT_')
            if item in buildings or tech not in techs or trait and trait not in snapshot['traits']:
                raise ValueError('research briefing building identity/applicability invalid')
            if item[9:].startswith(('PROJECT_', 'DISTRICT_')):
                raise ValueError('research building collides with productive namespace')
            buildings[item] = {'building_type': item, 'item_id': item[9:],
                              'prereq_tech': tech, 'trait_type': trait, 'fields': {},
                              'prerequisite_buildings': [], 'direct_yields': {}, 'modifiers': []}
        elif kind in {'field', 'yield', 'prereq', 'modifier'}:
            if len(parts) < 3 or parts[1] not in buildings:
                raise ValueError('research briefing orphan detail')
            building = buildings[parts[1]]
            if kind == 'field' and len(parts) == 4 and parts[2] in FIELDS:
                if parts[2] in building['fields']:
                    raise ValueError('duplicate research building field')
                building['fields'][parts[2]] = _field(parts[3], FIELDS[parts[2]])
            elif kind == 'yield' and len(parts) == 4:
                name = _token(parts[2], 'YIELD_')
                if name in building['direct_yields'] or parts[3] == '?':
                    raise ValueError('duplicate or unavailable direct yield')
                building['direct_yields'][name] = _field(parts[3], 'number')
            elif kind == 'prereq' and len(parts) == 3:
                building['prerequisite_buildings'].append(_token(parts[2], 'BUILDING_'))
            elif kind == 'modifier' and len(parts) == 3:
                building['modifiers'].append(_token(parts[2]))
            else:
                raise ValueError('research briefing detail shape invalid')
        else:
            raise ValueError('research briefing row shape invalid')
    if [_int(v) for v in end[1:]] != list(counts.values()):
        raise ValueError('research briefing incomplete frame')
    for building in buildings.values():
        if set(building['fields']) != set(FIELDS):
            raise ValueError('research briefing incomplete building fields')
        building['prerequisite_buildings'].sort()
        building['modifiers'].sort()
    snapshot['traits'].sort()
    snapshot['technologies'] = [techs[key] for key in sorted(techs)]
    snapshot['buildings'] = [buildings[key] for key in sorted(buildings)]
    return package(snapshot)


def package(snapshot: dict) -> dict:
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(',', ':'),
                                       allow_nan=False).encode()).hexdigest()
    return {'options': copy.deepcopy(snapshot['technologies']), 'building_unlocks': {
        'status': 'observed_effective_gameinfo', 'snapshot': snapshot,
        'effective_slice_sha256': digest,
        'source': 'loaded_GameInfo_direct_relations_and_declared_yields',
        'base_source_catalog_used': False, 'loaded_ruleset_identity': 'unverified',
        'scope': 'currently_researchable_tech_to_player_applicable_nonplacement_city_center',
        'support_filter': 'Known-false placement/wonder/purchase-only/internal flags only; '
            'omitted unlocks and null metadata do not establish absence of requirements.',
        'prerequisite_buildings_connective': 'OR',
        'connective_source': 'shipped_base_UI_tooltiphelper.lua_required_buildings_only',
        'city_conditions': 'unknown_until_separate_current_owned_city_observations',
        'modifier_effects': 'not_evaluated', 'research_cost_basis': 'GameInfo_not_effective_turns',
        'action_authority': 'none_fresh_native_catalog_and_legality_still_required'}}


def project(value: dict, player_id: int) -> dict:
    """Reject cross-player or tampered metadata at the player projection boundary."""
    if not isinstance(value, dict) or set(value) != {'options', 'building_unlocks'}:
        raise ValueError('research briefing envelope invalid')
    metadata = value['building_unlocks']
    snapshot = metadata.get('snapshot') if isinstance(metadata, dict) else None
    if (not isinstance(snapshot, dict) or type(snapshot.get('player_id')) is not int
            or snapshot['player_id'] != player_id or type(snapshot.get('version')) is not int
            or snapshot['version'] != 1):
        raise ValueError('research briefing player projection mismatch')
    # Rebuild only the closed native protocol fields, then reparse. Unknown keys,
    # injected detail, malformed scalar types and changed hashes cannot ride through
    # the trusted player projection merely by supplying a self-consistent digest.
    try:
        rows = [f'RBU|1|{player_id}|{snapshot["turn"]}',
                f'RBU_ID|{snapshot["leader"]}|{snapshot["civilization"]}']
        counts = dict.fromkeys(LIMITS, 0)

        def append(kind, *parts):
            counts[kind] += 1
            if counts[kind] > LIMITS[kind]:
                raise ValueError('research briefing projected cardinality exceeded')
            rows.append('RBU_' + kind.upper() + '|' + '|'.join(str(part) for part in parts))

        for trait in snapshot['traits']:
            append('trait', trait)
        for tech in snapshot['technologies']:
            append('tech', 'TECH_' + tech['tech_id'], tech['cost'])
        for building in snapshot['buildings']:
            item = building['building_type']
            append('build', item, building['prereq_tech'], building['trait_type'] or '?')
            for key in FIELDS:
                scalar = building['fields'][key]
                scalar = '?' if scalar is None else (
                    str(scalar).lower() if type(scalar) is bool else scalar)
                append('field', item, key, scalar)
            for key, scalar in building['direct_yields'].items():
                append('yield', item, key, scalar)
            for prerequisite in building['prerequisite_buildings']:
                append('prereq', item, prerequisite)
            for modifier in building['modifiers']:
                append('modifier', item, modifier)
        rows.append('RBU_END|' + '|'.join(str(counts[key]) for key in LIMITS))
        normalized = parse(rows, player_id)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('research briefing projected shape invalid') from exc
    if value != normalized:
        raise ValueError('research briefing metadata or slice digest mismatch')
    return normalized
