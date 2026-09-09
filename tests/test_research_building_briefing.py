"""Effective research hints use local Lua and projected fixtures, never live Civ6."""
import copy
import json
import shutil
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from civ_arena.agents.llm.context_curator import CONTEXT_MARKER, ContextCurator
from civ_arena.arena.visibility import VisibilityPolicy
from civ_arena.config import ConfigError, parse_config
from civ_arena.game.adapter import ObserveKind, ObserveRequest
from civ_arena.game.civ6 import research_briefing as rb
from civ_arena.replay import RecordedCall, ReplayRuntime
from civ_arena.session import tools
from test_adaptive_context import harness
from test_config_llm import VALID, llm_doc
from test_strategic_controller import Facade, advance

FIXTURE = """
local function catalog(rows)
 return setmetatable({}, {__call=function() local i=0; return function()
  i=i+1; return rows[i] end end})
end
function building(id,tech,trait,district)
 return {BuildingType=id,PrereqTech=tech,TraitType=trait,
  PrereqDistrict=district or 'DISTRICT_CITY_CENTER',RequiresPlacement=false,IsWonder=false,
  MustPurchase=false,InternalOnly=false,RequiresAdjacentRiver=false,Coast=false,
  RequiresReligion=false,EnabledByReligion=false,Cost=80,Maintenance=0,Housing=0,
  OuterDefenseHitPoints=100,OuterDefenseStrength=3}
end
wall=building('BUILDING_WALLS','TECH_MASONRY')
mill=building('BUILDING_WATER_MILL','TECH_THE_WHEEL');mill.RequiresAdjacentRiver=true
library=building('BUILDING_LIBRARY','TECH_WRITING',nil,'DISTRICT_CAMPUS')
castle=building('BUILDING_CASTLE','TECH_CASTLES')
buildings={wall,mill,library,castle}
technologies={{TechnologyType='TECH_MASONRY',Index=0,Cost=80},
 {TechnologyType='TECH_THE_WHEEL',Index=1,Cost=80},
 {TechnologyType='TECH_WRITING',Index=2,Cost=50},
 {TechnologyType='TECH_CASTLES',Index=3,Cost=390}}
Game={GetLocalPlayer=function() return 0 end,GetCurrentGameTurn=function() return 39 end}
techs={HasTech=function() return false end,CanResearch=function() return true end}
Players={[0]={GetTechs=function() return techs end}}
PlayerConfigurations={[0]={GetLeaderTypeName=function() return 'LEADER_TEST' end,
 GetCivilizationTypeName=function() return 'CIVILIZATION_TEST' end}}
GameInfo={Technologies=catalog(technologies),Buildings=catalog(buildings),
 LeaderTraits=catalog({}),CivilizationTraits=catalog({{CivilizationType='CIVILIZATION_TEST',
 TraitType='TRAIT_UNIQUE'}}),BuildingReplaces=catalog({}),
 BuildingPrereqs=catalog({{Building='BUILDING_CASTLE',PrereqBuilding='BUILDING_WALLS'}}),
 Building_YieldChanges=catalog({{BuildingType='BUILDING_WATER_MILL',YieldType='YIELD_FOOD',
 YieldChange=1},{BuildingType='BUILDING_WATER_MILL',YieldType='YIELD_PRODUCTION',YieldChange=1}}),
 BuildingModifiers=catalog({{BuildingType='BUILDING_WATER_MILL',ModifierId='MODIFIER_EXAMPLE'}})}
"""


def run_lua(tmp_path, change=''):
    executable = shutil.which('texlua')
    if not executable:
        pytest.skip('texlua unavailable')
    path = tmp_path / 'research.lua'
    path.write_text(FIXTURE + '\n' + change + '\n' + rb.query(0))
    return subprocess.run([executable, str(path)], capture_output=True, text=True, timeout=5)


