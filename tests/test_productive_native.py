"""Typed productive actions: local Lua/fake transport proof, no Civ6 process."""

import asyncio
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from civ_arena.game.adapter import ActionCommand, ObserveKind, ObserveRequest
from civ_arena.game.civ6 import firetuner
from civ_arena.game.civ6 import lua_translator as lt
from civ_arena.game.civ6 import productive_native as pn
from civ_arena.game.civ6.fake_tuner_server import FakeMod
from civ_arena.game.civ6.response_parser import parse_available_production, parse_cities
from civ_arena.game.civ6.vendor.connection import GameConnection
from civ_arena.session import legality, tools

FIXTURE = """
local function catalog(rows)
 local lookup={};for _,r in ipairs(rows) do
  lookup[r.Hash]=r;lookup[r.DistrictType or r.ProjectType or r.UnitType or r.BuildingType]=r end
 return setmetatable(lookup,{__call=function() local i=0;return function()
  i=i+1;return rows[i] end end})
end
queueHash=0; plotDistrict=-1; owner=0; visible=true; feature=-1; resource=-1; improvement=-1
eligible=true; targetsValid=true; apply=true; calls=0
local project={ProjectType='PROJECT_ENHANCE_DISTRICT_CAMPUS',Hash=108,Index=8,Cost=25}
local district={DistrictType='DISTRICT_CAMPUS',Hash=209,Index=7,Cost=54}
GameInfo={Units=catalog({}),Buildings=catalog({}),Projects=catalog({project}),Districts=catalog({district})}
Game={GetLocalPlayer=function() return 0 end}
CityOperationTypes={BUILD=1,PARAM_PROJECT_TYPE='project',PARAM_DISTRICT_TYPE='district',
 PARAM_X='x',PARAM_Y='y',PARAM_INSERT_MODE='mode',VALUE_EXCLUSIVE=1}
CityOperationResults={PLOTS='plots'}
local plot={GetIndex=function() return 42 end,
 GetX=function() return 2 end,GetY=function() return 3 end,
 GetOwner=function() return owner end,IsCity=function() return false end,
 GetFeatureType=function() return feature end,GetResourceType=function() return resource end,
 GetImprovementType=function() return improvement end,
 GetDistrictType=function() return plotDistrict end}
Map={GetPlotByIndex=function(i) if i==42 then return plot end end,
 GetPlot=function(x,y) if x==2 and y==3 then return plot end end}
PlayersVisibility={[0]={IsVisible=function() return visible end}}
bq={GetCurrentProductionTypeHash=function() return queueHash end,
 CanProduce=function() return eligible end,HasBeenPlaced=function() return false end,
 GetProjectCost=function() return 25 end,GetProjectProgress=function() return 0 end,
 GetDistrictCost=function() return 54 end,GetTurnsLeft=function() return 4 end}
pCity={GetID=function() return 65536 end,GetOwner=function() return 0 end,
 GetX=function() return 0 end,GetY=function() return 0 end,GetName=function() return 'Capital' end,
 GetPopulation=function() return 4 end,GetBuildQueue=function() return bq end,
 GetDistricts=function() return {Members=function()
 local rows={};if plotDistrict==7 then rows={{GetX=function() return 2 end,
 GetY=function() return 3 end,GetType=function() return 7 end}} end
 return ipairs(rows) end} end}
CityManager={GetCity=function(me,cid) if me==0 and cid==65536 then return pCity end end,
 GetOperationTargets=function() return {plots=targetsValid and {42} or {}} end,
 CanStartOperation=function() return eligible end,
 RequestOperation=function(city,op,params) calls=calls+1
  if apply then queueHash=params.project or params.district
   if params.district then plotDistrict=7 end end end}
local player={GetID=function() return 0 end,GetCities=function() return {Members=function()
 return ipairs({pCity}) end} end}
PlayerManager={GetAliveMajors=function() return {player} end}
Locale={Lookup=function(s) return s end}
"""


def lua(tmp_path, code, changes=""):
    executable = shutil.which("texlua")
    if not executable:
        pytest.skip("texlua unavailable")
    path = tmp_path / "productive.lua"
    path.write_text(FIXTURE + changes + "\n" + code)
    return subprocess.run([executable, str(path)], capture_output=True, text=True, timeout=5)


def request(item="DISTRICT_CAMPUS", dest="1,3"):
    return pn.request_lua("c0:65536", item, dest)


