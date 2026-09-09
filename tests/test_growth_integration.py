"""Controller/facade execution evidence using projected fixtures; no engine/provider."""

import copy
import json

import pytest

from civ_arena.agents.growth_policy import GrowthControls, GrowthPolicy
from civ_arena.agents.llm.runtime import LLMAgentRuntime
from civ_arena.agents.llm.strategic_controller import StrategicController
from civ_arena.agents.production_policy import choose_production
from civ_arena.agents.runtime import AgentProfile, build_runtime
from civ_arena.agents.settlement_executor import SettlementExecutor
from civ_arena.agents.strategy_directive import validate_directive
from civ_arena.arena.referee import MatchAborted
from civ_arena.config import ConfigError, LLMSpec, parse_config
from fakes import FakeModel, use
from test_growth_policy import BUILDING, city, option, state, unit
from test_strategic_controller import Facade, advance

CATALOG = [
    option("WARRIOR", power=20),
    option("SETTLER", role="settler"),
    option("BUILDER", role="builder"),
    BUILDING,
]


class GrowthFacade(Facade):
    def __init__(self):
        super().__init__()
        self.units = [
            unit("guard", kind="WARRIOR", power=20),
            unit("escort", kind="WARRIOR", power=20),
            unit("settler", kind="SETTLER", power=0),
        ]
        # Stable lexical identity selects the guard first.
        self.units[0]["unit_id"], self.units[1]["unit_id"] = "a_guard", "b_escort"
        self.cities = [city()]
        self.tiles = {
            f"{q},0": {"terrain": "GRASSLAND", "owner_id": -1, "city_id": ""} for q in range(7)
        }
        self.catalog = copy.deepcopy(CATALOG)
        self.move_status = "accepted"
        self.move_changes = True
        self.founding_changes = True

    async def get_visible_map(self):
        return {"tiles": copy.deepcopy(self.tiles)}

    async def get_available_production(self, city_id):
        return copy.deepcopy(self.catalog)

    async def move_unit(self, unit_id, dest, **kwargs):
        self.calls.append(("move_unit", unit_id, dest))
        assert kwargs["idempotency_key"].startswith(("scout-", "settle-"))
        if self.move_status == "accepted" and self.move_changes:
            next(u for u in self.units if u["unit_id"] == unit_id)["coord"] = dest
        return {"status": self.move_status}

    async def fortify(self, unit_id, **kwargs):
        self.calls.append(("fortify", unit_id))
        next(u for u in self.units if u["unit_id"] == unit_id)["fortified"] = True
        return {"status": "accepted"}

    async def found_city(self, unit_id, **kwargs):
        self.calls.append(("found_city", unit_id))
        if self.founding_changes:
            settler = next(u for u in self.units if u["unit_id"] == unit_id)
            self.cities.append(city("new_city", coord=settler["coord"]))
            self.tiles[settler["coord"]].update(owner_id=0, city_id="new_city")
            self.units.remove(settler)
        return {"status": "accepted"}


def setup(*, frozen=False, directive=None):
    model = FakeModel([[use("submit_directive", directive or {"version": 1})]])
    llm = LLMSpec("http://unused", "UNUSED", "fake")
    runtime = LLMAgentRuntime.build(AgentProfile("a", 0, "llm", 4, llm=llm), client=model)
    records = []
    controller = StrategicController(
        "growth-fixture",
        cadence=60,
        audit=records.append,
        growth_autopilot=True,
        opening_units_frozen=frozen,
    )
    return controller, runtime, model, GrowthFacade(), records


def mission_records(records):
    return [row for row in records if row["audit"] == "strategy_settlement"]


