"""Golden packet parity: the SAME world through the old (CITIES|1 /
VMAP|3 / OVX|1) and new (CITIES|2 / VMAP|4 / OVX|2) wire fixtures yields
BYTE-IDENTICAL model-facing packets — the ONE default delta is city hp
(the fake hp:100 placeholder disappears and the real hp appears when the
extended read answers; contract §5)."""

from __future__ import annotations

import json

from civ_arena.agents.llm.context_curator import CONTEXT_MARKER, ContextCurator
from civ_arena.arena.visibility import VisibilityPolicy
from civ_arena.canonical import canonical
from civ_arena.game.civ6 import response_parser

# -- the same world, old and new fixtures --------------------------------------

OLD_OVERVIEW = ["OVX|1", "TURN|7",
                "OVROW|0|CIVILIZATION_SPAIN|120|MINING",
                "OVRESEARCHED|0|MINING;POTTERY", "---END---"]
NEW_OVERVIEW = ["OVX|2", "TURN|7", "OVERA|ERA_ANCIENT",
                "OVROW|0|CIVILIZATION_SPAIN|120|MINING|6|5|4|2|3|0|?|?|?",
                "OVRESEARCHED|0|MINING;POTTERY",
                "OVCIVICS|0|CIVIC_CODE_OF_LAWS", "---END---"]

OLD_CITIES = ["CITIES|1",
              "CITYROW|c0:1|0|Madrid|2|0|3|SCOUT",
              "CITYROW|c1:2|1|Rome|30|0|4|-", "---END---"]
NEW_CITIES = ["CITIES|2",
              "CITYROW|c0:1|0|Madrid|2|0|3|SCOUT|true|true|180|200|10|15|3|5|4|"
              "BUILDING_MONUMENT|DISTRICT_CITY_CENTER",
              "CITYROW|c1:2|1|Rome|30|0|4|-|true|false|?|?|?|?|?|?|?|-|-",
              "---END---"]

OLD_MAP = ["VMAP|3", "TURN|7",
           "TILEROW|2|0|TERRAIN_GRASS|true|0|c0:1",
           "TILEROW|30|0|TERRAIN_PLAINS|true|1|c1:2",
           "TILEROW|3|0|TERRAIN_GRASS_HILLS|true|-1|", "---END---"]
NEW_MAP = ["VMAP|4", "TURN|7",
           "TILEROW|2|0|TERRAIN_GRASS|true|0|c0:1|FEATURE_FOREST|"
           "RESOURCE_IRON|IMPROVEMENT_FARM|-|true|12|true",
           "TILEROW|30|0|TERRAIN_PLAINS|true|1|c1:2|-|-|-|-|false|0|true",
           "TILEROW|3|0|TERRAIN_GRASS_HILLS|true|-1||-|-|-|-|false|7|false",
           "---END---"]


def _packets(overview, cities, tiles, *, player=0, own_economy=False):
    policy = VisibilityPolicy()
    ov = response_parser.parse_overview(overview)
    ct = response_parser.parse_cities(cities, qualified=True)
    mp = response_parser.parse_visible_map(tiles)
    observable = frozenset(mp["visible"])
    remembered = frozenset(mp["tiles"]) - observable
    return {
        "overview": policy.project(ov, "overview", player, observable, remembered),
        "cities": policy.project(ct, "cities", player, observable, remembered),
        "map": policy.project(mp, "visible_map", player, observable, remembered),
    }