def test_native_enabled_options_typed_and_coordinate_correct(tmp_path):
    run = lua(tmp_path, lt.available_production_read("c0:65536"))
    assert run.returncode == 0, run.stdout + run.stderr
    options = parse_available_production(run.stdout.splitlines(), require_productive=True)
    assert options == [
        {
            "kind": "district",
            "item_id": "DISTRICT_CAMPUS",
            "cost": 54,
            "turns": 4,
            "placements": ["1,3"],
        },
        {"kind": "project", "item_id": "PROJECT_ENHANCE_DISTRICT_CAMPUS", "cost": 25, "turns": 4},
    ]


@pytest.mark.parametrize(
    "change",
    [
        "feature=0",
        "resource=1",
        "improvement=2",
        "plotDistrict=3",
        "visible=false",
        "owner=1",
        "targetsValid=false",
    ],
)
def test_options_exclude_unowned_hidden_consequences_or_illegal_plot(tmp_path, change):
    run = lua(tmp_path, lt.available_production_read("c0:65536"), change)
    assert run.returncode == 0, run.stdout
    assert [r["kind"] for r in parse_available_production(run.stdout.splitlines())] == ["project"]


@pytest.mark.parametrize(
    "change",
    [
        "GameInfo.Projects=nil",
        "GameInfo.Districts=nil",
        "bq.GetDistrictCost=nil",
        "CityManager.GetOperationTargets=nil",
        "PlayersVisibility=nil",
        "eligible=nil",
        "feature=nil",
    ],
)
def test_unavailable_native_data_fails_not_empty(tmp_path, change):
    run = lua(tmp_path, lt.available_production_read("c0:65536"), change)
    assert run.returncode != 0
    with pytest.raises(ValueError):
        parse_available_production(run.stdout.splitlines(), require_productive=True)


@pytest.mark.parametrize(
    "change,reason",
    [
        ("queueHash=11", "ALREADY"),
        ("eligible=false", "PREREQ_UNMET"),
        ("targetsValid=false", "ILLEGAL_DEST"),
        ("feature=1", "ILLEGAL_DEST"),
        ("visible=false", "ILLEGAL_DEST"),
        ("owner=1", "ILLEGAL_DEST"),
        ("bq.HasBeenPlaced=function() return true end", "ALREADY"),
        ("GameInfo.Units.UNIT_DISTRICT_CAMPUS={Hash=99}", "ARGS_INVALID"),
    ],
)
def test_placement_guard_refuses_without_any_request(tmp_path, change, reason):
    run = lua(tmp_path, "(function()\n" + request() + "\nend)()\nprint('CALLS|'..calls)", change)
    assert run.returncode == 0, run.stdout
    assert f"ERR|{reason}|" in run.stdout and "CALLS|0" in run.stdout


@pytest.mark.parametrize(
    "item,dest,expected",
    [("DISTRICT_CAMPUS", "1,3", 209), ("PROJECT_ENHANCE_DISTRICT_CAMPUS", None, 108)],
)
def test_exact_request_receipt_and_subsequent_native_readback(tmp_path, item, dest, expected):
    run = lua(tmp_path, request(item, dest) + pn.readback_lua("c0:65536", item, dest))
    assert run.returncode == 0, run.stdout
    lines = run.stdout.splitlines()
    receipt = pn.parse_receipt(lines[:4], item, dest)
    observed = pn.parse_state(lines[4:])
    assert receipt["production_hash"] == observed["production_hash"] == expected
    if dest:
        assert observed["belongs_to_city"] and observed["district_index"] == 7
        assert observed["plot_index"] == 42 and observed["owner_id"] == 0
        assert observed["consequence_free"]


@pytest.mark.parametrize(
    "hash_,expected", [(108, "PROJECT_ENHANCE_DISTRICT_CAMPUS"), (209, "DISTRICT_CAMPUS")]
)
def test_city_queue_preserves_typed_identity(tmp_path, hash_, expected):
    run = lua(tmp_path, lt.cities_read(), f"queueHash={hash_}")
    assert run.returncode == 0, run.stdout
    assert parse_cities(run.stdout.splitlines(), qualified=True)[0]["production_queue"] == [
        expected
    ]


