"""Production submission is asynchronous; only subsequent observation confirms it."""

import asyncio
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from civ_arena.game.adapter import ActionCommand, ObserveKind, ObserveRequest
from civ_arena.game.civ6 import firetuner, lua_translator, response_parser


def prepared_adapter(monkeypatch, replies):
    adapter = firetuner.FireTunerAdapter()
    adapter._phase_open = 0
    adapter._refresh_digest = AsyncMock()
    adapter._drain_command_diff = AsyncMock(return_value=[])
    adapter._conn = SimpleNamespace(execute_write=AsyncMock(side_effect=[
        ['PRODUCTION_REQUEST|-2025065568', 'ACT|set_city_production|OK|MONUMENT|3'],
        *replies]), execute_read=AsyncMock())
    monkeypatch.setattr(firetuner, '_PRODUCTION_POLL_INTERVAL_S', 0)
    return adapter


def command():
    return ActionCommand(tool='set_city_production', player_id=0,
                         args={'city_id': 'c0:65536', 'item_id': 'MONUMENT'},
                         idempotency_key='production', lease_id='fixture')


async def test_delayed_production_is_submitted_once_and_accepted_after_exact_readback(monkeypatch):
    adapter = prepared_adapter(monkeypatch, [['CURPROD|0'], ['CURPROD|1872107673'],
                                            ['CURPROD|-2025065568']])
    result = await adapter.act(command())
    assert result.status == 'accepted'
    assert result.result['production_hash'] == -2025065568
    assert result.result['verification'] == 'subsequent_ingame_read'
    chunks = [args.args[0] for args in adapter._conn.execute_write.call_args_list]
    assert len(chunks) == 4
    assert sum('CityManager.RequestOperation' in chunk for chunk in chunks) == 1
    assert all('CURPROD|' in chunk for chunk in chunks[1:])
    adapter._refresh_digest.assert_awaited_once()
    adapter._drain_command_diff.assert_awaited_once()


@pytest.mark.parametrize('readback', [['CURPROD|-1'], [], ['CURPROD|bad'],
                                    ['CURPROD|0', 'CURPROD|0']])
async def test_unavailable_production_after_submission_is_an_honest_abort(monkeypatch, readback):
    adapter = prepared_adapter(monkeypatch, [readback])
    with pytest.raises(RuntimeError, match='unavailable'):
        await adapter.act(command())
    adapter._refresh_digest.assert_not_awaited()


async def test_whole_verification_deadline_includes_stalled_connection(monkeypatch):
    adapter = prepared_adapter(monkeypatch, [])
    monkeypatch.setattr(firetuner, '_PRODUCTION_VERIFY_TIMEOUT_S', 0.02)
    writes = []
    cancelled = asyncio.Event()

    async def write(lua):
        writes.append(lua)
        if 'RequestOperation' in lua:
            return ['PRODUCTION_REQUEST|-2025065568', 'ACT|set_city_production|OK|MONUMENT']
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    adapter._conn.execute_write = write
    with pytest.raises(RuntimeError, match='submitted request may still apply'):
        await adapter.act(command())
    assert cancelled.is_set()
    assert len(writes) == 2
    adapter._refresh_digest.assert_not_awaited()


async def test_missing_submission_receipt_never_claims_acceptance(monkeypatch):
    adapter = prepared_adapter(monkeypatch, [])
    adapter._conn.execute_write.side_effect = [['ACT|set_city_production|OK|MONUMENT']]
    with pytest.raises(RuntimeError, match='target-hash receipt'):
        await adapter.act(command())
    assert adapter._conn.execute_write.await_count == 1


async def test_city_observations_read_actual_queues_in_ingame():
    adapter = firetuner.FireTunerAdapter()
    adapter._conn = SimpleNamespace(execute_write=AsyncMock(return_value=[
        'CITIES|1', 'CITYROW|c0:65536|0|City|1|2|1|MONUMENT']),
        execute_read=AsyncMock(side_effect=AssertionError('GameCore lacks queue getter')))
    cities = await adapter.observe(ObserveRequest(kind=ObserveKind.CITIES, player_id=0))
    assert cities[0]['production_queue'] == ['MONUMENT']
    adapter._conn.execute_write.assert_awaited_once()
    adapter._conn.execute_read.assert_not_awaited()


@pytest.mark.parametrize('current, expected', [
    (0, []), (1872107673, ['SCOUT']), (-2025065568, ['MONUMENT']),
    (789, ['UNKNOWN_PRODUCTION_789']),
])
def test_real_lua_city_queue_resolves_actual_signed_hash(tmp_path, current, expected):
    executable = shutil.which('texlua')
    if executable is None:
        pytest.skip('texlua unavailable for executable queue fixture')
    fixture = f"""
local city={{GetID=function() return 65536 end,GetX=function() return 1 end,
 GetY=function() return 2 end,GetName=function() return 'City' end,
 GetBuildQueue=function() return {{GetCurrentProductionTypeHash=function()
 return {current} end}} end}}
local player={{GetID=function() return 0 end,GetCities=function()
 return {{Members=function() return ipairs({{city}}) end}} end}}
PlayerManager={{GetAliveMajors=function() return {{player}} end}}
Locale={{Lookup=function(value) return value end}}
local function rows(row) local emitted=false; return function()
 if not emitted then emitted=true;return row end end end
GameInfo={{Units=function() return rows({{Hash=1872107673,UnitType='UNIT_SCOUT'}}) end,
 Buildings=function() return rows({{Hash=-2025065568,BuildingType='BUILDING_MONUMENT'}}) end}}
"""
    path = tmp_path / 'queue.lua'
    path.write_text(fixture + lua_translator.cities_read())
    result = subprocess.run([executable, str(path)], capture_output=True,
                            text=True, timeout=5, check=True)
    cities = response_parser.parse_cities(result.stdout.splitlines(), qualified=True)
    assert cities[0]['production_queue'] == expected
