"""Execute the real mod in Lua with a minimal engine fixture; no live game I/O."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from civ_arena.game.civ6 import lua_translator

MOD = Path(__file__).resolve().parents[1] / 'mods/PuppeteerMod/PuppeteerMod.lua'

ENGINE = """
currentTurn, localPlayer, human, active = 1, 0, true, true
freezes, restores, attackRestores = 0, 0, 0
hooks = {}
local function event(name)
  return {Add=function(fn) hooks[name]=fn end,
          Remove=function(fn) if hooks[name]==fn then hooks[name]=nil end end}
end
GameEvents = {PlayerTurnStartComplete=event('start'), PlayerTurnActivated=event('activated')}
Events = {PlayerTurnDeactivated=event('deactivated')}
Game = {GetCurrentGameTurn=function() return currentTurn end,
        GetLocalPlayer=function() return localPlayer end}
function newUnit(id, attacks)
  return {id=id, moves=2, maximum=2, attacks=attacks,
    GetID=function(s) return s.id end, GetX=function() return 0 end,
    GetY=function() return 0 end, GetDamage=function() return 0 end,
    GetMovesRemaining=function(s) return s.moves end,
    GetMaxMoves=function(s) return s.maximum end,
    GetAttacksRemaining=function(s) return s.attacks end}
end
units = {newUnit(10, 1), newUnit(20, 0)}
local unitCollection = {
  Members=function() return ipairs(units) end,
  FindID=function(_, id) for _, u in ipairs(units) do if u.id==id then return u end end end}
local player = {
  IsHuman=function() return human end, IsTurnActive=function() return active end,
  GetUnits=function() return unitCollection end,
  GetCities=function() return {Members=function() return ipairs({}) end} end,
  GetTechs=function() return {GetResearchingTech=function() return -1 end} end,
  GetTreasury=function() return {GetGoldBalance=function() return 0 end} end}
Players = {[0]=player, [1]=player}
UnitManager = {
  FinishMoves=function(u)
    freezes=freezes+1
    if failUnit==u.id then error('freeze error') end
    u.moves=0
    if loseAttack then u.attacks=0 end
  end,
  RestoreMovement=function(u) restores=restores+1; u.moves=u.maximum end,
  RestoreUnitAttacks=function(u) attackRestores=attackRestores+1; u.attacks=1 end}
"""


@pytest.fixture
def run_mod(tmp_path):
    executable = shutil.which('texlua')
    if executable is None:
        pytest.skip('texlua unavailable for executable initial-attach fixture')

    def run(body):
        path = tmp_path / 'attach.lua'
        path.write_text(ENGINE + '\ndofile(' + json.dumps(str(MOD)) + ')\n' + body)
        result = subprocess.run([executable, str(path)], capture_output=True, text=True,
                                timeout=5, check=True)
        return result.stdout.splitlines()
    return run


@pytest.mark.parametrize('setup, player, turn, reason', [
    ('', 0, 2, 'not_initial_turn'),
    ('currentTurn=2', 0, 1, 'turn_mismatch'),
    ('localPlayer=1', 0, 1, 'not_local_player'),
    ('human=false', 0, 1, 'not_human'),
    ('active=false', 0, 1, 'not_active'),
    ('Puppeteer.SetPuppet(0,false)', 0, 1, 'not_armed'),
    ('Players[0]=nil', 0, 1, 'missing_player'),
    ('units[2].moves=1', 0, 1, 'movement_spent'),
    ('units[2].GetMaxMoves=nil', 0, 1, 'guard_unavailable'),
    ('units[2].GetAttacksRemaining=nil', 0, 1, 'guard_unavailable'),
    ('units[2].attacks=-1', 0, 1, 'invalid_attacks'),
    ('PUPPETEER_INITIAL_ATTACH_USED[0]=true', 0, 1, 'already_acquired'),
    ('units={}', 0, 1, 'no_live_units'),
])
def test_attach_guards_reject_before_any_freeze(run_mod, setup, player, turn, reason):
    rows = run_mod(f"""
Puppeteer.SetPuppet(0,true)
{setup}
Puppeteer.AttachCurrentTurn({player},{turn})
print('FREEZES|' .. freezes)
""")
    assert f'ATTACH_CURRENT|rejected|{player}|{turn}|{reason}' in rows
    assert rows[-1] == 'FREEZES|0'


def test_acquisition_and_duplicate_preserve_attack_and_movement_allowances(run_mod):
    rows = run_mod("""