@pytest.mark.parametrize(
    "row",
    [
        "ITEMROW|district|CAMPUS|54|4|1,3",
        "ITEMROW|district|DISTRICT_CAMPUS|54|4|1,3;1,3",
        "ITEMROW|district|DISTRICT_CAMPUS|54|4|01,3",
        "ITEMROW|district|DISTRICT_CAMPUS|54|4|",
        "ITEMROW|project|PROJECT_X|?|4",
        "ITEMROW|project|PROJECT_X|4|4|1,3",
        "ITEMROW|building|GRANARY|4|4|1,3",
    ],
)
def test_closed_option_parser_refuses_invalid_rows(row):
    with pytest.raises(ValueError):
        parse_available_production(["AVPROD|1", row, "PRODUCTIVE_OPTIONS_END|1", "---END---"])


def test_duplicate_cross_kind_and_missing_completion_refused():
    rows = [
        "AVPROD|1",
        "ITEMROW|unit|PROJECT_X|1|1",
        "ITEMROW|project|PROJECT_X|1|1",
        "PRODUCTIVE_OPTIONS_END|1",
        "---END---",
    ]
    with pytest.raises(ValueError, match="reserved productive namespace"):
        parse_available_production(rows)
    with pytest.raises(ValueError, match="completeness"):
        parse_available_production(["AVPROD|1", "---END---"], require_productive=True)


RECEIPT = [
    "PRODUCTIVE_REQUEST|district|DISTRICT_CAMPUS|209|42|7",
    "ACT|set_city_production|OK|DISTRICT_CAMPUS",
    "PRODUCTIVE_REQUEST_END|1",
    "---END---",
]
STATE = ["PRODUCTIVE_STATE|209|42|7|0|true|true", "PRODUCTIVE_STATE_END|1", "---END---"]


def adapter(monkeypatch, readbacks):
    obj = firetuner.FireTunerAdapter()
    obj._phase_open = 0
    obj._refresh_digest = AsyncMock()
    obj._drain_command_diff = AsyncMock(return_value=[])
    obj._conn = SimpleNamespace(
        execute_write_once=AsyncMock(side_effect=[RECEIPT, *readbacks]),
        execute_write=AsyncMock(side_effect=AssertionError("retrying path")),
        execute_read=AsyncMock(),
    )
    monkeypatch.setattr(firetuner, "_PRODUCTION_POLL_INTERVAL_S", 0)
    return obj


def command(item="DISTRICT_CAMPUS", dest="1,3", city="c0:65536"):
    args = {"city_id": city, "item_id": item}
    if dest is not None:
        args["dest"] = dest
    return ActionCommand(
        tool="set_city_production",
        args=args,
        player_id=0,
        idempotency_key="productive-fixture",
        lease_id="lease-fixture",
    )


async def test_adapter_submits_once_then_waits_for_exact_queue_and_plot(monkeypatch):
    obj = adapter(
        monkeypatch,
        [["PRODUCTIVE_STATE|209|42|-1|0|false|true", "PRODUCTIVE_STATE_END|1", "---END---"], STATE],
    )
    result = await obj.act(command())
    assert result.status == "accepted" and not result.mutations
    assert result.result["production_readback"]["completion_proven"] is False
    assert result.result["production_readback"]["observed"]["belongs_to_city"]
    chunks = [c.args[0] for c in obj._conn.execute_write_once.call_args_list]
    assert len(chunks) == 3 and sum("RequestOperation" in c for c in chunks) == 1
    obj._conn.execute_write.assert_not_awaited()
    obj._conn.execute_read.assert_not_awaited()  # no unit restoration
    obj._refresh_digest.assert_awaited_once()


@pytest.mark.parametrize(
    "state",
    [
        [],
        ["PRODUCTIVE_STATE|209|42|7|1|true|true", "PRODUCTIVE_STATE_END|1", "---END---"],
        ["PRODUCTIVE_STATE|209|42|7|0|true|false", "PRODUCTIVE_STATE_END|1", "---END---"],
    ],
)
async def test_wrong_or_unavailable_readback_never_accepts(monkeypatch, state):
    obj = adapter(monkeypatch, [state])
    obj._conn.execute_write_once.side_effect = [
        RECEIPT,
        state,
        RuntimeError("no confirmed readback"),
    ]
    with pytest.raises(RuntimeError):
        await obj.act(command())
    obj._refresh_digest.assert_not_awaited()
    assert (
        sum("RequestOperation" in c.args[0] for c in obj._conn.execute_write_once.call_args_list)
        == 1
    )


async def test_timeout_cancels_readback_and_never_replays(monkeypatch):
    obj = adapter(monkeypatch, [])
    monkeypatch.setattr(firetuner, "_PRODUCTION_VERIFY_TIMEOUT_S", 0.02)
    cancelled = asyncio.Event()
    chunks = []

    async def write(code):
        chunks.append(code)
        if "RequestOperation" in code:
            return RECEIPT
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    obj._conn.execute_write_once = write
    with pytest.raises(RuntimeError, match="no automatic replay"):
        await obj.act(command())
    assert cancelled.is_set() and len(chunks) == 2


