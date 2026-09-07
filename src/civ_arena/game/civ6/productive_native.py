"""Closed district/project production, separate from recorder mutation coverage.

Only the existing production facade dispatches these requests. A request is sent
once; exact subsequent queue/plot observations establish admission, not completion.
"""

from __future__ import annotations

import re

from civ_arena.game.civ6.entity_ids import decode

TYPE = re.compile(r"^(DISTRICT|PROJECT)_[A-Z][A-Z0-9_]{0,63}$")
COORD = re.compile(r"^(0|-?[1-9][0-9]{0,5}),(0|-?[1-9][0-9]{0,5})$")


def kind(item: str) -> str | None:
    match = TYPE.fullmatch(item) if isinstance(item, str) else None
    return match[1].lower() if match else None


def validate(item: str, dest: str | None) -> str:
    family = kind(item)
    if family is None or (family == "project" and dest is not None):
        raise ValueError("productive request requires exact typed identity and placement shape")
    if family == "district" and (not isinstance(dest, str) or not COORD.fullmatch(dest)):
        raise ValueError("district requires canonical placement coordinate")
    return family


# Calls may throw on unavailable native accessors: never turn errors into empty
# catalogs or into a permission to send a request. Every queried target is bounded.
_HELPERS = """
local function exactNumber(v, low, high)
    if type(v) ~= 'number' or v ~= v or v ~= math.floor(v) or v < low or v > high then
        error('productive native number unavailable')
    end
    return v
end
local function exactBool(v)
    if type(v) ~= 'boolean' then error('productive native boolean unavailable') end
    return v
end
local function clearPlot(plot)
    if plot == nil then error('productive plot unavailable') end
    local x, y = plot:GetX(), plot:GetY()
    if PlayersVisibility == nil or PlayersVisibility[me] == nil then
        error('productive native visibility unavailable')
    end
    if not exactBool(PlayersVisibility[me]:IsVisible(x,y)) then return false end
    if exactNumber(plot:GetOwner(),-1,1000) ~= me then return false end
    if exactBool(plot:IsCity()) then return false end
    return exactNumber(plot:GetDistrictType(),-1,1000000) == -1
       and exactNumber(plot:GetFeatureType(),-1,1000000) == -1
       and exactNumber(plot:GetImprovementType(),-1,1000000) == -1
       and exactNumber(plot:GetResourceType(),-1,1000000) == -1
end
local totalTargetCount = 0
local function targets(row)
    local params = {}
    params[CityOperationTypes.PARAM_DISTRICT_TYPE] = row.Hash
    local result = CityManager.GetOperationTargets(pCity, CityOperationTypes.BUILD, params)
    if type(result) ~= 'table' then error('productive target result unavailable') end
    local plots = result[CityOperationResults.PLOTS]
    if plots == nil then error('productive PLOTS result unavailable') end
    if type(plots) ~= 'table' or #plots > 256 then error('productive target bound/type') end
    for key in pairs(plots) do
        if type(key) ~= 'number' or key ~= math.floor(key) or key < 1 or key > #plots then
            error('productive targets must be a dense array') end
    end
    local accepted, seen = {}, {}
    for _, index in ipairs(plots) do
        totalTargetCount = totalTargetCount + 1
        if totalTargetCount > 256 then error('productive total target bound') end
        exactNumber(index,0,10000000)
        if seen[index] then error('duplicate productive target') end
        seen[index] = true
        local plot = Map.GetPlotByIndex(index)
        if clearPlot(plot) then
            local check = {}
            check[CityOperationTypes.PARAM_DISTRICT_TYPE] = row.Hash
            check[CityOperationTypes.PARAM_X] = plot:GetX()
            check[CityOperationTypes.PARAM_Y] = plot:GetY()
            if exactBool(CityManager.CanStartOperation(
                pCity,CityOperationTypes.BUILD,check,true)) then
                table.insert(accepted, plot)
            end
        end
    end
    return accepted
end
"""


