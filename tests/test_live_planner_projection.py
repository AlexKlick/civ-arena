"""End-to-end live-wire → parser → visibility → PlannerBelief regression tests.

These pin the full seam from raw UNITROW/CITYROW/OVROW/TILEROW wire lines
(what `response_parser.parse_*` consumes) through `VisibilityPolicy.project`
(what `Referee.observe` runs) into `PlannerBelief.observe_*` (what the
planner-runtime consumes). A shape drift anywhere in that chain — parser,
projection, or belief-strict-check — surfaces here as a `ValueError`.

The original bug that motivated this test file: `VisibilityPolicy._unit` was
emitting `is_barbarian` for OWN units (always False from the player's own
perspective — barbarians are AI-controlled by a separate BarbarianPlayer).
`PlannerBelief.OWN_UNIT_FIELDS` (belief.py:67) does not include the field;
the strict check at belief.py:185 fired on the first observation and aborted
the dispatch at turn 1. Fix landed in `arena/visibility.py` (drop
`**metadata` from the own-unit return).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from civ_arena.arena.visibility import VisibilityPolicy
from civ_arena.game.civ6.response_parser import (
    parse_cities,
    parse_overview,
    parse_units,
    parse_visible_map,
)
from civ_arena.planner.belief import PlannerBelief

PLAYER = 0
TURN = 1


# -- helpers ----------------------------------------------------------------

def _project_units(player_id: int, units_doc: list[dict], *, observable=None):
    policy = VisibilityPolicy()
    return policy.project(
        units_doc, "units", player_id,
        observable=observable or frozenset(),
        remembered=frozenset())


def _project_cities(player_id: int, cities_doc: list[dict], *, observable=None, remembered=None):
    policy = VisibilityPolicy()
    return policy.project(
        cities_doc, "cities", player_id,
        observable=observable or frozenset(),
        remembered=remembered or frozenset())


def _project_overview(player_id: int, overview_doc: dict):
    policy = VisibilityPolicy()
    return policy.project(
        overview_doc, "overview", player_id,
        observable=frozenset(), remembered=frozenset())


def _project_map(player_id: int, map_doc: dict, *, observable=None, remembered=None):
    policy = VisibilityPolicy()
    return policy.project(
        map_doc, "visible_map", player_id,
        observable=observable or frozenset(),
        remembered=remembered or frozenset())


def _boundary_reencode(projected):
    """Mirror production: EntityBoundary re-encodes qualified string ids
    into Cantor-pair ints before PlannerBelief ever sees them."""
    from civ_arena.planner.entity_boundary import EntityBoundary

    async def _same():
        return projected
    facade = SimpleNamespace(
        get_units=_same, get_cities=_same, get_overview=_same,
        get_visible_map=_same)
    boundary = EntityBoundary(facade)

    async def _units():
        return await boundary.get_units()

    async def _cities():
        return await boundary.get_cities()
    return _units, _cities


# -- units ------------------------------------------------------------------

class TestLiveUnitsProjection:
    """Wire UNITROW → parser → visibility → belief.observe_units."""

    def test_own_14field_row_with_is_barbarian_does_not_break_belief(self):
        # The original bug: 14-field rows carry is_barbarian=false. Visibility
        # used to propagate it into the own-unit dict, which then had an
        # extra field beyond OWN_UNIT_FIELDS → strict check raised.
        lines = ["UNITS|1",
                 "UNITROW|u0:65536|0|SETTLER|36|20|100|0|2|0|0|false|false|100|true",
                 "UNITROW|u0:131073|0|WARRIOR|35|20|100|2|2|20|0|false|false|100|true",
                 "---END---"]
        wire = parse_units(lines, qualified=True)
        belief = PlannerBelief(player_id=PLAYER)
        # Should not raise.
        belief.observe_units(_project_units(PLAYER, wire), turn=TURN)
        assert len(belief.own_units) == 2
        assert belief.own_units["u0:65536"]["type"] == "SETTLER"
        assert belief.own_units["u0:131073"]["hp"] == 100

    def test_own_11field_row_without_health_metadata(self):
        # Pre-M4 / lite rows have no max_hp/health_valid/is_barbarian.
        # Format: uid|pid|type|q|r|hp|moves|maxmoves|combat|ranged|fortified
        lines = ["UNITS|1",
                 "UNITROW|u0:65536|0|SETTLER|36|20|100|0|2|0|0|false",
                 "---END---"]
        wire = parse_units(lines, qualified=True)
        belief = PlannerBelief(player_id=PLAYER)
        belief.observe_units(_project_units(PLAYER, wire), turn=TURN)
        assert len(belief.own_units) == 1

    def test_own_unit_with_unknown_health_is_observed_lite(self):
        # Wire says hp=unknown, health_valid=false. Visibility emits hp=None
        # and hp_bucket=None; belief must accept this as a legitimate
        # "unknown health" observation (not a shape violation). The
        # determinizer (_hp_from_bucket) must also handle None and default
        # to full health 100. End-to-end exercise happens in test_determinizer_*
        # once unit_ids have been re-encoded via EntityBoundary.
        lines = ["UNITS|1",
                 "UNITROW|u0:65536|0|WARRIOR|36|20|unknown|2|2|20|0|false|false|unknown|false",
                 "---END---"]
        wire = parse_units(lines, qualified=True)
        belief = PlannerBelief(player_id=PLAYER)
        belief.observe_units(_project_units(PLAYER, wire), turn=TURN)
        assert belief.own_units["u0:65536"]["hp"] is None
        # The determinizer must not crash on None hp_bucket — direct unit test:
        from civ_arena.planner.belief import _hp_from_bucket
        assert _hp_from_bucket(None) == 100

    def test_own_and_foreign_mixed_observation(self):
        # Both own and foreign units in one observation. Foreign get the
        # FOREIGN_UNIT_FIELDS allowlist (incl. is_barbarian); own stay clean.
        lines = ["UNITS|1",
                 "UNITROW|u0:65536|0|SETTLER|36|20|100|0|2|0|0|false|false|100|true",
                 "UNITROW|u1:65536|1|WARRIOR|10|10|100|2|2|20|0|false|false|100|true",
                 "---END---"]
        wire = parse_units(lines, qualified=True)
        observable = frozenset({"10,10"})
        projected = _project_units(PLAYER, wire, observable=observable)
        belief = PlannerBelief(player_id=PLAYER)
        belief.observe_units(projected, turn=TURN)
        assert len(belief.own_units) == 1
        assert belief.own_units["u0:65536"]["type"] == "SETTLER"
        assert belief.foreign_units  # foreign warrior observed

    def test_hidden_foreign_unit_is_absent_not_masked(self):
        # A foreign unit at an unobserved coord must not appear at all
        # (absent, not null-masked — the leak checker depends on this).
        lines = ["UNITS|1",
                 "UNITROW|u1:65536|1|WARRIOR|10|10|100|2|2|20|0|false|false|100|true",
                 "---END---"]
        wire = parse_units(lines, qualified=True)
        projected = _project_units(PLAYER, wire, observable=frozenset())
        belief = PlannerBelief(player_id=PLAYER)
        belief.observe_units(projected, turn=TURN)
        assert belief.foreign_units == {}

    def test_foreign_unit_unknown_type_does_not_crash_build_state_doc(self):
        # The original bug: SLINGER appeared in the wire (barbarian), and
        # build_state_doc did `UNIT_TYPES[u["type"]]` which raised KeyError.
        # Two layers of defense: SLINGER is now in UNIT_TYPES, AND the
        # lookup uses .get() with a conservative default. This test pins
        # BOTH layers — if a future unit type (e.g. LIGHT_CHARIOT) shows
        # up before it's catalogued, the planner still runs.
        lines = ["UNITS|1",
                 "UNITROW|u1:65536|1|SLINGER|10|10|100|2|2|5|15|false|false|100|true",
                 "UNITROW|u1:131073|1|LIGHT_CHARIOT|11|10|100|2|4|15|0|false|false|100|true",
                 "---END---"]
        wire = parse_units(lines, qualified=True)
        observable = frozenset({"10,10", "11,10"})
        projected = _project_units(PLAYER, wire, observable=observable)
        # Mirror production: EntityBoundary re-encodes qualified string IDs
        # into Cantor-pair ints before belief.observe_units sees them; the
        # old test path left them as strings, which crashed build_state_doc's
        # `int(u[1:])` lookup.
        from civ_arena.planner.entity_boundary import EntityBoundary
        import asyncio

        async def _units():
            return projected
        facade = SimpleNamespace(
            get_units=_units, get_cities=_units, get_overview=_units,
            get_visible_map=_units)
        boundary = EntityBoundary(facade)
        belief = PlannerBelief(player_id=PLAYER)
        belief.observe_units(asyncio.run(boundary.get_units()), turn=TURN)
        # Should not raise even with LIGHT_CHARIOT (unknown to UNIT_TYPES).
        from civ_arena.game.sim.state import DEFAULT_UNIT_SPEC
        from civ_arena.planner.belief import build_state_doc
        bstate = build_state_doc(belief, seed=24)
        # SLINGER is catalogued: real stats.
        slinger = next((u for u in bstate["units"].values()
                        if u["type"] == "SLINGER"), None)
        assert slinger is not None
        assert slinger["strength"] == 5
        assert slinger["ranged_strength"] == 15
        # LIGHT_CHARIOT is NOT catalogued: defaults applied, no crash.
        # Note: build_state_doc uses spec["mv"] but pulls strength/ranged
        # from the observation directly (the wire already has them).
        # The DEFAULT only kicks in for movement.
        unknown = next((u for u in bstate["units"].values()
                        if u["type"] == "LIGHT_CHARIOT"), None)
        assert unknown is not None
        assert unknown["movement"] == DEFAULT_UNIT_SPEC["mv"]
        assert unknown["max_movement"] == DEFAULT_UNIT_SPEC["mv"]


# -- cities -----------------------------------------------------------------

class TestLiveCitiesProjection:
    """Wire CITYROW → parser → visibility → belief.observe_cities."""

    def test_own_18field_row_with_extras_does_not_break_belief(self):
        # 18-field rows: cid|pid|name|q|r|pop|queue|major|capital|hp|maxhp|
        # food|thr|surplus|grow|prodturns|buildings|districts (18 total).
        lines = ["CITIES|2",
                 "CITYROW|c0:7|0|CITY_A|36|20|1|Settler|true|true|100|100|0|2|0|10|10|BUILDING_GRANARY|-",
                 "---END---"]
        wire = parse_cities(lines, qualified=True)
        belief = PlannerBelief(player_id=PLAYER)
        belief.observe_cities(_project_cities(PLAYER, wire), turn=TURN)
        assert len(belief.own_cities) == 1
        assert belief.own_cities["c0:7"]["name"] == "CITY_A"

    def test_own_minor_7field_row_without_production_queue(self):
        # 7-field minor row: cid|pid|name|q|r|pop|queue (queue=`?` = unread
        # for pcall-guarded minors). player_id=8 matches the city owner.
        # The strict required-minus-present check raises on absent
        # production_queue; if that's still raising here after the fix,
        # the projection layer's "minor cities get a default queue"
        # default hasn't landed yet.
        lines = ["CITIES|2",
                 "CITYROW|c8:1|8|CITYSTATE|10|10|1|?",
                 "---END---"]
        wire = parse_cities(lines, qualified=True)
        belief = PlannerBelief(player_id=8)
        belief.observe_cities(_project_cities(8, wire), turn=TURN)
        assert len(belief.own_cities) == 1
        # production_queue is absent on the wire (`?` = unread) but should
        # be defaulted to [] in the belief layer (mirrors OWN_CITY_DEFAULTS).
        assert belief.own_cities["c8:1"].get("production_queue") == []


# -- overview ---------------------------------------------------------------

class TestLiveOverviewProjection:
    """Wire OVROW → parser → visibility → belief.observe_overview."""

    def test_overview_picks_you_and_public_players(self):
        # Wire format: 4-field OVROW = pid|civ|gold|res; OVRESEARCHED/OVCIVICS
        # = pid|names (semicolon-joined); "?" fields are unread; "-" means
        # no current research. The 13-field economy row is also accepted
        # (M4) but not required for this test.
        lines = ["OVX|2", "TURN|3",
                 "OVROW|0|ROME|150|POTTERY",
                 "OVROW|1|EGYPT|100|-",
                 "OVRESEARCHED|0|POTTERY;IRRIGATION",
                 "OVRESEARCHED|1|",
                 "OVCIVICS|0|CODE_OF_LAWS",
                 "OVCIVICS|1|",
                 "OVERA|ERA_ANCIENT"]
        wire = parse_overview(lines)
        belief = PlannerBelief(player_id=PLAYER)
        belief.observe_overview(_project_overview(PLAYER, wire))
        assert belief.own_player["civ_name"] == "ROME"
        assert belief.own_player["gold"] == 150
        assert belief.own_player["researching"] == "POTTERY"
        # public players includes everyone (alive, civ_name)
        assert 0 in belief.public_players
        assert 1 in belief.public_players
        assert belief.public_players[1]["civ_name"] == "EGYPT"


# -- visible map ------------------------------------------------------------

class TestLiveVisibleMapProjection:
    """Wire TILEROW → parser → visibility → belief.observe_map."""

    def test_visible_tile_carries_owner_id_and_city_id(self):
        # 6-field TILEROW = q|r|terrain|visible|owner|city (visible flag is
        # true/false; owner/city only meaningful when visible=true).
        # The unobserved tile must be in `remembered` for the visibility
        # layer to keep it; otherwise it's dropped (matches the "absent,
        # not masked" contract).
        lines = ["VMAP|4", "TURN|5",
                 "TILEROW|36|20|TERRAIN_GRASS|true|0|c0:7",
                 "TILEROW|10|10|TERRAIN_PLAINS|false|0|-",
                 "---END---"]
        wire = parse_visible_map(lines)
        observable = frozenset({"36,20"})
        remembered = frozenset({"10,10"})
        projected = _project_map(PLAYER, wire, observable=observable,
                                 remembered=remembered)
        belief = PlannerBelief(player_id=PLAYER)
        belief.observe_map(projected)
        # The visible tile carries owner_id (post-projection).
        visible_tile = projected["tiles"]["36,20"]
        assert visible_tile["owner_id"] == 0
        # The "city" key gets translated to "city_id" by visibility.
        assert visible_tile["city_id"] == "c0:7"
        # Remembered tile stays without owner_id.
        remembered_tile = projected["tiles"]["10,10"]
        assert "owner_id" not in remembered_tile


# -- own-entity determinization ---------------------------------------------

class TestOwnEntityDeterminization:
    """The hop `test_own_unit_with_unknown_health_is_observed_lite` does not
    cover: what `build_state_doc` WRITES into the SimState doc for OWN
    entities read off the live wire.

    Accepting an unread native HP (the 2af3804 tolerance) is only half the
    seam — the determinized doc is what `legal_actions` / `run_ambient` /
    `search_option` do arithmetic on, and a None (or an engine-only
    vocabulary token) that survives determinization is a planner crash on
    the seat's next search, not an honest unknown."""

    def test_own_unit_unknown_hp_determinizes_and_ambient_rolls_out(self):
        # Wire-exact own row for a health pcall failure (lua_translator emits
        # hp/maxhp="unknown", healthValid="false"): the belief accepts it and
        # the determinizer must default the rollout hp like the FOREIGN path
        # already does via _hp_from_bucket. Own units sit at full movement at
        # a lease start, so run_ambient's healing guard is the first `None`
        # comparison the rollout hits.
        lines = ["UNITS|1",
                 "UNITROW|u0:65536|0|WARRIOR|36|20|unknown|2|2|20|0|false|false|unknown|false",
                 "---END---"]
        wire = parse_units(lines, qualified=True)
        belief = PlannerBelief(player_id=PLAYER)
        units, _ = _boundary_reencode(_project_units(PLAYER, wire))
        belief.observe_units(asyncio.run(units()), turn=TURN)

        from civ_arena.game.sim.engine import run_ambient
        from civ_arena.game.sim.state import SimState
        from civ_arena.planner.belief import build_state_doc
        state = SimState.from_doc(build_state_doc(belief, seed=24))
        run_ambient(state, PLAYER)  # was: TypeError: '<' not supported ... NoneType/int
        assert [u["hp"] for u in state.units.values()] == [100]

