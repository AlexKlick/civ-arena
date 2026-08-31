"""M16b: the LLM proposer — untrusted ranking over the option library.

Pinned: compilation is fail-soft and LEGALITY-NARROWING (unknown ids,
non-initiated options, duplicates, and garbage all drop out — the
proposer can never widen the search's choice set); the prior orders
first visits (with a sub-candidate budget the proposal literally chooses
what gets explored); a full match with a scripted proposer model plays
clean with zero rejections and replays identically; a dead proposer
(ModelUnavailable) degrades to unprimed search without touching the
match.
"""

from __future__ import annotations

import json

from civ_arena.agents.runtime import AgentProfile, build_runtime
from civ_arena.arena.coordinator import Arena
from civ_arena.config import AgentSpec, MatchSpec
from civ_arena.game.sim.layouts import duel_start
from civ_arena.game.sim.state import SimState
from civ_arena.planner.belief import PlannerBelief, build_state_doc
from civ_arena.planner.options import OPTIONS
from civ_arena.planner.proposer import (
    build_request,
    compile_proposal,
    render_menu,
    render_view,
)
from civ_arena.planner.runtime import PlannerRuntime
from civ_arena.planner.search import search_option
from civ_arena.replay import replay_run
from test_action_dag import FakeFacade, feed_via
from test_planner import playout


def base_state() -> SimState:
    return playout(21, 4)


def test_compile_drops_unknown_uninitiated_duplicate_and_garbage() -> None:
    state = base_state()
    initiated = {oid for oid in OPTIONS if OPTIONS[oid].initiation(state, 0)}
    assert len(initiated) >= 3  # the fixture must have real choice
    # rank EVERYTHING: known, unknown, not-initiated, duplicated
    everything = sorted(OPTIONS) + ["nuke_everything", "be_the_map"]
    text = json.dumps({"ranked": everything + everything[:2],
                       "assumptions": ["x", 5, None, "y"],
                       "contingencies": "not-a-list"})
    proposal = compile_proposal(text, state, 0)
    # order preserved from the model's ranking, filtered to legal ids only
    assert proposal.ranked == [oid for oid in everything if oid in initiated]
    assert set(proposal.ranked) == initiated
    for bad in ("", "not json", "[]", '{"ranked": "defend"}',
                '{"ranked": [1, 2]}', "null"):
        assert compile_proposal(bad, state, 0).ranked == []
    # assumptions keep strings only; contingencies non-list -> empty
    assert proposal.raw_assumptions == ["x", "y"]
    assert proposal.raw_contingencies == []


def test_view_and_menu_are_deterministic_and_identity_free() -> None:
    state = base_state()
    view, menu = render_view(state, 0), render_menu(state, 0)
    assert view == render_view(state, 0) and menu == render_menu(state, 0)
    assert "player_id" not in view and "agent_id" not in view
    assert menu.count("available_now=") == len(OPTIONS)  # every option rated
    req = build_request(state, 0)
    assert req["messages"][0]["content"].count("OPTION MENU") == 1


async def test_prior_orders_first_visits() -> None:
    state = SimState.from_doc(duel_start(21))
    facade = FakeFacade(state, 0)
    belief = PlannerBelief(0)
    await feed_via(facade, belief)
    bstate = SimState.from_doc(build_state_doc(belief, seed=4))
    initiated = [oid for oid in sorted(OPTIONS)
                 if OPTIONS[oid].initiation(bstate, 0)]
    assert len(initiated) >= 4

    # sub-candidate budget: unprimed search explores in candidate order,
    # primed search explores the PROPOSED option first
    unprimed = search_option(belief, 0, method="mcts", budget=1, seed=4)
    assert list(unprimed.root_visits) == [initiated[0]]
    assert unprimed.prior == []
    assert unprimed.chosen == initiated[0]

    target = initiated[-1]  # alphabetically LAST: the prior must move it first
    primed = search_option(belief, 0, method="mcts", budget=1, seed=4,
                           prior=[target, "not_an_option"])
    assert list(primed.root_visits) == [target]
    assert primed.chosen == target
    assert primed.candidates == unprimed.candidates  # prior permutes only


async def test_full_match_with_scripted_proposer(tmp_path):
    from fakes import FakeModel

    def reply(ranked):
        return [{"type": "text", "text": json.dumps({"ranked": ranked})}]

    model = FakeModel(script=[reply(["economy", "develop", "expand"])])
    spec = MatchSpec(
        match_id="planner-proposer", seed=424242, max_turns=10, adapter="simulator",
        watchdog_mode="flag_and_continue", violation_limit=5, checkpoint_every=5,
        agents=[
            AgentSpec(agent_id="planner", player_id=0, policy="planner", seed=7),
            AgentSpec(agent_id="korea", player_id=1, policy="turtler", seed=22),
        ],
    )
    bot = PlannerRuntime(0, 7, budget=4, proposer=model)
    turtler = build_runtime(AgentProfile(agent_id="korea", player_id=1,
                                         policy="turtler", seed=22))
    arena = Arena(tmp_path / "run", spec, runtimes={0: bot, 1: turtler})
    summary = await arena.run()
    assert summary["final_turn"] == 10 and summary["violations_total"] == 0
    assert model.posts_sent > 0

    records = [json.loads(line) for line in
               (tmp_path / "run" / "events.jsonl").read_text().splitlines()]
    rejected = [r for r in records if r["kind"] == "TOOL_RESULT"
                and r.get("status") == "rejected" and r["player_id"] == 0]
    assert rejected == []
    # the proposal rode the trace as a side artifact entry, never an event
    assert all(r["kind"] != "TOOL_CALL" or r["tool"] != "propose"
               for r in records)
    entries = [t for t in bot.trace if "proposal" in t]
    assert entries and entries[0]["proposal"]["ranked"], "proposer never primed"
    assert entries[0]["prior"] == entries[0]["proposal"]["ranked"]

    result = await replay_run(tmp_path / "run", spec, tmp_path / "replay")
    assert result["identical"], (
        f"replay diverged at comparable-event {result['first_divergence']}")


async def test_dead_proposer_degrades_to_unprimed(tmp_path):
    from civ_arena.agents.llm.client import ModelUnavailable

    class DeadModel:
        async def create(self, **_kw):
            raise ModelUnavailable("endpoint down")

        async def aclose(self):
            pass

    spec = MatchSpec(
        match_id="planner-dead-proposer", seed=424242, max_turns=6,
        adapter="simulator", watchdog_mode="flag_and_continue",
        violation_limit=5, checkpoint_every=3,
        agents=[
            AgentSpec(agent_id="planner", player_id=0, policy="planner", seed=7),
            AgentSpec(agent_id="korea", player_id=1, policy="turtler", seed=22),
        ],
    )
    bot = PlannerRuntime(0, 7, budget=2, proposer=DeadModel())
    turtler = build_runtime(AgentProfile(agent_id="korea", player_id=1,
                                         policy="turtler", seed=22))
    arena = Arena(tmp_path / "run", spec, runtimes={0: bot, 1: turtler})
    summary = await arena.run()
    assert summary["final_turn"] == 6 and summary["violations_total"] == 0
    assert all(t["proposal"] == {"turn": t["turn"], "error": "model_unavailable"}
               for t in bot.trace if "proposal" in t)
    assert all(t["prior"] == [] for t in bot.trace)