async def test_actual_eight_turn_escorted_founding_then_observed_completion():
    ctl, rt, model, facade, records = setup()
    for turn in range(1, 9):
        await advance(ctl, rt, facade, turn)
    missions = mission_records(records)
    assert len(missions) == 8
    assert all(len(row["execution"]) == 1 for row in missions)
    assert len({row["mission"]["mission_id"] for row in missions}) == 1
    assert [row["execution"][0]["args"]["unit_id"] for row in missions] == [
        "settler",
        "b_escort",
        "settler",
        "b_escort",
        "settler",
        "b_escort",
        "settler",
        "settler",
    ]
    assert missions[-1]["outcome"] == "accepted_founder_consumed_new_owned_city_observed"
    assert missions[-1]["mission"]["completion_basis"].startswith("accepted_found_city_then")
    assert ctl._growth.mission is None  # only this observed causal completion can retire
    assert len(facade.cities) == 2 and facade.calls.count("end_turn") == 8
    assert next(u for u in facade.units if u["unit_id"] == "a_guard")["coord"] == "0,0"
    assert model.posts_sent == 1  # quiet mission steps need no strategy HTTP
    metadata = json.loads(model.requests[0]["messages"][0]["content"].split("\n")[0])
    assert metadata["growth"]["mission"]["status"] == "proposal_ready"
    assert metadata["growth"]["observed_capabilities"]
    assert "growth_role_reserved" in {
        d["reason"]
        for row in records
        if row["audit"] == "strategy_graph"
        for d in row["graph"]["decisions"]
    }


@pytest.mark.parametrize("actor", ["settler", "b_escort", "a_guard"])
async def test_health_overlay_pauses_and_resumes_same_mission(actor):
    ctl, rt, _, facade, records = setup()
    await advance(ctl, rt, facade, 1)
    mission = ctl._growth.mission["mission_id"]
    target = next(u for u in facade.units if u["unit_id"] == actor)
    target["hp"] = 35
    await advance(ctl, rt, facade, 2)
    assert not mission_records(records)[-1]["execution"]
    assert ctl._growth.mission["mission_id"] == mission
    target["hp"] = 80
    await advance(ctl, rt, facade, 3)
    assert not mission_records(records)[-1]["execution"]
    target["hp"] = 90
    await advance(ctl, rt, facade, 4)
    assert len(mission_records(records)[-1]["execution"]) == 1
    assert ctl._growth.mission["mission_id"] == mission


async def test_model_tactical_hold_is_separate_and_blocks_automatic_mission():
    ctl, rt, _, facade, records = setup(
        directive={"tactical_overrides": [{"unit_id": "settler", "action": "hold"}]}
    )
    await advance(ctl, rt, facade, 1)
    assert not mission_records(records)[-1]["execution"]
    assert mission_records(records)[-1]["outcome"] == "explicit_model_tactics_priority"
    await advance(ctl, rt, facade, 2)
    assert mission_records(records)[-1]["execution"][0]["args"]["unit_id"] == "settler"
    assert ctl.directive["tactical_overrides"] == []


@pytest.mark.parametrize("frozen,expected", [(False, 0), (True, 1)])
async def test_zero_mp_requires_trusted_untouched_frozen_roster(frozen, expected):
    ctl, rt, _, facade, records = setup(frozen=frozen)
    for row in facade.units:
        row["movement"] = 0
    await advance(ctl, rt, facade, 1)
    assert len(mission_records(records)[-1]["execution"]) == expected
    if expected:
        assert mission_records(records)[-1]["execution"][0]["movement_authority"].startswith(
            "untouched"
        )


@pytest.mark.parametrize("status", ["accepted", "rejected"])
async def test_unconfirmed_or_rejected_action_not_repeated_and_review_bounded(status):
    ctl, rt, model, facade, records = setup()
    facade.move_status, facade.move_changes = status, False
    for turn in range(1, 6):
        await advance(ctl, rt, facade, turn)
    assert sum(len(row["execution"]) for row in mission_records(records)) == 1
    assert model.posts_sent == 2  # one initial + one unresolved-mission review
    assert ctl._settlement.pending["status"] == status
    assert not facade.calls.count(("found_city", "settler"))


async def test_delayed_move_confirmation_allows_next_observed_step():
    ctl, rt, _, facade, records = setup()
    facade.move_changes = False
    await advance(ctl, rt, facade, 1)
    next(u for u in facade.units if u["unit_id"] == "settler")["coord"] = "1,0"
    facade.move_changes = True
    await advance(ctl, rt, facade, 2)
    assert mission_records(records)[-1]["execution"][0]["args"]["unit_id"] == "b_escort"