async def test_dead_socket_one_shot_never_reconnects_or_resubmits():
    connection = GameConnection()
    connection._reader = object()
    connection._writer = SimpleNamespace(is_closing=lambda: False)
    connection.ingame_index = 1
    connection._locked_execute = AsyncMock(side_effect=ConnectionError("lost after send"))
    connection.reconnect = AsyncMock()
    with pytest.raises(ConnectionError):
        await pn.execute_once(connection, "request")
    connection._locked_execute.assert_awaited_once()
    connection.reconnect.assert_not_awaited()


async def test_missing_one_shot_transport_refuses_before_generic_write():
    conn = SimpleNamespace(execute_write=AsyncMock())
    with pytest.raises(RuntimeError, match="one-shot"):
        await pn.execute_once(conn, "request")
    conn.execute_write.assert_not_awaited()


async def test_foreign_action_and_observation_before_transport(monkeypatch):
    obj = adapter(monkeypatch, [])
    assert (await obj.act(command(city="c1:65536"))).rejection == "not_your_city"
    with pytest.raises(ValueError, match="owner mismatch"):
        await obj.observe(ObserveRequest(ObserveKind.AVAILABLE_PRODUCTION, 0, "c1:65536"))
    obj._conn.execute_write_once.assert_not_awaited()
    obj._conn.execute_write.assert_not_awaited()


@pytest.mark.parametrize(
    "item,dest",
    [("MONUMENT", "1,3"), ("PROJECT_X", "1,3"), ("DISTRICT_X", None), ("DISTRICT_X", "1,03")],
)
def test_shape_legality_refuses_before_adapter(item, dest):
    assert (
        legality.validate_args(
            "set_city_production", {"city_id": "c0:1", "item_id": item, "dest": dest}
        )
        == "args_invalid"
    )


async def test_facade_legacy_args_unchanged_and_new_dest_forwarded():
    ctx = SimpleNamespace(referee=SimpleNamespace(execute=AsyncMock(return_value={})))
    await tools.set_city_production(ctx, "c0:1", "MONUMENT")
    assert ctx.referee.execute.await_args.args[2] == {"city_id": "c0:1", "item_id": "MONUMENT"}
    await tools.set_city_production(ctx, "c0:1", "DISTRICT_CAMPUS", "1,3", idempotency_key="k")
    assert ctx.referee.execute.await_args.args[2]["dest"] == "1,3"
    assert ctx.referee.execute.await_args.kwargs["client_key"] == "k"


def test_fake_mod_typed_production_then_readback():
    fake = FakeMod()
    cid = next(iter(fake.cities))
    city = fake.cities[cid]
    fake.local_player = city["owner"]
    city["queue"] = ""
    fake.productive_options = {
        "DISTRICT_CAMPUS": {
            "kind": "district",
            "placements": ["1,3"],
            "plot_index": 42,
            "district_index": 7,
        }
    }
    city_id = f"c{city['owner']}:{cid}"
    rows = fake.respond(pn.request_lua(city_id, "DISTRICT_CAMPUS", "1,3"))
    assert pn.parse_receipt(rows, "DISTRICT_CAMPUS", "1,3")["plot_index"] == 42
    observed = pn.parse_state(fake.respond(pn.readback_lua(city_id, "DISTRICT_CAMPUS", "1,3")))
    assert observed["belongs_to_city"] and observed["district_index"] == 7
    assert city["queue"] == "DISTRICT_CAMPUS"
    again = fake.respond(pn.request_lua(city_id, "DISTRICT_CAMPUS", "1,3"))
    assert any("ERR|ALREADY" in row for row in again)


