"""M15c: the within-turn action DAG + guarded executor.

Pure layer: edge derivation, MUTEX refusal, canonical-order determinism and
the partial-order reduction (commuting permutations collapse to one
sequence). Arena layer: a DAG-driven bot plays full sim matches through the
REAL referee off its M15b belief — the Experiment-1 headline is pinned as
ZERO referee rejections (staleness is absorbed by belief prevalidation,
which the deliberately-stale plan below exercises), with replay identical.
"""

from __future__ import annotations

import copy
import json
import random
from typing import Any

from civ_arena.arena.coordinator import Arena
from civ_arena.arena.visibility import VisibilityPolicy
from civ_arena.config import AgentSpec, MatchSpec
from civ_arena.game.sim.layouts import duel_start
from civ_arena.game.sim.rules import apply_action, check_action, legal_actions
from civ_arena.game.sim.state import SimState
from civ_arena.game.sim.visibility import ground_truth
from civ_arena.planner.action_dag import build_dag, canonical_order
from civ_arena.planner.belief import PlannerBelief, build_state_doc
from civ_arena.planner.executor import execute_plan
from civ_arena.replay import replay_run

ACTION_TOOLS = {"move_unit", "attack", "fortify", "found_city",
                "set_research", "set_city_production", "purchase"}


class FakeFacade:
    """A truth-holding facade: actions run the real rules on TRUE state,
    observations return the real projections — referee behavior without
    the arena machinery, so executor scenarios can be crafted exactly."""

    def __init__(self, state: SimState, pid: int) -> None:
        self._state = state
        self._pid = pid
        self._policy = VisibilityPolicy()
        self.calls: list[tuple[str, dict]] = []
        self.rejections: list[tuple[str, dict, str]] = []

    def _vis(self):
        v = ground_truth(self._state, self._pid)
        return v.observable, v.remembered

    async def get_overview(self):
        obs, rem = self._vis()
        doc = {"turn": self._state.turn,
               "phase_player": self._state.phase_player,
               "players": copy.deepcopy(self._state.players),
               "cities": copy.deepcopy(self._state.cities),
               "units": copy.deepcopy(self._state.units),
               "tiles": copy.deepcopy(self._state.tiles)}
        return self._policy.project(doc, "overview", self._pid, obs, rem)

    async def get_units(self):
        obs, rem = self._vis()
        docs = sorted((copy.deepcopy(u) for u in self._state.units.values()),
                      key=lambda u: int(u["unit_id"][1:]))
        return self._policy.project(docs, "units", self._pid, obs, rem)

    async def get_cities(self):
        obs, rem = self._vis()
        docs = sorted((copy.deepcopy(c) for c in self._state.cities.values()),
                      key=lambda c: int(c["city_id"][1:]))
        return self._policy.project(docs, "cities", self._pid, obs, rem)

    async def get_visible_map(self):
        obs, rem = self._vis()
        return self._policy.project(
            {"turn": self._state.turn, "tiles": copy.deepcopy(self._state.tiles)},
            "visible_map", self._pid, obs, rem)

    def __getattr__(self, tool: str):
        if tool == "end_turn":
            async def end_turn():
                return {"status": "accepted", "turn": self._state.turn}
            return end_turn
        if tool not in ACTION_TOOLS:
            raise AttributeError(tool)

        async def call(**args):
            self.calls.append((tool, dict(args)))
            reason = check_action(self._state, self._pid, tool, args)
            if reason is not None:
                self.rejections.append((tool, dict(args), reason.value))
                return {"status": "rejected", "rejection": reason.value}
            apply_action(self._state, self._pid, tool, args)
            return {"status": "accepted"}

        return call


async def feed_via(facade: FakeFacade, belief: PlannerBelief) -> None:
    overview = await facade.get_overview()
    belief.observe_overview(overview)
    belief.observe_units(await facade.get_units(), overview["turn"])
    belief.observe_cities(await facade.get_cities(), overview["turn"])
    belief.observe_map(await facade.get_visible_map())

# ------------------------------------------------------------- pure layer


def test_same_unit_actions_are_sequenced_in_author_order() -> None:
    plan = [("move_unit", {"unit_id": "u3", "dest": "1,1"}),
            ("attack", {"unit_id": "u3", "target_id": "u9"}),
            ("fortify", {"unit_id": "u4"})]
    dag = build_dag(plan)
    assert dag.preceders[1] == {0}
    assert 2 not in dag.preceders  # different unit commutes


