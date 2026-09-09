"""OVX|2 player economy: parse rules, self-only projection, and the
curator's five-key default with the own_economy_context opt-in."""

from __future__ import annotations

import json

import pytest

from civ_arena.agents.llm.context_curator import CONTEXT_MARKER, ContextCurator
from civ_arena.arena.visibility import VisibilityPolicy
from civ_arena.game.civ6 import lua_translator, response_parser
from test_context_curator import World

LEGACY_YOU = {"player_id": 0, "civ_name": "CIV", "gold": 100,
              "researched": [], "researching": None}
RICH_YOU = {**LEGACY_YOU, "science": 6, "culture": 5, "faith": 4,
            "gold_per_turn": 2, "upkeep": 3, "era": 0,
            "progressing_civic": "CIVIC_A", "civic_progress": 7,
            "civic_cost": 60, "civics": ["CIVIC_A"]}


def test_ovx2_parse_yields_and_civic_progress_split():
    doc = response_parser.parse_overview([
        "OVX|2", "TURN|7", "OVERA|ERA_ANCIENT",
        # read-transport shape: civic-progress triple stays '?' (absent)
        "OVROW|0|CIVILIZATION_SPAIN|120|MINING|6|5|4|2|3|0|?|?|?",
        "OVRESEARCHED|0|MINING;POTTERY",
        "OVCIVICS|0|CIVIC_CODE_OF_LAWS",
        # write-transport shape: civic progress answered
        "OVROW|1|CIVILIZATION_ROME|90|-|7|6|3|3|4|1|CIVIC_B|12|70",
        "---END---"])
    spain = doc["players"]["0"]
    assert spain["science"] == 6 and spain["culture"] == 5
    assert spain["faith"] == 4 and spain["gold_per_turn"] == 2
    assert spain["upkeep"] == 3 and spain["era"] == "0"
    assert spain["researched"] == ["MINING", "POTTERY"]
    assert spain["civics"] == ["CIVIC_CODE_OF_LAWS"]
    for unread in ("progressing_civic", "civic_progress", "civic_cost"):
        assert unread not in spain
    rome = doc["players"]["1"]
    assert rome["progressing_civic"] == "CIVIC_B"
    assert rome["civic_progress"] == 12 and rome["civic_cost"] == 70
    assert doc["game_era"] == "ERA_ANCIENT"
    # legacy OVX|1 rows parse untouched
    legacy = response_parser.parse_overview([
        "OVX|1", "TURN|7", "OVROW|0|CIVILIZATION_SPAIN|120|MINING", "---END---"])
    assert set(legacy["players"]["0"]) == {
        "player_id", "civ_name", "gold", "researched", "researching", "alive"}
    assert "game_era" not in legacy


def test_parse_overview_accepts_era_name_string_from_live_probe():
    """Codex r2 finding 2: the Lua emits era NAMES (e.g. ERA_ANCIENT) on
    the live probe path; the parser must accept both forms (canonical
    int and identifier string) — and REJECT malformed tokens."""
    lines = [
        'OVX|2', 'TURN|1',
        'OVROW|0|CIV_FAKE|100|TECH_FAKE|10|8|5|2|1|ERA_ANCIENT|CIVIC_FAKE|7|60',
        'OVRESEARCHED|0',
        'OVCIVICS|0|CIVIC_FAKE', 'OVERA|ERA_ANCIENT', '---END---']
    doc = response_parser.parse_overview(lines)
    assert doc["players"]["0"]["era"] == "ERA_ANCIENT"
    assert doc["game_era"] == "ERA_ANCIENT"
    # malformed tokens must still raise (Codex r3 finding 3;
    # Codex r4 finding 2: fixtures must carry a full 13-field
    # OVROW so the era branch is reached, not the field-count
    # check; and the strict ERA_ prefix rejects arbitrary
    # identifiers like 'x' and reserved words like 'nan').
    # Codex r5 finding 1: ERA_-prefixed but unknown era names must
    # also raise (the closed civ6 catalog is the authority).
    for bad in ("1.5", "+7", "nan", "x", "ERA_ANCIENT_BAD",
                "era_ancient", ""):
        with pytest.raises(ValueError, match="non-canonical era"):
            response_parser.parse_overview([
                'OVX|2', 'TURN|1',
                f'OVROW|0|C|F|0|0|0|0|0|0|{bad}|-|7|60', '---END---'])


def test_ovx2_ordering_and_strictness():
    # OVCIVICS must FOLLOW its OVROW
    with pytest.raises(ValueError, match="before its OVROW"):
        response_parser.parse_overview([
            "OVX|2", "TURN|7", "OVCIVICS|0|CIVIC_A",
            "OVROW|0|CIV|100|-|1|1|1|1|1|0|?|?|?", "---END---"])
    with pytest.raises(ValueError, match="unknown player"):
        response_parser.parse_overview([
            "OVX|2", "TURN|7", "OVRESEARCHED|9|MINING", "---END---"])
    for bad in ("OVROW|0|CIV|100|-|6.5|1|1|1|1|0|?|?|?",
                "OVROW|0|CIV|100|-|6|1|1|1|1|+7|?|?|?",
                "OVERA|ERA|x"):
        with pytest.raises(ValueError):
            response_parser.parse_overview(["OVX|2", "TURN|7", bad, "---END---"])