@pytest.mark.parametrize("change", ["unknown", "mountain", "foreign", "owner_unknown"])
async def test_route_holds_from_current_observation(change):
    ctl, rt, _, facade, records = setup()
    await advance(ctl, rt, facade, 1)
    if change == "unknown":
        del facade.tiles["2,0"]
    elif change == "mountain":
        facade.tiles["2,0"]["terrain"] = "MOUNTAIN"
    elif change == "owner_unknown":
        del facade.tiles["2,0"]["owner_id"]
    else:
        facade.units.append(unit("other", owner=1, coord="3,0", is_barbarian=False))
    await advance(ctl, rt, facade, 2)
    assert not mission_records(records)[-1]["execution"]
    assert ctl._growth.assessment["mode"] == "grow"


async def test_confirmed_barbarian_pauses_then_three_clear_turns_restore():
    ctl, rt, _, facade, records = setup()
    await advance(ctl, rt, facade, 1)
    facade.units.append(unit("barb", owner=63, coord="3,0", power=20, is_barbarian=True))
    await advance(ctl, rt, facade, 2)
    assert not mission_records(records)[-1]["execution"]
    assert ctl._growth.assessment["mode"] == "defend"
    facade.units = [u for u in facade.units if u["unit_id"] != "barb"]
    for turn in (3, 4, 5):
        await advance(ctl, rt, facade, turn)
        assert bool(mission_records(records)[-1]["execution"]) == (turn == 5)


async def test_ambiguous_founder_cannot_count_existing_or_unconsumed_city():
    ctl, rt, _, facade, records = setup()
    facade.founding_changes = False
    for turn in range(1, 9):
        await advance(ctl, rt, facade, turn)
    assert ctl._settlement.pending["action"] == "found_city"
    facade.cities.append(city("new_city", coord="4,0"))
    await advance(ctl, rt, facade, 9)
    assert ctl._growth.mission["completion_basis"] == "observed_owned_city_not_causal_receipt"
    assert ctl._settlement.pending  # owned settler still exists
    assert len([c for c in facade.calls if isinstance(c, tuple) and c[0] == "found_city"]) == 1


async def test_no_catalog_means_no_invented_capability_or_automatic_mission():
    ctl, rt, _, facade, records = setup()
    facade.cities[0]["production_queue"] = "GRANARY"
    await advance(ctl, rt, facade, 1)
    assert not mission_records(records)[-1]["execution"]
    assert ctl._growth.summary()["observed_capabilities"] == []


async def test_active_queues_unchanged_and_no_evergreen_army_for_unhealthy_guard():
    ctl, rt, _, facade, records = setup()
    await advance(ctl, rt, facade, 1)
    old_queue = facade.cities[0]["production_queue"]
    facade.units[0]["hp"] = None
    facade.units[0]["health_valid"] = False
    await advance(ctl, rt, facade, 2)
    assert facade.cities[0]["production_queue"] == old_queue
    assert not mission_records(records)[-1]["execution"]


async def test_failure_to_close_poisoned_controller_does_not_commit_growth_turn():
    ctl, rt, _, facade, _ = setup()
    facade.closures = [{"status": "rejected", "rejection": "denied"}]
    with pytest.raises(MatchAborted):
        await advance(ctl, rt, facade, 1)
    assert ctl._growth._completed == 0
    with pytest.raises(MatchAborted, match="previously failed"):
        await advance(ctl, rt, facade, 2)


def test_escort_production_gap_is_one_slot_and_counts_queues_and_damaged_inventory():
    s = state([unit(kind="WARRIOR", power=20)])
    g = GrowthPolicy(0, mission_execution=True)
    g.begin_turn(s, turn=1, catalogs={"c0:1": CATALOG})
    directive = validate_directive({}, player_id=0, owned_unit_ids={"u0:1"})

    def choose(snapshot):
        return choose_production(
            snapshot,
            player_id=0,
            city_id="c0:1",
            options=CATALOG,
            directive=directive,
            growth_policy=g,
        )

    assert choose(s)["item_id"] == "WARRIOR"
    s["get_cities"].append(city("c0:2", coord="0,1", queue=["WARRIOR", "WARRIOR"]))
    assert choose(s)["item_id"] == "BUILDER"
    s["get_cities"].pop()
    s["get_units"].append(unit("damaged", kind="WARRIOR", hp=35, power=20))
    assert choose(s)["item_id"] == "BUILDER"