def test_projection_packets_byte_identical_except_hp():
    old = _packets(OLD_OVERVIEW, OLD_CITIES, OLD_MAP)
    new = _packets(NEW_OVERVIEW, NEW_CITIES, NEW_MAP)
    # the map packet is byte-identical: the new static/dynamic tile keys
    # NEVER reach it
    assert canonical(old["map"]) == canonical(new["map"])
    tile = next(iter(new["map"]["tiles"].values()))
    assert "feature" not in tile and "resource" not in tile
    assert "engine_visible" not in tile and "appeal" not in tile
    # overview: public is byte-identical; `you` keeps the five legacy keys
    # on the old wire and gains ONLY the self-scoped economy keys on the
    # new one (SELF data — the CONTEXT layer gates it; see below)
    assert canonical(old["overview"]["public"]) == canonical(new["overview"]["public"])
    assert set(old["overview"]["you"]) == {
        "player_id", "civ_name", "gold", "researched", "researching"}
    assert set(new["overview"]["you"]) - set(old["overview"]["you"]) <= {
        "science", "culture", "faith", "gold_per_turn", "upkeep", "era",
        "progressing_civic", "civic_progress", "civic_cost", "civics"}
    # cities: the ONLY projection delta is own-city enrichment (hp real
    # instead of absent, plus the own-scoped extended keys — own city
    # extras are own-cities-only by scope); every PUBLIC key is identical
    own_extras = {"max_hp", "food_bucket", "food_threshold", "food_surplus",
                  "turns_to_growth", "turns_to_production", "buildings",
                  "districts", "is_major", "is_capital"}
    old_own = {c["city_id"]: c for c in old["cities"]}["c0:1"]
    new_own = {c["city_id"]: c for c in new["cities"]}["c0:1"]
    assert "hp" not in old_own
    assert new_own["hp"] == 180 and new_own["max_hp"] == 200
    assert set(new_own) - set(old_own) <= {"hp"} | own_extras
    public_keys_old = {k: v for k, v in old_own.items()}
    public_keys_new = {k: v for k, v in new_own.items()
                       if k not in own_extras and k != "hp"}
    assert public_keys_old == public_keys_new
    # foreign city: hp conditional on BOTH wires (unread -> absent)
    old_foreign = {c["city_id"]: c for c in old["cities"]}["c1:2"]
    new_foreign = {c["city_id"]: c for c in new["cities"]}["c1:2"]
    assert "hp" not in old_foreign and "hp" not in new_foreign
    assert old_foreign == new_foreign


class _ParityFacade:
    """The curated render's view of the same projected world."""

    def __init__(self, packets):
        self._packets = packets
        self.calls = []

    async def get_visible_map(self):
        self.calls.append(("get_visible_map", {}))
        return self._packets["map"]

    async def get_units(self):
        self.calls.append(("get_units", {}))
        return []

    async def get_cities(self):
        self.calls.append(("get_cities", {}))
        return self._packets["cities"]

    async def get_overview(self):
        self.calls.append(("get_overview", {}))
        return self._packets["overview"]


async def test_curated_render_byte_identical_by_default():
    docs = []
    for overview, cities, tiles in ((OLD_OVERVIEW, OLD_CITIES, OLD_MAP),
                                    (NEW_OVERVIEW, NEW_CITIES, NEW_MAP)):
        curator = ContextCurator(_ParityFacade(_packets(overview, cities, tiles)),
                                 0, 8000)
        await curator.refresh(include_options=False)
        rendered = curator.render()
        assert len(rendered) <= 8000
        docs.append(json.loads(rendered[len(CONTEXT_MARKER):]))
    old_doc, new_doc = docs
    # the MODEL packet is byte-identical except the own-city hp delta:
    # `you` keeps exactly the five legacy keys on both wires (the economy
    # keys live behind own_economy_context)
    assert old_doc["you"] == new_doc["you"]
    assert set(new_doc["you"]) == {"player_id", "civ_name", "gold",
                                   "researched", "researching"}

    def _strip_hp(rows):
        return [{k: v for k, v in row.items() if k != "hp"} for row in rows]

    for key in old_doc:
        if key == "own_cities":
            continue
        assert old_doc[key] == new_doc[key], key
    assert _strip_hp(old_doc["own_cities"]) == _strip_hp(new_doc["own_cities"])
    assert all("hp" not in row for row in old_doc["own_cities"])
    assert any(row.get("hp") == 180 for row in new_doc["own_cities"])
    assert "science" not in new_doc["you"] and "era" not in new_doc["you"]


async def test_curated_render_hp_delta_is_the_only_optin_change():
    rich = ContextCurator(
        _ParityFacade(_packets(NEW_OVERVIEW, NEW_CITIES, NEW_MAP)), 0, 8000,
        own_economy_context=True)
    await rich.refresh(include_options=False)
    rendered = rich.render()
    assert len(rendered) <= 8000
    doc = json.loads(rendered[len(CONTEXT_MARKER):])
    # opt-in: the economy keys flow for SELF, within the same budget
    assert doc["you"]["gold"] == 120
    assert doc["you"]["science"] == 6 and doc["you"]["era"] == "0"
    assert doc["you"]["civics"] == ["CIVIC_CODE_OF_LAWS"]
    # the read-transport civic-progress triple stayed unread (absent)
    for unread in ("progressing_civic", "civic_progress", "civic_cost"):
        assert unread not in doc["you"]