def test_purchases_sequence_and_mutex_plans_refused() -> None:
    dag = build_dag([("purchase", {"city_id": "c1", "item_id": "WARRIOR"}),
                     ("purchase", {"city_id": "c2", "item_id": "MONUMENT"})])
    assert dag.preceders[1] == {0}
    for bad in (
        [("set_research", {"tech_id": "POTTERY"}),
         ("set_research", {"tech_id": "MINING"})],
        [("set_city_production", {"city_id": "c1", "item_id": "WARRIOR"}),
         ("set_city_production", {"city_id": "c1", "item_id": "WALLS"})],
    ):
        try:
            build_dag(bad)
            raise AssertionError(f"MUTEX plan accepted: {bad}")
        except ValueError:
            pass
    # the same production pair on DIFFERENT cities commutes
    dag = build_dag([("set_city_production", {"city_id": "c1", "item_id": "WARRIOR"}),
                     ("set_city_production", {"city_id": "c2", "item_id": "WALLS"})])
    assert dag.preceders == {}


def test_canonical_order_is_a_partial_order_reduction() -> None:
    commuting = [("fortify", {"unit_id": "u2"}),
                 ("set_research", {"tech_id": "POTTERY"}),
                 ("set_city_production", {"city_id": "c1", "item_id": "WARRIOR"}),
                 ("fortify", {"unit_id": "u1"})]
    rng = random.Random(7)
    baseline = None
    for _ in range(6):
        shuffled = list(commuting)
        rng.shuffle(shuffled)
        dag = build_dag(shuffled)
        ordered = [dag.actions[i] for i in canonical_order(dag)]
        if baseline is None:
            baseline = ordered
        assert ordered == baseline  # permutations collapse to ONE sequence
    assert baseline[0][0] == "set_research"  # the tool ladder leads
    # same-unit sequences keep author order even against the ladder
    seq = [("move_unit", {"unit_id": "u1", "dest": "1,1"}),
           ("attack", {"unit_id": "u1", "target_id": "u9"})]
    dag = build_dag(seq)
    assert [dag.actions[i] for i in canonical_order(dag)] == seq


# ---------------------------------------------------- executor truth-divergence


async def test_double_attack_on_killed_target_absorbed() -> None:
    """P1 regression: truth kills the target on the first strike while the
    bucket-reconstructed belief thinks it survives — reconciliation must
    retire it so the second attack never reaches the referee."""
    state = SimState.from_doc(duel_start(21))
    own = [u for u in state.units.values()
           if u["owner"] == 0 and u["strength"] > 0]
    w1, w2 = own[0], own[1]
    w2["q"], w2["r"] = w1["q"] + 1, w1["r"] - 1
    _, target = state.spawn_unit(1, "SETTLER", w1["q"] + 1, w1["r"])
    target["hp"] = 40  # bucket 1 -> belief reconstructs 37; str-0 dies to any hit
    facade = FakeFacade(state, 0)
    belief = PlannerBelief(0)
    await feed_via(facade, belief)
    plan = [("attack", {"unit_id": w1["unit_id"], "target_id": target["unit_id"]}),
            ("attack", {"unit_id": w2["unit_id"], "target_id": target["unit_id"]})]
    report = await execute_plan(facade, belief, 0, plan, seed=3)
    assert facade.rejections == []
    assert [a[0] for a in report.executed] == ["attack"]
    assert any("prevalidation" in s[2] for s in report.skipped)
    assert target["unit_id"] not in belief.foreign_units  # retired, in-lease proof


async def test_purchase_reconciles_true_spawned_id() -> None:
    """P1 regression: hidden rival units consumed unit ids, so the belief
    would mispredict the purchased id — reconciliation adopts the TRUE id
    and no phantom unit survives."""
    state = SimState.from_doc(duel_start(21))
    settler = next(u for u in state.units.values()
                   if u["owner"] == 0 and u["type"] == "SETTLER")
    assert check_action(state, 0, "found_city",
                        {"unit_id": settler["unit_id"]}) is None
    apply_action(state, 0, "found_city", {"unit_id": settler["unit_id"]})
    city_id = next(iter(state.cities))
    state.spawn_unit(1, "WARRIOR", 4, -2)  # hidden id consumers
    state.spawn_unit(1, "WARRIOR", 4, -1)
    state.player(0)["gold"] = 200
    truth_next = state.doc["next_unit_id"]
    facade = FakeFacade(state, 0)
    belief = PlannerBelief(0)
    await feed_via(facade, belief)
    report = await execute_plan(
        facade, belief, 0,
        [("purchase", {"city_id": city_id, "item_id": "WARRIOR"})], seed=3)
    assert facade.rejections == [] and len(report.executed) == 1
    true_id = f"u{truth_next}"
    assert true_id in belief.own_units          # the real spawned unit adopted
    phantom = f"u{truth_next - 2}"
    assert phantom not in belief.own_units      # no belief-predicted phantom
    assert belief.own_player["gold"] == 200 - 80  # gold reconciled too