@pytest.fixture
def observed(tmp_path):
    result = run_lua(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    return rb.parse(result.stdout.splitlines(), 0)


def test_effective_direct_unlocks_leave_city_conditions_and_modifiers_unknown(observed):
    meta = observed['building_unlocks']
    buildings = {b['item_id']: b for b in meta['snapshot']['buildings']}
    assert set(buildings) == {'WALLS', 'WATER_MILL', 'CASTLE'}
    assert buildings['WATER_MILL']['fields']['RequiresAdjacentRiver'] is True
    assert buildings['WATER_MILL']['direct_yields'] == {'YIELD_FOOD': 1, 'YIELD_PRODUCTION': 1}
    assert buildings['CASTLE']['prerequisite_buildings'] == ['BUILDING_WALLS']
    assert meta['prerequisite_buildings_connective'] == 'OR'
    assert meta['modifier_effects'] == 'not_evaluated' and meta['base_source_catalog_used'] is False
    assert meta['loaded_ruleset_identity'] == 'unverified'
    assert 'fresh_native_catalog' in meta['action_authority']
    assert rb.project(observed, 0) == observed


@pytest.mark.parametrize('trait,expected', [(None, True), ('TRAIT_UNIQUE', True),
                                           ('TRAIT_FOREIGN', False), ('TRAIT_BARBARIAN', False)])
def test_player_trait_filter_is_effective_and_player_specific(tmp_path, trait, expected):
    value = 'nil' if trait is None else repr(trait)
    result = run_lua(tmp_path, f"wall.TraitType={value}")
    assert result.returncode == 0, result.stdout
    names = [b['item_id'] for b in rb.parse(result.stdout.splitlines(), 0)
             ['building_unlocks']['snapshot']['buildings']]
    assert ('WALLS' in names) is expected


@pytest.mark.parametrize('trait,base_present', [('TRAIT_UNIQUE', False), ('TRAIT_FOREIGN', True)])
def test_replacement_filter_precedes_tech_and_district_filter(tmp_path, trait, base_present):
    result = run_lua(tmp_path, f"""
    table.insert(buildings,building('BUILDING_UNIQUE','TECH_UNRESEARCHABLE','{trait}','DISTRICT_CAMPUS'))
    GameInfo.BuildingReplaces=setmetatable({{}},{{__call=function() local done=false
      return function() if not done then done=true;return {{CivUniqueBuildingType='BUILDING_UNIQUE',
       ReplacesBuildingType='BUILDING_WALLS'}} end end end}})
    """)
    assert result.returncode == 0, result.stdout
    names = [b['item_id'] for b in rb.parse(result.stdout.splitlines(), 0)
             ['building_unlocks']['snapshot']['buildings']]
    assert ('WALLS' in names) is base_present
    assert 'UNIQUE' not in names


@pytest.mark.parametrize('flag', ['RequiresPlacement', 'IsWonder', 'MustPurchase', 'InternalOnly'])
@pytest.mark.parametrize('value', ['true', 'nil'])
def test_unknown_or_unsupported_building_flags_never_authorize_hint(tmp_path, flag, value):
    result = run_lua(tmp_path, f'wall.{flag}={value}')
    assert result.returncode == 0
    assert 'WALLS' not in [b['item_id'] for b in rb.parse(result.stdout.splitlines(), 0)
                          ['building_unlocks']['snapshot']['buildings']]


@pytest.mark.parametrize('change', [
    'PlayerConfigurations=nil', 'Game.GetLocalPlayer=function() return 1 end',
    'techs.CanResearch=function() return nil end', 'GameInfo.BuildingPrereqs=nil',
    'GameInfo.Building_YieldChanges=nil', 'GameInfo.CivilizationTraits=nil',
    'for i=1,1025 do table.insert(buildings,wall) end',
])
def test_unavailable_or_unbounded_native_data_fails_without_a_complete_frame(tmp_path, change):
    result = run_lua(tmp_path, change)
    assert result.returncode != 0
    assert 'RBU_END|' not in result.stdout
    with pytest.raises(ValueError):
        rb.parse(result.stdout.splitlines(), 0)


def test_native_effective_values_change_digest_without_static_base_substitution(tmp_path, observed):
    result = run_lua(tmp_path, 'wall.Cost=117;wall.Housing=0.5;mill.RequiresAdjacentRiver=nil')
    changed = rb.parse(result.stdout.splitlines(), 0)
    assert changed['building_unlocks']['effective_slice_sha256'] != observed[
        'building_unlocks']['effective_slice_sha256']
    buildings = {b['item_id']: b for b in changed['building_unlocks']['snapshot']['buildings']}
    assert buildings['WALLS']['fields']['Cost'] == 117
    assert buildings['WALLS']['fields']['Housing'] == .5
    assert buildings['WATER_MILL']['fields']['RequiresAdjacentRiver'] is None


@pytest.mark.parametrize('mutate', [
    lambda rows: rows[:-2], lambda rows: rows + ['hidden output'],
    lambda rows: [rows[0].replace('|0|39', '|1|39'), *rows[1:]],
    lambda rows: [*rows[:-2], 'RBU_UNKNOWN|SECRET', *rows[-2:]],
    lambda rows: [*rows[:-2], 'RBU_FIELD|BUILDING_WALLS|Cost|99', *rows[-2:]],
    lambda rows: [r for r in rows if not r.startswith('RBU_FIELD|BUILDING_WALLS|Housing|')],
])
def test_closed_parser_refuses_incomplete_extra_foreign_or_conflicting_fields(tmp_path, mutate):
    rows = run_lua(tmp_path).stdout.splitlines()
    with pytest.raises(ValueError):
        rb.parse(mutate(rows), 0)


@pytest.mark.parametrize('change', ['foreign', 'extra_snapshot', 'extra_building', 'extra_field',
                                   'bad_digest', 'tampered_options', 'too_many_buildings'])
def test_projection_rejects_forged_self_consistent_metadata(observed, change):
    value = copy.deepcopy(observed)
    snapshot = value['building_unlocks']['snapshot']
    if change == 'foreign':
        snapshot['player_id'] = 1
    elif change == 'extra_snapshot':
        snapshot['hidden_units'] = ['foreign']
    elif change == 'extra_building':
        snapshot['buildings'][0]['hidden_city'] = 'c1:1'
    elif change == 'extra_field':
        snapshot['buildings'][0]['fields']['Secret'] = 1
    elif change == 'too_many_buildings':
        snapshot['buildings'] *= 30
    if change == 'bad_digest':
        value['building_unlocks']['effective_slice_sha256'] = '0' * 64
    elif change == 'tampered_options':
        value['options'] = []
    else:
        value = rb.package(snapshot)  # An attacker may know the hash algorithm.
    with pytest.raises(ValueError):
        VisibilityPolicy().project(value, 'available_research', 0, frozenset(), frozenset())


class BriefingFacade(Facade):
    def __init__(self, observation):
        super().__init__()
        self.you['researching'] = None
        self.observation = observation
        self.research_reads = []

    async def get_available_research(self, *, research_building_briefing=False):
        self.research_reads.append(research_building_briefing)
        return copy.deepcopy(self.observation if research_building_briefing else
                             self.observation['options'])


async def test_curator_collects_one_observation_retains_complete_hints_and_defers_active(observed):
    facade = BriefingFacade(observed)
    curator = ContextCurator(facade, 0, 1, research_building_briefing=True)
    await curator.refresh()
    await curator.refresh()
    doc = json.loads(curator.render(adaptive_task='strategy', target_chars=1)
                     .removeprefix(CONTEXT_MARKER))
    assert facade.research_reads == [True]
    assert doc['research_building_unlocks'] == observed['building_unlocks']
    assert len(curator.render(adaptive_task='economy')) > 1
    facade.you['researching'] = 'MASONRY'
    curator.action_result('set_research', {'tech_id': 'MASONRY'}, {'status': 'accepted'})
    await curator.refresh()
    doc = json.loads(curator.render(adaptive_task='strategy').removeprefix(CONTEXT_MARKER))
    assert doc['research_building_unlocks']['status'] == 'not_requested_active_choice'
    assert facade.research_reads == [True]


async def test_disabled_mode_preserves_existing_payload_and_has_no_briefing_read(observed):
    left, right = BriefingFacade(observed), BriefingFacade(observed)
    a, b = ContextCurator(left, 0, 8000), ContextCurator(right, 0, 8000,
                                                      research_building_briefing=False)
    await a.refresh()
    await b.refresh()
    assert a.render() == b.render()
    assert left.research_reads == right.research_reads == [False]
    assert 'research_building_unlocks' not in a.render()


async def test_counted_and_generated_context_contains_full_unlock_snapshot(monkeypatch, observed):
    runtime, controller, requests, _, ledger, _ = harness(monkeypatch, target=1)
    monkeypatch.setattr('civ_arena.agents.llm.strategic_controller.run_scouting',
                        AsyncMock(return_value={'execution': []}))
    runtime.llm = replace(runtime.llm, research_building_briefing=True, max_result_chars=32)
    runtime.profile = replace(runtime.profile, player_id=0, llm=runtime.llm)
    facade = BriefingFacade(observed)
    try:
        await advance(controller, runtime, facade, 1)
        counted, generated = [body for _, body in requests]
        assert counted == {key: value for key, value in generated.items() if key != 'max_tokens'}
        doc = json.loads(counted['messages'][0]['content'].split(CONTEXT_MARKER, 1)[1])
        assert doc['research_building_unlocks'] == observed['building_unlocks']
        assert len(ledger) == 2 and facade.research_reads == [True]
        await advance(controller, runtime, facade, 2)
        assert len(ledger) == 2 and facade.research_reads == [True]
    finally:
        await runtime.client.aclose()


async def test_controller_keyword_is_auditable_and_replayed_without_new_model_tool():
    referee = SimpleNamespace(observe=AsyncMock(return_value=[]))
    ctx = SimpleNamespace(referee=referee)
    await tools.get_available_research(ctx, research_building_briefing=True)
    referee.observe.assert_awaited_once_with(ctx, ObserveKind.AVAILABLE_RESEARCH,
                                           research_building_briefing=True)
    facade = SimpleNamespace(get_available_research=AsyncMock(return_value=[]),
                             end_turn=AsyncMock(return_value={'status': 'accepted'}))
    replay = ReplayRuntime([RecordedCall('get_available_research',
                          {'research_building_briefing': True}, None),
                          RecordedCall('end_turn', {}, None)])
    await replay.take_turn(facade)
    facade.get_available_research.assert_awaited_once_with(research_building_briefing=True)


async def test_optin_observation_dispatches_one_read_and_checks_player(monkeypatch, tmp_path):
    from civ_arena.game.civ6.firetuner import FireTunerAdapter
    rows = run_lua(tmp_path).stdout.splitlines()
    connection = SimpleNamespace(execute_read=AsyncMock(return_value=rows))
    adapter = FireTunerAdapter(conn=connection)
    value = await adapter.observe(ObserveRequest(ObserveKind.AVAILABLE_RESEARCH, 0,
                                                 research_building_briefing=True))
    assert value['building_unlocks']['snapshot']['player_id'] == 0
    connection.execute_read.assert_awaited_once()
    assert 'RequestOperation' not in connection.execute_read.call_args.args[0]


@pytest.mark.parametrize('flag', [None, 1, 'true'])
def test_optin_requires_exact_boolean_and_adaptive_context(flag):
    block = dict(VALID, research_building_briefing=flag)
    with pytest.raises(ConfigError, match='research_building_briefing'):
        parse_config(llm_doc(block))


def test_config_enables_only_strategic_adaptive_controller():
    doc = llm_doc(dict(VALID, research_building_briefing=True,
                      adaptive_context={'provider_context_tokens': 1000000}))
    with pytest.raises(ConfigError, match='strategic_autopilot'):
        parse_config(doc)
    doc['agents'][0]['decision_mode'] = 'strategic_autopilot'
    assert parse_config(doc).agents[0].llm.research_building_briefing is True


def test_only_current_native_researchability_can_supply_direct_unlocks(tmp_path):
    result = run_lua(tmp_path, '''
    techs.HasTech=function(self,index) return index==0 end
    techs.CanResearch=function(self,index) return index~=1 end
    ''')
    value = rb.parse(result.stdout.splitlines(), 0)
    assert {r['tech_id'] for r in value['options']} == {'WRITING', 'CASTLES'}
    assert [b['item_id'] for b in value['building_unlocks']['snapshot']['buildings']] == ['CASTLE']


def test_native_exact_integer_floats_and_small_fractional_yields_normalize(tmp_path):
    result = run_lua(tmp_path, 'technologies[1].Cost=80.0;wall.Housing=0.000001')
    value = rb.parse(result.stdout.splitlines(), 0)
    assert next(t for t in value['options'] if t['tech_id'] == 'MASONRY')['cost'] == 80
    assert next(b for b in value['building_unlocks']['snapshot']['buildings']
                if b['item_id'] == 'WALLS')['fields']['Housing'] == .000001


async def test_city_prerequisite_facts_stay_unknown_and_action_invalidates_research(observed):
    facade = BriefingFacade(observed)
    facade.cities = [{'city_id': 'c0:1', 'owner_id': 0, 'coord': '0,0', 'production_queue': []}]
    curator = ContextCurator(facade, 0, 8000, research_building_briefing=True)
    await curator.refresh()
    doc = json.loads(curator.render(adaptive_task='strategy').removeprefix(CONTEXT_MARKER))
    facts = doc['research_building_city_conditions']['c0:1']
    assert facts['built_or_pillaged'] == facts['prerequisite_buildings_satisfied'] == 'unknown'
    assert facts['river_and_terrain_requirements'] == facts['city_center_complete'] == 'unknown'
    curator.action_result('move_unit', {'unit_id': 'u0:1', 'dest': '1,0'}, {'status': 'accepted'})
    await curator.refresh(include_options=False)
    doc = json.loads(curator.render(adaptive_task='strategy').removeprefix(CONTEXT_MARKER))
    assert 'snapshot' not in doc['research_building_unlocks']
    assert doc['research_building_unlocks']['status'] == 'deferred'
    await curator.refresh()
    assert facade.research_reads == [True, True]  # Rewards may change research options.