Puppeteer.SetPuppet(0,true)
Puppeteer.AttachCurrentTurn(0,1)
assert(units[1].moves==0 and units[2].moves==0)
Puppeteer.RestoreUnit(20,0)
assert(units[2].moves==2 and units[2].attacks==0)
Puppeteer.AttachCurrentTurn(0,1)
hooks.start(0)
Puppeteer.RestoreUnit(20,0)
print('COUNTS|' .. freezes .. '|' .. restores .. '|' .. attackRestores)
print(Puppeteer.Status())
""")
    assert 'ATTACH_CURRENT|accepted|0|1|acquired' in rows
    assert 'ATTACH_CURRENT|duplicate|0|1|already_engaged' in rows
    assert 'COUNTS|2|1|0' in rows
    assert 'LEASE_PLAYER|0' in rows and 'LEASE_TURN|1' in rows


def test_existing_foreign_lease_is_never_replaced(run_mod):
    rows = run_mod("""
Puppeteer.SetPuppet(0,true)
Puppeteer.SetPuppet(1,true)
hooks.start(1)
freezes=0
Puppeteer.AttachCurrentTurn(0,1)
print('FREEZES|' .. freezes)
print(Puppeteer.Status())
""")
    assert 'ATTACH_CURRENT|rejected|0|1|existing_lease' in rows
    assert 'FREEZES|0' in rows and 'LEASE_PLAYER|1' in rows


def test_same_version_reinjection_preserves_lease_restore_budget_and_release_history(run_mod):
    rows = run_mod(f"""
Puppeteer.SetPuppet(0,true)
Puppeteer.AttachCurrentTurn(0,1)
Puppeteer.RestoreUnit(10,0)
local original=Puppeteer
dofile({json.dumps(str(MOD))})
assert(original==Puppeteer)
Puppeteer.RestoreUnit(10,0)
Puppeteer.AttachCurrentTurn(0,1)
Puppeteer.Release(0,1)
dofile({json.dumps(str(MOD))})
Puppeteer.AttachCurrentTurn(0,1)
hooks.start(0)
assert(string.find(Puppeteer.Status(), 'PUPPET_ACTIVE|false', 1, true))
print('COUNTS|' .. freezes .. '|' .. restores)
""")
    assert 'ATTACH_CURRENT|duplicate|0|1|already_engaged' in rows
    assert 'ATTACH_CURRENT|rejected|0|1|already_acquired' in rows
    assert rows[-1] == 'COUNTS|2|1'


@pytest.mark.parametrize('setup, detail', [
    ('failUnit=20', 'freeze_failed'), ('loseAttack=true', 'freeze_verification_failed'),
])
def test_failed_freeze_is_not_accepted_and_cannot_be_retried(run_mod, setup, detail):
    rows = run_mod(f"""
Puppeteer.SetPuppet(0,true)
{setup}
Puppeteer.AttachCurrentTurn(0,1)
local attempted=freezes
Puppeteer.AttachCurrentTurn(0,1)
hooks.start(0)
assert(freezes==attempted)
print(Puppeteer.Status())
""")
    assert f'ATTACH_CURRENT|failed|0|1|{detail}' in rows
    assert 'ATTACH_CURRENT|accepted|0|1|acquired' not in rows
    assert 'ATTACH_CURRENT|rejected|0|1|already_acquired' in rows
    assert 'PUPPET_ACTIVE|false' in rows


def test_native_lease_keeps_existing_restore_behavior(run_mod):
    rows = run_mod("""
Puppeteer.SetPuppet(0,true)
hooks.start(0)
Puppeteer.RestoreUnit(10,0)
print('ATTACK_RESTORES|' .. attackRestores)
""")
    assert rows[-1] == 'ATTACK_RESTORES|1'


def test_translator_emits_only_guarded_initial_call():
    assert lua_translator.attach_current_turn(0, 1) == 'Puppeteer.AttachCurrentTurn(0, 1)'
    for args in ((True, 1), (0, True), (0, 2), (-1, 1), ('0', 1)):
        with pytest.raises(ValueError):
            lua_translator.attach_current_turn(*args)
