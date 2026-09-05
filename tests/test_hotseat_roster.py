"""The startup roster must survive hosting and be proven by an engine census."""

import importlib.util
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launch = load_script('live_hotseat_launch')
zero = load_script('live_zero_touch')


LUA_FIXTURE = """
SlotStatus = {SS_CLOSED=0, SS_OPEN=1, SS_COMPUTER=2, SS_TAKEN=3}
CivilizationLevelTypes = {CIVILIZATION_LEVEL_FULL_CIV=0}
GameModeTypes = {HOTSEAT=1}
ServerType = {SERVER_TYPE_HOTSEAT=1}
PlayerConfigurations = {}
for _, pid in ipairs({0, 1, 2, 3, 10, 63}) do
  local pc = {slot=1, level=(pid < 4 and 0 or 1), leader='RANDOM', ready=false}
  function pc:GetSlotStatus() return self.slot end
  function pc:SetSlotStatus(v) self.slot=v end
  function pc:GetCivilizationLevelTypeID() return self.level end
  function pc:SetMajorCiv() self.level=0 end
  function pc:IsHuman() return self.slot==3 end
  function pc:SetLeaderTypeName(v) self.leader=v end
  function pc:GetLeaderTypeName() return self.leader end
  function pc:SetHotseatName(v) self.name=v end
  function pc:SetHotseatPassword(v) self.password=v end
  function pc:GetHotseatPassword() return self.password end
  function pc:SetReady(v) self.ready=v end
  function pc:GetReady() return self.ready end
  PlayerConfigurations[pid]=pc
end
MapConfiguration = {
  SetMapSize=function(v) end, GetMapSize=function() return 123 end,
  SetMinMajorPlayers=function(v) end, SetMaxMajorPlayers=function(v) end,
}
local active = false
GameConfiguration = {
  SetGameMode=function(v) end, GetGameMode=function() return 1 end,
  IsHotseat=function() return true end,
  SetParticipatingPlayerCount=function(v) end,
  SetGameSpeedType=function(v) end,
  GetHumanPlayerCount=function() return 2 end,
  GetAIPlayerCount=function() return 0 end,
  GetParticipatingPlayerCount=function() return 2 end,
  GetParticipatingPlayerIDs=function()
    local ids={}
    for pid=0,63 do
      local pc=PlayerConfigurations[pid]
      if pc and pc:GetSlotStatus()~=0 then table.insert(ids,pid) end
    end
    return ids
  end,
}
Network = {
  SetLocalNetworkMode=function(v) end,
  BroadcastPlayerInfo=function(pid) end,
  IsSessionActive=function() return active end,
  HostGame=function(server)
    assert(PlayerConfigurations[2].slot==0 and PlayerConfigurations[3].slot==0,
      'extra open slots must close before host')
    -- Reproduce an immediate hosting reset; the same-command post-host pass
    -- must close these again instead of trusting pre-host participating=2.
    PlayerConfigurations[2].slot=2; PlayerConfigurations[3].slot=1
    active=true
  end,
}
"""


@pytest.mark.parametrize('variant', ['success', 'refused_closure', 'human_demoted'])
def test_real_lua_closes_extra_slots_before_and_after_host(variant, tmp_path):
    executable = shutil.which('texlua')
    if executable is None:
        pytest.skip('texlua unavailable for executable roster fixture')
    source = LUA_FIXTURE
    if variant == 'refused_closure':
        source += 'PlayerConfigurations[2].SetSlotStatus=function() end\n'
    elif variant == 'human_demoted':
        source += """
local original=Network.HostGame
Network.HostGame=function(server) original(server); PlayerConfigurations[1].slot=1 end
"""
    source += launch.CONFIG_HOTSEAT_EMPTY_LUA + launch.HOST_HOTSEAT_LUA
    source += """
assert(PlayerConfigurations[0].slot==3 and PlayerConfigurations[1].slot==3)
assert(PlayerConfigurations[2].slot==0 and PlayerConfigurations[3].slot==0)
assert(PlayerConfigurations[10].slot==1 and PlayerConfigurations[63].slot==1,
  'minor/barbarian slots must remain unchanged')
assert(PlayerConfigurations[0].password=='' and PlayerConfigurations[1].password=='')
"""
    path = tmp_path / 'roster.lua'
    path.write_text(source)
    result = subprocess.run([executable, str(path)], capture_output=True, text=True, timeout=5)
    if variant == 'success':
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.count('MAJOR_ROSTER|0,1|humans=true|extra_slots=closed') == 2
        assert 'POSTHOST_ROSTER|two_humans_no_extra_major_slots' in result.stdout
    else:
        assert result.returncode != 0
        assert 'POSTHOST_ROSTER|' not in result.stdout