def test_mixed_armed_capability_capacity_still_blocks_all_armed_hybrids():
    legion = option("ROMAN_LEGION", power=40)
    legion["unit_capabilities"]["build_charges"] = 1
    boat = option("GALLEY", power=30, domain="DOMAIN_SEA")
    s = state(
        [unit(kind="WARRIOR", power=20)],
        cities=[city(), city("c0:2", coord="0,1", queue=["GALLEY"])],
    )
    g = GrowthPolicy(0, mission_execution=True, controls=GrowthControls(military_cap=2))
    g.begin_turn(s, turn=1, catalogs={"c0:1": CATALOG + [legion, boat]})
    result = choose_production(
        s,
        player_id=0,
        city_id="c0:1",
        options=[legion, BUILDING],
        directive=validate_directive({}, player_id=0, owned_unit_ids={"u0:1"}),
        growth_policy=g,
    )
    assert result["item_id"] == "GRANARY"
    assert result["growth"]["military_capacity_slots"] == 2


def config_doc(value=False, mode="strategic_autopilot"):
    return {
        "match": {"match_id": "growth-test", "seed": 1, "max_turns": 2},
        "agents": [
            {
                "agent_id": f"a{p}",
                "player_id": p,
                "policy": "llm",
                "decision_mode": mode,
                "growth_autopilot": value,
                "llm": {"base_url": "http://unused", "api_key_env": "UNUSED", "model_id": "fake"},
            }
            for p in (0, 1)
        ],
    }


@pytest.mark.parametrize("value", [None, "true", 1, []])
def test_growth_config_requires_literal_boolean(value):
    with pytest.raises(ConfigError, match="explicit boolean"):
        parse_config(config_doc(value))


def test_growth_default_false_and_factory_forward(monkeypatch):
    doc = config_doc()
    for agent in doc["agents"]:
        del agent["growth_autopilot"]
    assert all(not a.growth_autopilot for a in parse_config(doc).agents)
    spec = parse_config(config_doc(True))
    from civ_arena.game.civ6.live_driver import _agent_profile

    profile = _agent_profile(spec.agents[0])
    assert profile.growth_autopilot
    monkeypatch.setattr(
        LLMAgentRuntime,
        "build",
        lambda *a, **kw: LLMAgentRuntime(profile, profile.llm, FakeModel([[use("end_turn")]])),
    )
    runtime = build_runtime(profile, match_id="growth-test")
    assert runtime.strategic_controller.growth_autopilot
    with pytest.raises(ConfigError, match="requires strategic"):
        parse_config(config_doc(True, "legacy"))


@pytest.mark.parametrize("value,mode", [("true", "strategic_autopilot"), (True, "legacy")])
def test_runtime_refuses_bypassed_invalid_config(value, mode):
    with pytest.raises(ValueError, match="growth_autopilot"):
        build_runtime(AgentProfile("a", 0, "llm", 1, growth_autopilot=value, decision_mode=mode))


def test_executor_refuses_foreign_player_binding():
    with pytest.raises(ValueError, match="player mismatch"):
        SettlementExecutor(1).observe(GrowthPolicy(0), state(), 1)


async def test_explicit_found_after_ambiguous_auto_found_does_not_claim_its_causality():
    ctl, rt, model, facade, records = setup()
    facade.founding_changes = False
    for turn in range(1, 9):
        await advance(ctl, rt, facade, turn)
    facade.founding_changes = True
    model.script = [
        [
            use(
                "submit_directive",
                {"tactical_overrides": [{"unit_id": "settler", "action": "found_city"}]},
            )
        ]
    ]
    await advance(ctl, rt, facade, 9, tactical_requested=True)
    assert mission_records(records)[-1]["observation"]["outcome"] == (
        "intervening_actor_action_correlation_unavailable"
    )
    assert ctl._growth.mission["completion_basis"] == "observed_owned_city_not_causal_receipt"


