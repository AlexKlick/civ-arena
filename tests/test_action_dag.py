"""M15c: the within-turn action DAG + guarded executor.

Pure layer: edge derivation, MUTEX refusal, canonical-order determinism and
the partial-order reduction (commuting permutations collapse to one
sequence). Arena layer: a DAG-driven bot plays full sim matches through the
REAL referee off its M15b belief — the Experiment-1 headline is pinned as
ZERO referee rejections (staleness is absorbed by belief prevalidation,
which the deliberately-stale plan below exercises), with replay identical.
"""

from __future__ import annotations

import json
import random
from typing import Any

from civ_arena.arena.coordinator import Arena
from civ_arena.config import AgentSpec, MatchSpec
from civ_arena.game.sim.rules import legal_actions
from civ_arena.game.sim.state import SimState
from civ_arena.planner.action_dag import build_dag, canonical_order
from civ_arena.planner.belief import PlannerBelief, build_state_doc
from civ_arena.planner.executor import execute_plan
from civ_arena.replay import replay_run

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
        for tool, args in by_tool.get("fortify", []):
            unit = bstate.unit(args["unit_id"])
            if unit and unit["type"] != "SETTLER" and args["unit_id"] not in planned_units:
                plan.append((tool, args))

        report = await execute_plan(facade, bstate, self.player_id, plan)
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
    executed = sum(len(rep.executed) for rep in bot.reports)
    assert executed > 0
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