@pytest.mark.parametrize('receipt', [True, False])
async def test_ui_start_requires_posthost_receipt_without_dead_tuner_poll(receipt, monkeypatch):
    conn = SimpleNamespace(lua_states={1: 'StagingRoom'}, disconnect=AsyncMock())
    monkeypatch.setattr(launch, 'connect_state', AsyncMock(return_value=conn))
    configured = ['MAJOR_ROSTER|0,1|humans=true|extra_slots=closed']
    hosted = ['InSession|true']
    if receipt:
        hosted.append('POSTHOST_ROSTER|two_humans_no_extra_major_slots')
    run = AsyncMock(side_effect=[configured, hosted])
    monkeypatch.setattr(launch, 'run_lua', run)
    rediscover = AsyncMock(side_effect=AssertionError('must not poll the dead tuner'))
    monkeypatch.setattr(launch, '_rediscover', rediscover)
    result = await launch.run_full('127.0.0.1', 4318, 'StagingRoom', ui_start=True)
    assert result == (0 if receipt else 12)
    assert run.await_count == 2
    rediscover.assert_not_awaited()
    conn.disconnect.assert_awaited_once()


async def test_failed_prehost_roster_never_calls_host(monkeypatch):
    conn = SimpleNamespace(lua_states={1: 'StagingRoom'}, disconnect=AsyncMock())
    monkeypatch.setattr(launch, 'connect_state', AsyncMock(return_value=conn))
    run = AsyncMock(return_value=['Participating|2', 'Humans|2', 'AI|0'])
    monkeypatch.setattr(launch, 'run_lua', run)
    assert await launch.run_full('127.0.0.1', 4318, 'StagingRoom', ui_start=True) == 12
    run.assert_awaited_once()
    conn.disconnect.assert_awaited_once()


def row(pid, *, human='true', cfg='true', major='true', alive='true', slot='3'):
    return (f'P{pid}|human={human}|cfghuman={cfg}|major={major}|alive={alive}'
            f'|leader=fixture|slot={slot}|pause=?')


@pytest.mark.parametrize('cfg', ['true', '?'])
def test_full_census_accepts_two_humans_and_minor_barbarian_rows(cfg):
    rows = [row(0, cfg=cfg), row(1, cfg=cfg),
            row(10, human='false', major='false'), row(63, human='false', major='false')]
    census = zero.verify_two_major_census('\n'.join([*rows, 'CENSUS_END|4']))
    assert census['alive_major_ids'] == [0, 1]
    assert len(census['players']) == 4


@pytest.mark.parametrize('rows, trailer', [
    ([row(0), row(1), row(2, human='false')], 'CENSUS_END|3'),
    ([row(0)], 'CENSUS_END|1'),
    ([row(0), row(1), row(1)], 'CENSUS_END|3'),
    ([row(0), row(1, human='false')], 'CENSUS_END|2'),
    ([row(0), row(1, slot='1')], 'CENSUS_END|2'),
    ([row(0), row(1, cfg='false')], 'CENSUS_END|2'),
    ([row(0), row(1)], 'CENSUS_END|3'),
    ([row(0), row(1)], ''),
    ([row(0), row(1, alive='?')], 'CENSUS_END|2'),
])
def test_bad_or_partial_census_cannot_dispatch(rows, trailer):
    with pytest.raises(RuntimeError):
        zero.verify_two_major_census('\n'.join([*rows, trailer]))


def test_live_census_script_emits_count_and_wire_completion():
    census = load_script('live_seat_check')
    assert 'print("CENSUS_END|" .. tostring(#out))' in census.LUA
    assert census.LUA.rstrip().endswith('print("---END---")')