def test_optional_scouting_inventory_remains_bounded_when_defense_satisfied():
    s = state([unit(kind="WARRIOR", power=20), unit("escort", kind="WARRIOR", power=20)])
    scout = option("SCOUT", power=10)
    g = GrowthPolicy(0, mission_execution=True)
    g.begin_turn(s, turn=1, catalogs={"c0:1": CATALOG + [scout]})
    directive = validate_directive(
        {"production_preferences": ["SCOUT"]}, player_id=0, owned_unit_ids={"u0:1", "escort"}
    )

    def choose():
        return choose_production(
            s,
            player_id=0,
            city_id="c0:1",
            options=[scout, BUILDING],
            directive=directive,
            growth_policy=g,
        )

    assert choose()["item_id"] == "SCOUT"
    s["get_units"].extend(unit(f"scout{i}", kind="SCOUT", power=10) for i in range(2))
    assert choose()["item_id"] == "GRANARY"
    directive["unit_targets"] = {"SCOUT": 0}
    assert choose()["item_id"] == "GRANARY"


async def test_queued_settler_after_twelve_turns_keeps_site_and_can_execute():
    ctl, rt, _, facade, records = setup()
    facade.units = [u for u in facade.units if u["unit_id"] != "settler"]
    for turn in range(1, 15):
        await advance(ctl, rt, facade, turn)
    assert facade.cities[0]["production_queue"] == "SETTLER"
    mission_id = ctl._growth.mission["mission_id"]
    assert ctl._growth.mission["status"] == "awaiting_settler"
    assert "travel_started_turn" not in ctl._growth.mission
    facade.units.append(unit("settler", kind="SETTLER", power=0))
    facade.cities[0]["production_queue"] = []
    await advance(ctl, rt, facade, 15)
    assert mission_records(records)[-1]["execution"]
    assert ctl._growth.mission["mission_id"] == mission_id
    assert ctl._growth.mission["last_confirmed_progress_turn"] == 15


async def test_long_escorted_route_uses_observed_progress_not_mission_age():
    ctl, rt, _, facade, records = setup()
    # Known distant city excludes all nearer settlement sites while leaving a
    # passable projected corridor. With spacing 8 the nearest valid site is 8,0.
    ctl._growth = GrowthPolicy(
        0, controls=GrowthControls(min_city_spacing=8), mission_execution=True
    )
    ctl._settlement = SettlementExecutor(0)
    facade.tiles = {
        f"{q},0": {"terrain": "GRASSLAND", "owner_id": -1, "city_id": ""} for q in range(11)
    }
    for turn in range(1, 17):
        await advance(ctl, rt, facade, turn)
    assert len(mission_records(records)) == 16
    assert all(row["execution"] for row in mission_records(records))
    assert (
        mission_records(records)[-1]["outcome"]
        == "accepted_founder_consumed_new_owned_city_observed"
    )
    assert ctl._growth.mission is None


async def test_true_travel_stall_expires_without_acceptance_reset():
    ctl, rt, _, facade, records = setup()
    facade.move_changes = False
    for turn in range(1, 14):
        await advance(ctl, rt, facade, turn)
    assert ctl._growth.mission["status"] == "expired"
    assert ctl._growth.mission["expiry_basis"] == "no_confirmed_travel_progress"
    assert sum(len(row["execution"]) for row in mission_records(records)) == 1


def test_unproductive_wait_requests_review_without_killing_mission():
    s = state([unit(kind="WARRIOR"), unit("escort", kind="WARRIOR")])
    g = GrowthPolicy(
        0, controls=GrowthControls(production_wait_review_turns=3), mission_execution=True
    )
    for turn in range(1, 5):
        g.begin_turn(s, turn=turn, catalogs={"c0:1": CATALOG})
        g.refresh(s)
        if turn < 4:
            assert g.mission["status"] == "awaiting_settler"
        g.complete_turn(turn)
    assert g.mission["status"] == "awaiting_settler"
    assert g.mission["production_wait_review_due"]
    assert "last_confirmed_progress_turn" not in g.mission


def test_productive_settler_queue_does_not_expire_or_request_redundant_review():
    s = state([unit(kind="WARRIOR"), unit("escort", kind="WARRIOR")])
    g = GrowthPolicy(
        0, controls=GrowthControls(production_wait_review_turns=3), mission_execution=True
    )
    g.begin_turn(s, turn=1, catalogs={"c0:1": CATALOG})
    g.complete_turn(1)
    s["get_cities"][0]["production_queue"] = ["SETTLER"]
    for turn in range(2, 70):
        g.begin_turn(s, turn=turn)
        assert g.mission["status"] == "awaiting_settler"
        assert g.mission["observed_settler_queued"]
        assert not g.mission["production_wait_review_due"]
        g.complete_turn(turn)