def test_projection_public_never_carries_economy():
    doc = {"turn": 7, "players": {
        "0": dict(RICH_YOU, alive=True),
        "1": dict(LEGACY_YOU, player_id=1, civ_name="CIV_B", alive=True)}}
    policy = VisibilityPolicy()
    public = policy._public_match(doc)  # noqa: SLF001
    assert all(set(p) == {"player_id", "civ_name", "alive"}
               for p in public["players"])
    you = policy._own_player(doc, 0)  # noqa: SLF001
    assert you["science"] == 6 and you["era"] == 0
    other = policy._own_player(doc, 1)  # noqa: SLF001
    assert set(other) == set(LEGACY_YOU)


def test_texlua_minors_never_ride_the_ovx_majors(tmp_path):
    """The OVX Lua filters majors (public.players never contains a
    non-major BY CONSTRUCTION) and omits civic progress without
    GetCulturalProgress (the read transport)."""
    import shutil
    import subprocess

    executable = shutil.which("texlua")
    if executable is None:
        pytest.skip("texlua unavailable for executable economy fixture")
    stub = """
local function player(id, major, cu)
  return {IsAlive=function() return true end, IsMajor=function() return major end,
   IsBarbarian=function() return false end, GetCulture=function() return cu end,
   GetTechs=function()
     return {GetScienceYield=function() return 6 end,
             GetResearchingTech=function() return -1 end,
             HasTech=function() return false end} end,
   GetTreasury=function()
     return {GetGoldBalance=function() return 100 end,
             GetGoldYield=function() return 2 end,
             GetTotalMaintenance=function() return 3 end} end,
   GetReligion=function() return {GetFaithYield=function() return 4 end} end,
   GetEra=function() return 0 end}
end
-- GameCore-shaped culture object: NO GetCulturalProgress/GetCostNextCivic
local cu = {GetCultureYield=function() return 5 end,
            GetProgressingCivic=function() return -1 end,
            HasCivic=function() return true end}
Players={[0]=player(0, true, cu), [12]=player(12, false, cu)}
PlayerConfigurations={
 [0]={GetCivilizationTypeName=function() return 'CIVILIZATION_SPAIN' end}}
GameInfo={Technologies={{Index=1, TechnologyType='TECH_MINING'},
                        {Index=2, TechnologyType='TECH_POTTERY'}},
 Civics={{Index=1, CivicType='CIVIC_CODE_OF_LAWS'}}}
Game={GetCurrentGameTurn=function() return 3 end}
"""
    path = tmp_path / "economy.lua"
    path.write_text(stub + lua_translator.overview_read())
    result = subprocess.run([executable, str(path)], capture_output=True,
                            text=True, timeout=5, check=True)
    lines = result.stdout.splitlines()
    rows = [row for row in lines if row.startswith("OVROW|")]
    assert rows == ["OVROW|0|CIVILIZATION_SPAIN|100|-|6|5|4|2|3|0|?|?|?"]
    doc = response_parser.parse_overview(lines)
    spain = doc["players"]["0"]
    for unread in ("progressing_civic", "civic_progress", "civic_cost"):
        assert unread not in spain
    assert "OVCIVICS|" not in result.stdout  # HasCivic false on the stub
    assert "OVERA|" not in result.stdout      # Eras table absent: omitted


class _YouWorld(World):
    YOU: dict = {}

    async def get_overview(self):
        self.calls.append(('get_overview', {}))
        return {'you': dict(self.YOU), 'public': {'players': []}}


class LegacyYouWorld(_YouWorld):
    YOU = LEGACY_YOU


class RichWorld(_YouWorld):
    YOU = RICH_YOU


async def test_curator_you_is_byte_identical_by_default():
    legacy = ContextCurator(LegacyYouWorld(), 0, 8000)
    rich = ContextCurator(RichWorld(), 0, 8000)
    await legacy.refresh(include_options=False)
    await rich.refresh(include_options=False)
    default = json.loads(rich.render()[len(CONTEXT_MARKER):])
    baseline = json.loads(legacy.render()[len(CONTEXT_MARKER):])
    assert default['you'] == baseline['you']
    assert default['you'] == LEGACY_YOU
    assert 'science' not in default['you'] and 'era' not in default['you']


async def test_curator_opt_in_adds_economy_within_budget():
    curator = ContextCurator(RichWorld(), 0, 8000, own_economy_context=True)
    with pytest.raises(ValueError, match='boolean'):
        ContextCurator(RichWorld(), 0, 8000, own_economy_context='yes')
    await curator.refresh(include_options=False)
    rendered = curator.render()
    assert len(rendered) <= 8000
    doc = json.loads(rendered[len(CONTEXT_MARKER):])
    assert doc['you']['science'] == 6 and doc['you']['era'] == 0
    assert doc['you']['civics'] == ['CIVIC_A']
    # the decision packet rides the SAME gate
    from civ_arena.agents.llm.decision_packet import decision_snapshot
    snapshot = decision_snapshot(curator, 7)
    assert snapshot['you']['gold_per_turn'] == 2
    plain = ContextCurator(LegacyYouWorld(), 0, 8000)
    await plain.refresh(include_options=False)
    assert 'gold_per_turn' not in decision_snapshot(plain, 7)['you']