async def test_actual_vendor_transport_strips_sentinel_preserves_own_frames(monkeypatch):
    from civ_arena.game.civ6.fake_tuner_server import FakeTunerServer

    fake = FakeMod()
    cid = next(iter(fake.cities))
    city = fake.cities[cid]
    fake.local_player = city["owner"]
    city["queue"] = ""
    fake.productive_options = {
        "DISTRICT_CAMPUS": {
            "kind": "district",
            "placements": ["1,3"],
            "plot_index": 42,
            "district_index": 7,
        }
    }
    server = FakeTunerServer([], mod=fake)
    port = await server.start()
    connection = GameConnection("127.0.0.1", port)
    try:
        await connection.connect()
        options = await connection.execute_write(lt.available_production_read(f"c0:{cid}"))
        assert "---END---" not in options and options[-1] == "PRODUCTIVE_OPTIONS_END|1"
        assert any(
            r["kind"] == "district"
            for r in parse_available_production(options, require_productive=True)
        )
        obj = firetuner.FireTunerAdapter(conn=connection)
        obj._phase_open = 0
        obj._refresh_digest = AsyncMock()
        obj._drain_command_diff = AsyncMock(return_value=[])
        result = await obj.act(command(city=f"c0:{cid}"))
        assert result.status == "accepted"
        assert result.result["production_readback"]["observed"]["district_index"] == 7
        assert sum("CityManager.RequestOperation" in c for c in server.received_commands) == 1
    finally:
        await connection.disconnect()
        await server.stop()


@pytest.mark.parametrize(
    "change",
    [
        "CityManager.GetOperationTargets=function() return {} end",
        "CityManager.GetOperationTargets=function() return {plots={[2]=42}} end",
        "CityManager.GetOperationTargets=function() local p={42,43,44,45}; "
        "p[2]=nil; assert(#p==4); return {plots=p} end",
    ],
)
def test_missing_or_sparse_plot_schema_is_unavailable_not_empty(tmp_path, change):
    result = lua(tmp_path, lt.available_production_read("c0:65536"), change)
    assert result.returncode != 0


async def test_cancellation_during_one_shot_releases_lock_without_reconnect():
    conn = GameConnection()
    conn._reader = object()
    conn._writer = SimpleNamespace(is_closing=lambda: False)
    conn.ingame_index = 1
    started = asyncio.Event()

    async def block(*args):
        started.set()
        await asyncio.Event().wait()

    conn._locked_execute = AsyncMock(side_effect=block)
    conn.reconnect = AsyncMock()
    task = asyncio.create_task(pn.execute_once(conn, "request"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not conn._lock.locked()
    conn._locked_execute.assert_awaited_once()
    conn.reconnect.assert_not_awaited()


async def test_changed_connection_generation_cannot_confirm_old_request():
    conn = GameConnection()
    conn._reader = object()
    conn._writer = SimpleNamespace(is_closing=lambda: False)
    conn.ingame_index = 1
    token = pn.generation(conn)

    async def read(*args):
        conn._reader = object()
        return STATE

    conn._locked_execute = AsyncMock(side_effect=read)
    with pytest.raises(RuntimeError, match="connection changed"):
        await pn.verify(
            conn,
            "c0:65536",
            "DISTRICT_CAMPUS",
            "1,3",
            RECEIPT,
            timeout=1,
            interval=0,
            connection_generation=token,
        )
    conn._locked_execute.assert_awaited_once()


def test_target_change_after_options_is_rechecked_before_submission(tmp_path):
    code = lt.available_production_read("c0:65536") + "\nfeature=3\n" + request()
    result = lua(tmp_path, code)
    assert result.returncode == 0, result.stdout
    assert "ITEMROW|district|DISTRICT_CAMPUS" in result.stdout
    assert "ERR|ILLEGAL_DEST|" in result.stdout
    assert "PRODUCTIVE_REQUEST|" not in result.stdout


@pytest.mark.parametrize(
    "rows", [STATE[:-2], STATE + ["unexpected"], STATE[:-1] + ["PRODUCTIVE_STATE_END|1"]]
)
def test_readback_requires_own_complete_frame_without_trailing_output(rows):
    with pytest.raises(RuntimeError, match="framing"):
        pn.parse_state(rows)


@pytest.mark.parametrize("kind,item", [("unit", "PROJECT_X"), ("building", "DISTRICT_CAMPUS")])
def test_legacy_catalog_cannot_claim_reserved_typed_namespace(kind, item):
    with pytest.raises(ValueError, match="reserved productive namespace"):
        parse_available_production([f"ITEMROW|{kind}|{item}|1|1"])


def test_legacy_queue_suffix_cannot_masquerade_as_typed_district(tmp_path):
    change = (
        "GameInfo.Units[209]={};GameInfo.Units=setmetatable({}, {__call=function() "
        "local done=false;return function() if not done then done=true;return "
        "{Hash=209,UnitType='UNIT_DISTRICT_CAMPUS'} end end end});queueHash=209"
    )
    result = lua(tmp_path, lt.cities_read(), change)
    assert result.returncode != 0 and "reserved productive namespace" in result.stdout