async def test_purchase_chain_skip_does_not_kill_affordable_tail() -> None:
    """P2 regression: order edges sequence purchases but a skipped one must
    not cascade — the still-affordable tail executes."""
    state = SimState.from_doc(duel_start(21))
    settler = next(u for u in state.units.values()
                   if u["owner"] == 0 and u["type"] == "SETTLER")
    apply_action(state, 0, "found_city", {"unit_id": settler["unit_id"]})
    city_id = next(iter(state.cities))
    state.player(0)["gold"] = 200
    facade = FakeFacade(state, 0)
    belief = PlannerBelief(0)
    await feed_via(facade, belief)
    plan = [("purchase", {"city_id": city_id, "item_id": "SCOUT"}),      # 50
            ("purchase", {"city_id": city_id, "item_id": "SETTLER"}),    # 160
            ("purchase", {"city_id": city_id, "item_id": "WARRIOR"})]    # 80
    report = await execute_plan(facade, belief, 0, plan, seed=3)
    assert facade.rejections == []
    assert [a[1]["item_id"] for a in report.executed] == ["SCOUT", "WARRIOR"]
    assert len(report.skipped) == 1
    assert report.skipped[0][1]["item_id"] == "SETTLER"
    assert "insufficient_gold" in report.skipped[0][2]


async def test_found_found_keeps_author_order() -> None:
    """P1 regression: two foundings interact through territory claims and
    city-id assignment — author order binds even against the id tie-break."""
    state = SimState.from_doc(duel_start(21))
    settlers = [u for u in state.units.values()
                if u["owner"] == 0 and u["type"] == "SETTLER"]
    s_lo, s_hi = sorted(settlers, key=lambda u: int(u["unit_id"][1:]))
    s_hi["q"], s_hi["r"] = s_lo["q"] + 3, s_lo["r"]
    for dq in (1, 2, 3):
        state.tiles[f"{s_lo['q'] + dq},{s_lo['r']}"]["terrain"] = "GRASSLAND"
    facade = FakeFacade(state, 0)
    belief = PlannerBelief(0)
    await feed_via(facade, belief)
    plan = [("found_city", {"unit_id": s_hi["unit_id"]}),   # author: high id first
            ("found_city", {"unit_id": s_lo["unit_id"]})]
    report = await execute_plan(facade, belief, 0, plan, seed=3)
    assert facade.rejections == []
    assert [c[1]["unit_id"] for c in facade.calls if c[0] == "found_city"] == [
        s_hi["unit_id"], s_lo["unit_id"]]
    assert len(report.executed) == 2


async def test_same_city_purchase_before_production_absorbs_already() -> None:
    """P2 regression: author order purchase-then-produce of one building is
    preserved (no ladder reversal), so the production is absorbed ALREADY."""
    state = SimState.from_doc(duel_start(21))
    settler = next(u for u in state.units.values()
                   if u["owner"] == 0 and u["type"] == "SETTLER")
    apply_action(state, 0, "found_city", {"unit_id": settler["unit_id"]})
    city_id = next(iter(state.cities))
    state.player(0)["gold"] = 200
    facade = FakeFacade(state, 0)
    belief = PlannerBelief(0)
    await feed_via(facade, belief)
    plan = [("purchase", {"city_id": city_id, "item_id": "MONUMENT"}),
            ("set_city_production", {"city_id": city_id, "item_id": "MONUMENT"})]
    report = await execute_plan(facade, belief, 0, plan, seed=3)
    assert facade.rejections == []
    assert [a[0] for a in report.executed] == ["purchase"]
    assert report.skipped and "already" in report.skipped[0][2]


# ------------------------------------------------------------- arena layer