def options_lua() -> str:
    """Append to the existing local-player/city/bq production query."""
    return (
        _HELPERS
        + """
if GameInfo.Projects == nil or GameInfo.Districts == nil then
    error('productive catalogs unavailable')
end
local projectCount, districtCount, placementCount = 0, 0, 0
for row in GameInfo.Projects() do
    projectCount = projectCount + 1
    if projectCount > 128 then error('productive project bound') end
    if exactBool(bq:CanProduce(row.Hash,true))
        and exactBool(bq:CanProduce(row.Hash,false,true)) then
        local check = {}
        check[CityOperationTypes.PARAM_PROJECT_TYPE] = row.Hash
        if exactBool(CityManager.CanStartOperation(
                pCity,CityOperationTypes.BUILD,check,true)) then
            print('ITEMROW|project|' .. row.ProjectType .. '|'
                .. exactNumber(bq:GetProjectCost(row.Index),0,1000000000) .. '|'
                .. exactNumber(bq:GetTurnsLeft(row.ProjectType),-1,1000000))
        end
    end
end
for row in GameInfo.Districts() do
    districtCount = districtCount + 1
    if districtCount > 128 then error('productive district bound') end
    if exactBool(bq:CanProduce(row.Hash,true)) and exactBool(bq:CanProduce(row.Hash,false,true))
        and not exactBool(bq:HasBeenPlaced(row.Hash)) then
        local placements = {}
        for _, plot in ipairs(targets(row)) do
            placementCount = placementCount + 1
            if placementCount > 256 then error('productive placement bound') end
            local x,y = plot:GetX(),plot:GetY()
            table.insert(placements,tostring(x-math.floor(y/2)) .. ',' .. tostring(y))
        end
        if #placements > 0 then
            table.sort(placements)
            print('ITEMROW|district|' .. row.DistrictType .. '|'
                .. exactNumber(bq:GetDistrictCost(row.Index),0,1000000000) .. '|'
                .. exactNumber(bq:GetTurnsLeft(row.DistrictType),-1,1000000) .. '|'
                .. table.concat(placements,';'))
        end
    end
end
print('PRODUCTIVE_OPTIONS_END|1')
"""
    )


def _city(city_id: str) -> str:
    owner, raw = decode(city_id, "c")
    return f"""
local me = Game.GetLocalPlayer()
if me ~= {owner} then error('productive local player mismatch') end
local pCity = CityManager.GetCity(me,{raw})
if pCity == nil or pCity:GetID() ~= {raw} or pCity:GetOwner() ~= me then
    error('productive city unavailable or ownership changed')
end
local bq = pCity:GetBuildQueue()
if bq == nil then error('productive build queue unavailable') end
"""


def request_lua(city_id: str, item: str, dest: str | None) -> str:
    family = validate(item, dest)
    x = y = -1
    if dest is not None:
        q, y = map(int, dest.split(","))
        x = q + y // 2
    table, field, parameter = (
        ("Districts", "DistrictType", "PARAM_DISTRICT_TYPE")
        if family == "district"
        else ("Projects", "ProjectType", "PARAM_PROJECT_TYPE")
    )
    return f"""-- arena:tool=set_city_production
-- arena:productive={family}|{item}|{x}|{y}
{_city(city_id)}
{_HELPERS}
local function reject(reason)
    print('ACT|set_city_production|ERR|' .. reason .. '|productive-refused')
    print('---END---')
end
if exactNumber(bq:GetCurrentProductionTypeHash(),-9007199254740991,9007199254740991) ~= 0 then
    reject('ALREADY') return
end
if GameInfo.{table} == nil then error('productive catalog unavailable') end
local row = GameInfo.{table}['{item}']
if row == nil or row.{field} ~= '{item}' then reject('ARGS_INVALID') return end
if GameInfo.Units['UNIT_{item}'] ~= nil or GameInfo.Buildings['BUILDING_{item}'] ~= nil then
    reject('ARGS_INVALID') return
end
local hash = exactNumber(row.Hash,-9007199254740991,9007199254740991)
if hash == 0 then error('productive hash unavailable') end
if not exactBool(bq:CanProduce(hash,true)) or not exactBool(bq:CanProduce(hash,false,true)) then
    reject('PREREQ_UNMET') return
end
local params = {{}}
params[CityOperationTypes.{parameter}] = hash
local plotIndex, districtIndex = -1, -1
if '{family}' == 'district' then
    if exactBool(bq:HasBeenPlaced(hash)) then reject('ALREADY') return end
    local found = nil
    for _, plot in ipairs(targets(row)) do
        if plot:GetX() == {x} and plot:GetY() == {y} then found = plot end
    end
    if found == nil then reject('ILLEGAL_DEST') return end
    plotIndex = exactNumber(found:GetIndex(),0,10000000)
    districtIndex = exactNumber(row.Index,0,1000000)
    params[CityOperationTypes.PARAM_X] = {x}
    params[CityOperationTypes.PARAM_Y] = {y}
end
if not exactBool(CityManager.CanStartOperation(pCity,CityOperationTypes.BUILD,params,true)) then
    reject('PREREQ_UNMET') return
end
params[CityOperationTypes.PARAM_INSERT_MODE] = CityOperationTypes.VALUE_EXCLUSIVE
CityManager.RequestOperation(pCity,CityOperationTypes.BUILD,params)
print('PRODUCTIVE_REQUEST|{family}|{item}|' .. hash .. '|' .. plotIndex .. '|' .. districtIndex)
print('ACT|set_city_production|OK|{item}')
print('PRODUCTIVE_REQUEST_END|1')
print('---END---')
"""


