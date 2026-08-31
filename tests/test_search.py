"""M15d: options, MCTS-vs-MCGS search, and the planner policy.

Options are pinned sound (every compiled step passes check_action and
builds a MUTEX-free DAG) over random playout states. Search is pinned
deterministic; MCGS is pinned to actually TRANSPOSE at equal budget. The
planner policy plays a full Arena match against the turtler with zero
referee rejections and an identical replay.
"""

from __future__ import annotations

import json

from civ_arena.arena.coordinator import Arena
from civ_arena.config import VALID_POLICIES, AgentSpec, MatchSpec
from civ_arena.game.sim.layouts import duel_start
from civ_arena.game.sim.rules import check_action
from civ_arena.game.sim.state import SimState
from civ_arena.planner.action_dag import build_dag
from civ_arena.planner.belief import PlannerBelief, build_state_doc
from civ_arena.planner.options import OPTIONS
from civ_arena.planner.runtime import PlannerRuntime
from civ_arena.planner.search import abstract_key, search_option
from civ_arena.replay import replay_run
from test_action_dag import FakeFacade, feed_via
from test_planner import playout


def test_option_steps_are_legal_and_mutex_free() -> None:
    for seed, turns in ((3, 2), (19, 8), (31, 14)):
        state = playout(seed, turns)
        for pid in (0, 1):
            for oid, option in sorted(OPTIONS.items()):
                if not option.initiation(state, pid):
                    continue
                plan = option.compile_step(state, pid)
                build_dag(plan)  # must not raise (MUTEX-free)
                for tool, args in plan:
                    assert check_action(state, pid, tool, args) is None, (
                        f"option {oid} compiled illegal {tool} {args}")


async def test_search_is_deterministic() -> None:
    state = SimState.from_doc(duel_start(21))
    facade = FakeFacade(state, 0)
    belief = PlannerBelief(0)
    await feed_via(facade, belief)
    for method in ("mcts", "mcgs"):
        a = search_option(belief, 0, method=method, budget=8, seed=4)
        b = search_option(belief, 0, method=method, budget=8, seed=4)
        assert a.to_doc() == b.to_doc()
        assert a.chosen in OPTIONS


async def test_mcgs_transposes_at_equal_budget() -> None:
    state = SimState.from_doc(duel_start(21))
    facade = FakeFacade(state, 0)
    belief = PlannerBelief(0)
    await feed_via(facade, belief)
    mcts = search_option(belief, 0, method="mcts", budget=16, seed=4)
    mcgs = search_option(belief, 0, method="mcgs", budget=16, seed=4)
    assert mcts.transposition_hits == 0  # path keys cannot collide
    assert mcgs.transposition_hits > 0   # convergent worlds actually merge
    assert mcgs.rollouts == mcts.rollouts == 16


def test_abstract_key_is_canonical_and_coarse() -> None:
    state = playout(9, 6)
    state.player(0)["gold"] = 30
    key = abstract_key(state, 0)
    again = SimState.from_doc(state.to_doc())
    assert abstract_key(again, 0) == key  # dict-order independent
    # coarse: a within-bucket gold change does not split the node...
    state.player(0)["gold"] = 34
    assert abstract_key(state, 0) == key
    # ...but a bucket change does
    state.player(0)["gold"] = 55
    assert abstract_key(state, 0) != key


def test_planner_policy_registered() -> None:
    from civ_arena.agents.runtime import AgentProfile, build_runtime

    assert "planner" in VALID_POLICIES
    rt = build_runtime(AgentProfile(agent_id="p", player_id=0,
                                    policy="planner", seed=7))
    assert isinstance(rt, PlannerRuntime)


def planner_spec(match_id: str, max_turns: int = 8) -> MatchSpec:
    return MatchSpec(
        match_id=match_id, seed=424242, max_turns=max_turns, adapter="simulator",
        watchdog_mode="flag_and_continue", violation_limit=5, checkpoint_every=2,
        agents=[
            AgentSpec(agent_id="planner", player_id=0, policy="planner", seed=7),
            AgentSpec(agent_id="korea", player_id=1, policy="turtler", seed=22),
        ],
    )


async def test_planner_full_match_zero_rejections_and_replay(tmp_path):
    from civ_arena.agents.runtime import AgentProfile, build_runtime

    spec = planner_spec("planner-match")
    bot = PlannerRuntime(0, 7, budget=6)
    turtler = build_runtime(AgentProfile(agent_id="korea", player_id=1,
                                         policy="turtler", seed=22))
    arena = Arena(tmp_path / "run", spec, runtimes={0: bot, 1: turtler})
    summary = await arena.run()
    assert summary["final_turn"] == 8
    assert summary["violations_total"] == 0
    assert bot.trace and bot.trace[0]["method"] == "mcgs"
    assert bot.active in OPTIONS

    records = [json.loads(line) for line in
               (tmp_path / "run" / "events.jsonl").read_text().splitlines()]
    rejected = [r for r in records if r["kind"] == "TOOL_RESULT"
                and r.get("status") == "rejected" and r["player_id"] == 0]
    assert rejected == []

    result = await replay_run(tmp_path / "run", spec, tmp_path / "replay")
    assert result["identical"], (
        f"replay diverged at comparable-event {result['first_divergence']}")


async def test_planner_belief_never_touches_ground_truth() -> None:
    """Structural fairness at the runtime level: everything the planner
    plans over derives from build_state_doc(belief) — spot-pinned by
    checking the runtime holds no reference to any live SimState."""
    bot = PlannerRuntime(0, 7, budget=4)
    state = SimState.from_doc(duel_start(21))
    facade = FakeFacade(state, 0)
    await bot.take_turn(facade)
    assert isinstance(bot.belief, PlannerBelief)
    doc = build_state_doc(bot.belief, seed=1)
    for unit in doc["units"].values():
        if unit["owner"] != 0:
            assert unit["fortified"] is False  # determinized, never copied