class DagBot:
    """A planner-shaped runtime: observe -> belief -> plan -> execute.

    The plan deliberately includes a move for the settler that just
    founded (stale by construction): belief prevalidation must absorb it
    with zero referee spend.
    """

    def __init__(self, player_id: int) -> None:
        self.player_id = player_id
        self.rng = random.Random(1000 + player_id)
        self.belief = PlannerBelief(player_id)
        self.reports: list[Any] = []

    async def take_turn(self, facade: Any) -> None:
        overview = await facade.get_overview()
        self.belief.observe_overview(overview)
        turn = overview["turn"]
        self.belief.observe_units(await facade.get_units(), turn)
        self.belief.observe_cities(await facade.get_cities(), turn)
        self.belief.observe_map(await facade.get_visible_map())

        bstate = SimState.from_doc(build_state_doc(self.belief, seed=turn))
        acts = legal_actions(bstate, self.player_id)
        by_tool: dict[str, list[tuple[str, dict]]] = {}
        for tool, args in acts:
            by_tool.setdefault(tool, []).append((tool, args))

        plan: list[tuple[str, dict]] = []
        planned_units: set[str] = set()
        if by_tool.get("found_city"):
            found = by_tool["found_city"][0]
            plan.append(found)
            planned_units.add(found[1]["unit_id"])
            # deliberately stale: the settler is consumed by the founding
            moves = [a for a in by_tool.get("move_unit", [])
                     if a[1]["unit_id"] == found[1]["unit_id"]]
            if moves:
                plan.append(moves[0])
        if (bstate.player(self.player_id)["researching"] == ""
                and by_tool.get("set_research")):
            plan.append(by_tool["set_research"][0])
        idle = {c["city_id"] for c in bstate.cities.values()
                if c["owner"] == self.player_id and not c["production_queue"]}
        seen_cities: set[str] = set()
        for tool, args in by_tool.get("set_city_production", []):
            if args["city_id"] in idle and args["city_id"] not in seen_cities:
                plan.append((tool, args))
                seen_cities.add(args["city_id"])
        gold = bstate.player(self.player_id)["gold"]
        if gold >= 100:
            buys = [a for a in by_tool.get("purchase", [])
                    if a[1]["item_id"] == "SCOUT"]
            if buys:
                plan.append(buys[0])
        scouts = [a for a in by_tool.get("move_unit", [])
                  if (u := bstate.unit(a[1]["unit_id"])) and u["type"] == "SCOUT"
                  and a[1]["dest"] != f"{u['q']},{u['r']}"]
        if scouts:
            plan.append(scouts[0])
            planned_units.add(scouts[0][1]["unit_id"])
        for tool, args in by_tool.get("fortify", []):
            unit = bstate.unit(args["unit_id"])
            if unit and unit["type"] not in ("SETTLER", "SCOUT") \
                    and args["unit_id"] not in planned_units:
                plan.append((tool, args))

        report = await execute_plan(facade, self.belief, self.player_id, plan,
                                    seed=turn)
        self.reports.append(report)
        await facade.end_turn()


def dag_spec(match_id: str, max_turns: int = 8) -> MatchSpec:
    return MatchSpec(
        match_id=match_id, seed=424242, max_turns=max_turns, adapter="simulator",
        watchdog_mode="flag_and_continue", violation_limit=5, checkpoint_every=2,
        agents=[
            AgentSpec(agent_id="dag-bot", player_id=0, policy="expansionist", seed=11),
            AgentSpec(agent_id="korea", player_id=1, policy="turtler", seed=22),
        ],
    )


async def test_dag_bot_full_match_zero_rejections(tmp_path):
    from civ_arena.agents.runtime import AgentProfile, build_runtime

    spec = dag_spec("dag-match")
    bot = DagBot(0)
    turtler = build_runtime(AgentProfile(agent_id="korea", player_id=1,
                                         policy="turtler", seed=22))
    arena = Arena(tmp_path / "run", spec, runtimes={0: bot, 1: turtler})
    summary = await arena.run()
    assert summary["final_turn"] == 8
    assert summary["violations_total"] == 0

    records = [json.loads(line) for line in
               (tmp_path / "run" / "events.jsonl").read_text().splitlines()]
    rejected = [r for r in records if r["kind"] == "TOOL_RESULT"
                and r.get("status") == "rejected" and r["player_id"] == 0]
    assert rejected == []  # the Experiment-1 headline: zero referee spend on staleness
    executed = [a for rep in bot.reports for a in rep.executed]
    assert executed
    # power: the match exercised the divergence-risk families too
    tools_used = {a[0] for a in executed}
    assert "purchase" in tools_used, tools_used
    assert "move_unit" in tools_used, tools_used
    # the deliberately-stale settler move was absorbed by prevalidation
    absorbed = [s for rep in bot.reports for s in rep.skipped
                if s[0] == "move_unit" and "prevalidation" in s[2]]
    assert absorbed, "the stale plan entry never exercised prevalidation"


async def test_dag_bot_match_replays_identical(tmp_path):
    from civ_arena.agents.runtime import AgentProfile, build_runtime

    spec = dag_spec("dag-replay", max_turns=6)
    bot = DagBot(0)
    turtler = build_runtime(AgentProfile(agent_id="korea", player_id=1,
                                         policy="turtler", seed=22))
    arena = Arena(tmp_path / "run", spec, runtimes={0: bot, 1: turtler})
    await arena.run()
    result = await replay_run(tmp_path / "run", spec, tmp_path / "replay")
    assert result["identical"], (
        f"replay diverged at comparable-event {result['first_divergence']}")