def readback_lua(city_id: str, item: str, dest: str | None) -> str:
    family = validate(item, dest)
    x = y = -1
    if dest:
        q, y = map(int, dest.split(","))
        x = q + y // 2
    return f"""-- arena:productive_readback={family}|{item}|{x}|{y}
{_city(city_id)}
{_HELPERS}
local hash = exactNumber(bq:GetCurrentProductionTypeHash(),-9007199254740991,9007199254740991)
local index, district, owner, belongs, clear = -1,-1,-1,false,false
if '{family}' == 'district' then
    local plot = Map.GetPlot({x},{y})
    if plot == nil then error('productive readback plot unavailable') end
    if PlayersVisibility == nil or PlayersVisibility[me] == nil or not
        exactBool(PlayersVisibility[me]:IsVisible({x},{y})) then
        error('productive readback visibility unavailable') end
    index = exactNumber(plot:GetIndex(),0,10000000)
    district = exactNumber(plot:GetDistrictType(),-1,1000000)
    owner = exactNumber(plot:GetOwner(),-1,1000)
    clear = exactNumber(plot:GetFeatureType(),-1,1000000) == -1
        and exactNumber(plot:GetImprovementType(),-1,1000000) == -1
        and exactNumber(plot:GetResourceType(),-1,1000000) == -1
    for _, d in pCity:GetDistricts():Members() do
        if d:GetX() == {x} and d:GetY() == {y} and d:GetType() == district then belongs = true end
    end
end
print('PRODUCTIVE_STATE|' .. hash .. '|' .. index .. '|' .. district .. '|'
    .. owner .. '|' .. tostring(belongs) .. '|' .. tostring(clear))
print('PRODUCTIVE_STATE_END|1')
print('---END---')
"""


def parse_receipt(lines: list[str], item: str, dest: str | None) -> dict:
    from civ_arena.game.civ6.response_parser import _split_lines

    family = validate(item, dest)
    flattened = _split_lines(lines)
    if flattened and flattened[-1] == "---END---":
        flattened.pop()
    if len(flattened) != 3 or flattened[1:] != [
        "ACT|set_city_production|OK|" + item,
        "PRODUCTIVE_REQUEST_END|1",
    ]:
        raise RuntimeError("productive request receipt framing unavailable")
    rows = [line.split("|") for line in flattened if line.startswith("PRODUCTIVE_REQUEST|")]
    if len(rows) != 1 or len(rows[0]) != 6 or rows[0][1:3] != [family, item]:
        raise RuntimeError("productive request receipt identity unavailable")
    try:
        numbers = [int(value) for value in rows[0][3:]]
        if any(str(n) != value for n, value in zip(numbers, rows[0][3:], strict=True)):
            raise ValueError("noncanonical integer")
        h, plot, district = numbers
        if not 0 < abs(h) < 2**53 or (family == "project" and (plot, district) != (-1, -1)):
            raise ValueError("invalid hash/project receipt")
        if family == "district" and not (0 <= plot <= 10000000 and 0 <= district <= 1000000):
            raise ValueError("invalid placement receipt")
    except ValueError as exc:
        raise RuntimeError("productive request receipt values unavailable") from exc
    return {
        "kind": family,
        "item_id": item,
        "production_hash": h,
        "plot_index": plot,
        "district_index": district,
    }


def parse_state(lines: list[str]) -> dict:
    from civ_arena.game.civ6.response_parser import _split_lines

    rows = _split_lines(lines)
    if rows and rows[-1] == "---END---":
        rows.pop()
    if len(rows) != 2 or rows[-1] != "PRODUCTIVE_STATE_END|1":
        raise RuntimeError("productive readback framing unavailable")
    parts = rows[0].split("|")
    if (
        len(parts) != 7
        or parts[0] != "PRODUCTIVE_STATE"
        or any(value not in {"true", "false"} for value in parts[5:])
    ):
        raise RuntimeError("productive readback fields unavailable")
    try:
        values = [int(value) for value in parts[1:5]]
        if any(str(n) != value for n, value in zip(values, parts[1:5], strict=True)):
            raise ValueError("noncanonical integer")
        h, plot, district, owner = values
        if not (
            -(2**53) < h < 2**53
            and -1 <= plot <= 10000000
            and -1 <= district <= 1000000
            and -1 <= owner <= 1000
        ):
            raise ValueError("out of bounds")
    except ValueError as exc:
        raise RuntimeError("productive readback values unavailable") from exc
    return {
        "production_hash": h,
        "plot_index": plot,
        "district_index": district,
        "owner_id": owner,
        "belongs_to_city": parts[5] == "true",
        "consequence_free": parts[6] == "true",
    }


def generation(connection):
    from civ_arena.game.civ6.vendor.connection import GameConnection

    if isinstance(connection, GameConnection):
        return (connection._reader, connection._writer, connection.ingame_index)
    return (id(connection),)


async def execute_once(connection, lua: str) -> list[str]:
    """No reconnect or fallback after dispatch; native client must already be ready."""
    from civ_arena.game.civ6.vendor.connection import GameConnection

    if isinstance(connection, GameConnection):
        before = generation(connection)
        async with connection._lock:
            if (
                not connection.is_connected
                or connection._reader is None
                or connection.ingame_index is None
                or generation(connection) != before
            ):
                raise RuntimeError("productive one-shot connection unavailable or changed")
            return await connection._locked_execute(connection.ingame_index, lua, 5.0)
    method = getattr(connection, "execute_write_once", None)
    if method is None:
        raise RuntimeError("productive transport requires explicit one-shot execution")
    return await method(lua)


async def verify(
    connection,
    city_id: str,
    item: str,
    dest: str | None,
    submitted: list[str],
    *,
    timeout: float,
    interval: float,
    connection_generation=None,
) -> dict:
    import asyncio

    receipt = parse_receipt(submitted, item, dest)
    before = generation(connection) if connection_generation is None else connection_generation
    try:
        async with asyncio.timeout(timeout):
            while True:
                if generation(connection) != before:
                    raise RuntimeError(
                        "productive connection changed after request; outcome ambiguous"
                    )
                lines = await execute_once(connection, readback_lua(city_id, item, dest))
                if generation(connection) != before:
                    raise RuntimeError(
                        "productive connection changed during readback; outcome ambiguous"
                    )
                state = parse_state(lines)
                matched = state["production_hash"] == receipt["production_hash"]
                if receipt["kind"] == "district":
                    matched = matched and all(
                        (
                            state["plot_index"] == receipt["plot_index"],
                            state["district_index"] == receipt["district_index"],
                            state["owner_id"] == decode(city_id, "c")[0],
                            state["belongs_to_city"],
                            state["consequence_free"],
                        )
                    )
                if matched:
                    return {
                        **receipt,
                        "city_id": city_id,
                        "dest": dest,
                        "verification": "subsequent_exact_queue_and_placement_read",
                        "observed": state,
                        "completion_proven": False,
                        "coverage": "separate_observation_not_mod_digest_or_mutation_ledger",
                    }
                await asyncio.sleep(interval)
    except TimeoutError as exc:
        raise RuntimeError(
            "productive request unconfirmed; may still apply; no automatic replay"
        ) from exc
