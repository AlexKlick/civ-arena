"""M20b: the contextual fixed-point bandit — online learning that acts.

Pinned: the state is fixed-point integers only (canonical to_doc /
from_doc round-trip, loud refusals on floats and impossible stats);
ranking is exact integer cross-multiplication (the M19b rank() precedent)
over a FULL permutation of the given menu with a deterministic untried
tail; context buckets are the M19c mining ladders over the search
abstract doc (below-threshold drifts share a context, crossings split
it, and the rung counts equal mining.feature_items' item counts on the
same doc); the state bound evicts smallest-n (ties -> lexicographically
smallest) deterministically; the runtime chain fails soft (a poisoned
bandit degrades to unprimed and the match completes hash-identical to
the unarmed twin); same seed -> identical traces; the resume pin holds
WITH the bandit armed because the bandit state AND the pending decision
ride the journal's bandit block; and an init state that dominates a
context actually ACTS — the search's first visit goes to the dominant
option at sub-menu budget.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from civ_arena.agents.runtime import AgentProfile, build_runtime
from civ_arena.arena.checkpoints import CheckpointState
from civ_arena.arena.coordinator import Arena
from civ_arena.canonical import canonical
from civ_arena.config import AgentSpec, MatchSpec
from civ_arena.game.sim.layouts import duel_start
from civ_arena.game.sim.state import SimState
from civ_arena.planner.bandit import (
    MAX_CONTEXTS,
    Bandit,
    context_bucket,
)
from civ_arena.planner.mining import (
    GOLD_THRESHOLDS,
    RESEARCHED_THRESHOLDS,
    UNIT_THRESHOLDS,
    feature_items,
)
from civ_arena.planner.runtime import PlannerRuntime
from civ_arena.planner.search import abstract_key
from test_planner_resume import runtimes as resume_runtimes
from test_planner_resume import spec_for as resume_spec_for


def _spec(match_id: str, max_turns: int = 8) -> MatchSpec:
    return MatchSpec(
        match_id=match_id, seed=424242, max_turns=max_turns, adapter="simulator",
        watchdog_mode="flag_and_continue", violation_limit=5, checkpoint_every=5,
        agents=[
            AgentSpec(agent_id="planner", player_id=0, policy="planner", seed=7),
            AgentSpec(agent_id="turtler", player_id=1, policy="turtler", seed=22),
        ],
    )


def _runtimes(bandit: Any = None, budget: int = 4) -> dict[int, Any]:
    return {
        0: PlannerRuntime(0, 7, budget=budget, bandit=bandit),
        1: build_runtime(AgentProfile(agent_id="turtler", player_id=1,
                                      policy="turtler", seed=22)),
    }


async def _run(tmp_path, match_id: str, runtimes: dict[int, Any],
               max_turns: int = 8) -> tuple[Arena, dict[str, Any]]:
    arena = Arena(tmp_path / match_id, _spec(match_id, max_turns),
                  runtimes=runtimes)
    return arena, await arena.run()


def _no_floats(node: Any) -> bool:
    if isinstance(node, float):
        return False
    if isinstance(node, dict):
        return all(_no_floats(v) for v in node.values())
    if isinstance(node, list):
        return all(_no_floats(v) for v in node)
    return True


# --------------------------------------------------------- fixed point


def test_bandit_fixed_point_no_floats() -> None:
    b = Bandit()
    ctx_a, ctx_b = (0, 0, 0, 1, 0, 0), (1, 2, 0, 3, 1, 2)
    for i in range(5):
        b.update(ctx_a, "economy", 137 + i)
        b.update(ctx_a, "rush", -91)
        b.update(ctx_b, "expand", 7 if i % 2 else -7)
    doc = b.to_doc()
    canonical(doc)  # canonical() refuses floats/tuples — must not raise
    assert _no_floats(doc)
    assert doc["contexts"]["0,0,0,1,0,0"]["economy"] == {"n": 5, "adv_sum": 695}

    # JSON round-trip (the journal/write path) then from_doc: exact state
    twin = Bandit.from_doc(json.loads(json.dumps(doc)))
    assert twin.to_doc() == doc
    assert canonical(twin.to_doc()) == canonical(doc)
    menu = ["economy", "rush", "expand", "tech_race"]
    assert twin.rank(ctx_a, menu) == b.rank(ctx_a, menu)
    assert twin.updates == b.updates == 15
    assert twin.abs_adv_sum == b.abs_adv_sum

    # loud refusals hold the contract at every entry point
    with pytest.raises(ValueError):
        b.update(ctx_a, "economy", 1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Bandit({"schema": 2, "contexts": {}})
    with pytest.raises(ValueError):
        Bandit({"schema": 1, "contexts":
                {"0,0": {"x": {"n": 0, "adv_sum": 0}}}})  # n=0 carries nothing
    with pytest.raises(ValueError):
        Bandit({"schema": 1, "contexts":
                {"0,0": {"x": {"n": 1, "adv_sum": 0.5}}}})
    with pytest.raises(ValueError):
        Bandit({"schema": 1, "contexts": {"zero": {}}})  # bad context key


# ------------------------------------------------------- ranking rules


def test_bandit_rank_exact_rational_and_tail() -> None:
    """Informed arms by mean advantage via EXACT cross-multiplication
    (the 2**53-scale float hazard orders correctly), ties by option_id,
    untried arms in a deterministic sorted tail; rank is a permutation of
    the given menu (duplicates drop, non-strings drop)."""
    b = Bandit()
    ctx = (0,) * 6
    # two arms whose means are EXACTLY equal (1/2) at 2**53 scale, via an
    # init doc: the float prints 0.5 for both, the exact comparison ties,
    # option_id asc decides -> big < half
    big_n, big_adv = 2**54, 2**53
    init = {"schema": 1, "contexts": {
        ",".join(map(str, ctx)): {
            "big": {"n": big_n, "adv_sum": big_adv},
            "half": {"n": big_n, "adv_sum": big_adv},
        }}, "updates": 0, "abs_adv_sum": 0}
    assert big_adv / big_n == 0.5  # the float hazard is real at this scale
    assert Bandit(init).rank(ctx, ["half", "big"]) == ["big", "half"]
    # nudge half's mean above 1/2 by the SMALLEST exact amount: the float
    # still prints 0.5 (2**53+1 is not representable), the exact integer
    # cross-multiplication does not tie
    init["contexts"][",".join(map(str, ctx))]["half"] = \
        {"n": big_n, "adv_sum": big_adv + 1}
    assert (big_adv + 1) / big_n == 0.5  # the hazard is still real
    assert Bandit(init).rank(ctx, ["half", "big"]) == ["half", "big"]

    # mean ordering + untried tail
    b.update(ctx, "economy", 50)
    b.update(ctx, "economy", 50)      # mean 50
    b.update(ctx, "rush", -10)        # mean -10
    b.update(ctx, "tech_race", 50)    # ties economy -> option_id asc
    ranked = b.rank(ctx, ["tech_race", "defend", "rush", "economy",
                          "economy", "tech_race"])
    assert ranked == ["economy", "tech_race", "rush", "defend"]
    assert sorted(ranked) == ["defend", "economy", "rush", "tech_race"]
    assert b.rank((9, 9, 9, 9, 9, 9), ["rush", "economy"]) == \
        ["economy", "rush"]  # unknown context: untried tail only


# ------------------------------------------------------ context ladders


def test_bandit_context_buckets_match_ladders() -> None:
    base = SimState.from_doc(duel_start(21))
    assert context_bucket(base, 0) == (0, 0, 0, 1, 0, 0)

    # the alignment pin: rung counts == mining.feature_items item counts
    # over the SAME abstract doc (the shared M19c context vocabulary)
    rich = SimState.from_doc(base.to_doc())
    rich.player(0)["gold"] = 600          # gold_bucket 24 -> ladder 3
    rich.player(0)["researched"] = ["MINING", "POTTERY", "SAILING",
                                    "WRITING"]  # 4 techs -> ladder 2
    rich.player(1)["researched"] = ["MINING"]  # rival 1 tech -> ladder 1
    ctx = context_bucket(rich, 0)
    assert ctx == (0, 3, 2, 1, 0, 1)
    items = feature_items(abstract_key(rich, 0), "economy")
    assert ctx[1] == sum(1 for t in GOLD_THRESHOLDS
                         if f"p0:gold_bucket>={t}" in items)
    assert ctx[2] == sum(1 for t in RESEARCHED_THRESHOLDS
                         if f"p0:researched>={t}" in items)
    assert ctx[3] == sum(1 for t in UNIT_THRESHOLDS
                         if f"p0:units>={t}" in items)
    assert ctx[5] == sum(1 for t in RESEARCHED_THRESHOLDS
                         if f"p1:researched>={t}" in items)

    def with_gold(gold: int) -> SimState:
        s = SimState.from_doc(base.to_doc())
        s.player(0)["gold"] = gold
        return s

    def with_own_units(count: int) -> SimState:
        s = SimState.from_doc(base.to_doc())
        own = sorted(u["unit_id"] for u in s.units.values()
                     if u["owner"] == 0)
        for uid in own[count:]:
            s.remove_unit(uid)
        return s

    # BELOW the thresholds: drift shares a context (100 vs 174 gold are
    # buckets 4 vs 6, both ladder 0; 3 vs 4 units both ladder 0)
    assert context_bucket(with_gold(100), 0) == context_bucket(with_gold(174), 0)
    assert context_bucket(with_own_units(3), 0) \
        == context_bucket(with_own_units(4), 0)
    # CROSSING a threshold splits it (199 vs 200 gold == bucket 7 vs 8;
    # 4 vs 5 units == below/above the 5-unit rung)
    assert context_bucket(with_gold(199), 0)[1] == 0
    assert context_bucket(with_gold(200), 0)[1] == 1
    assert context_bucket(with_gold(199), 0) != context_bucket(with_gold(200), 0)
    assert context_bucket(with_own_units(4), 0) != context_bucket(base, 0)

    # research crossings, own and rival independently
    own_tech = SimState.from_doc(base.to_doc())
    own_tech.player(0)["researched"] = ["MINING"]
    assert context_bucket(own_tech, 0) != context_bucket(base, 0)
    rival_tech = SimState.from_doc(base.to_doc())
    rival_tech.player(1)["researched"] = ["MINING"]
    assert context_bucket(rival_tech, 0) != context_bucket(base, 0)
    assert context_bucket(rival_tech, 0) == (0, 0, 0, 1, 0, 1)

    # cities stay EXACT (0 vs 1 vs 2 all split — no bucketing axis)
    def with_cities(n: int) -> SimState:
        s = SimState.from_doc(base.to_doc())
        for i in range(n):
            s.cities[f"c{i + 9}"] = {
                "city_id": f"c{i + 9}", "owner": 0, "population": 1,
                "q": -3 - i, "r": 2, "buildings": [],
                "production_queue": []}
        return s

    assert context_bucket(with_cities(0), 0) != context_bucket(with_cities(1), 0)
    assert context_bucket(with_cities(1), 0) != context_bucket(with_cities(2), 0)
    assert context_bucket(with_cities(2), 0)[:1] == (2,)


# ---------------------------------------------------------- state bound


def test_bandit_state_bound_deterministic() -> None:
    b = Bandit()
    ctxs = [tuple([i] + [0] * 5) for i in range(MAX_CONTEXTS)]
    for i, ctx in enumerate(ctxs):
        for _ in range(i + 1):  # ctx i carries total n = i + 1
            b.update(ctx, "economy", 1)
    assert len(b) == MAX_CONTEXTS
    assert len(b.to_doc()["contexts"]) == MAX_CONTEXTS

    # the 513th context evicts the SMALLEST-n context (ctx 0, total n=1)
    b.update(tuple([999] + [0] * 5), "rush", 5)
    keys = b.to_doc()["contexts"]
    assert len(keys) == MAX_CONTEXTS
    assert "0,0,0,0,0,0" not in keys      # smallest n dropped
    assert "1,0,0,0,0,0" in keys
    assert "999,0,0,0,0,0" in keys

    # ties (equal total n) drop the lexicographically smallest context,
    # deterministically: same feed sequence -> identical docs
    t = Bandit()
    for i in range(MAX_CONTEXTS):
        t.update(tuple([i] + [0] * 5), "economy", 1)
    t.update(tuple([999] + [0] * 5), "economy", 1)
    t2 = Bandit()
    for i in range(MAX_CONTEXTS):
        t2.update(tuple([i] + [0] * 5), "economy", 1)
    t2.update(tuple([999] + [0] * 5), "economy", 1)
    assert t.to_doc() == t2.to_doc()
    tie_keys = t.to_doc()["contexts"]
    assert len(tie_keys) == MAX_CONTEXTS
    assert "0,0,0,0,0,0" not in tie_keys and "1,0,0,0,0,0" in tie_keys

    # construction enforces the bound too: an oversized init doc trims
    # smallest-n-first, ties lexicographic — here the three smallest keys
    fat = {"schema": 1, "contexts": {
        ",".join(map(str, [i] + [0] * 5)): {"economy": {"n": 1, "adv_sum": 0}}
        for i in range(MAX_CONTEXTS + 3)}}
    trimmed = Bandit(fat)
    assert len(trimmed) == MAX_CONTEXTS
    trimmed_keys = trimmed.to_doc()["contexts"]
    for i in (0, 1, 2):
        assert ",".join(map(str, [i] + [0] * 5)) not in trimmed_keys
    assert ",".join(map(str, [3] + [0] * 5)) in trimmed_keys


# ------------------------------------------------------ runtime fail-soft


async def test_bandit_permutation_only_and_fails_soft(tmp_path) -> None:
    # healthy armed match: the prior can only PERMUTE — every decision's
    # bandit ranking is a subset of that decision's candidate menu, and
    # the trace doc carries turn/ranked/context
    arena, summary = await _run(tmp_path, "bandit-perm", _runtimes(Bandit()))
    assert summary["final_turn"] == 8 and summary["violations_total"] == 0
    trace = arena.runtimes[0].trace
    assert trace and all("bandit_prior" in t for t in trace)
    for entry in trace:
        assert set(entry["bandit_prior"]["ranked"]) <= set(entry["candidates"])
        assert set(entry.get("prior") or []) <= set(entry["candidates"])
        assert len(entry["bandit_prior"]["context"]) == 6
        assert entry["bandit_prior"]["turn"] == entry["turn"]

    # poisoned bandit (rank AND update raise): degrades to unprimed, the
    # match completes, and is hash-identical to the same-seed unarmed twin
    bad = Bandit()

    def boom(*args: Any, **kwargs: Any) -> list[str]:
        raise RuntimeError("poisoned bandit")

    bad.rank = boom  # type: ignore[method-assign]
    bad.update = boom  # type: ignore[method-assign]

    poisoned, poisoned_summary = await _run(
        tmp_path, "bandit-poisoned", _runtimes(bad), max_turns=6)
    assert poisoned_summary["final_turn"] == 6
    assert poisoned_summary["violations_total"] == 0
    ptrace = poisoned.runtimes[0].trace
    assert ptrace and all(
        t["bandit_prior"] == {"turn": t["turn"],
                              "error": "bandit_prior_failed"}
        for t in ptrace)
    assert all(t["prior"] == [] for t in ptrace)

    bare, bare_summary = await _run(tmp_path, "bandit-bare",
                                    _runtimes(), max_turns=6)
    assert bare_summary["final_state_hash"] == poisoned_summary["final_state_hash"]
    assert all("bandit_prior" not in t for t in bare.runtimes[0].trace)


async def test_bandit_deterministic_given_seed(tmp_path) -> None:
    a, summary_a = await _run(tmp_path, "bandit-det-a", _runtimes(Bandit()),
                              max_turns=12)
    b, summary_b = await _run(tmp_path, "bandit-det-b", _runtimes(Bandit()),
                              max_turns=12)
    assert summary_a["final_turn"] == 12 and summary_b["final_turn"] == 12
    assert summary_a["violations_total"] == 0
    assert summary_a["final_state_hash"] == summary_b["final_state_hash"]
    assert a.runtimes[0].trace == b.runtimes[0].trace
    assert a.runtimes[0].bandit.to_doc() == b.runtimes[0].bandit.to_doc()
    assert a.runtimes[0].bandit.to_doc()["updates"] > 0  # learning ran


# ------------------------------------------------------------ it acts


async def test_bandit_learns_obvious_case(tmp_path) -> None:
    """The 'it actually acts' pin: an init state whose advantages dominate
    one context puts that option FIRST in the prior, and at budget 1
    (below the 5-8 candidate menu — the M19b activation condition) the
    search's only visit goes to it, so it IS the chosen option."""
    harvest, _ = await _run(tmp_path, "learns-harvest",
                            _runtimes(Bandit(), budget=1), max_turns=2)
    first = harvest.runtimes[0].trace[0]
    ctx = tuple(first["bandit_prior"]["context"])
    menu = list(first["candidates"])
    dominant = next(oid for oid in sorted(menu) if oid != first["chosen"])

    init = Bandit()
    for _ in range(6):
        init.update(ctx, dominant, 900)
    for other in menu:
        if other != dominant:
            init.update(ctx, other, -900)
    assert init.rank(ctx, menu) == [dominant, *(sorted(
        oid for oid in menu if oid != dominant))]

    arena, summary = await _run(tmp_path, "learns-armed",
                                _runtimes(init, budget=1), max_turns=2)
    assert summary["violations_total"] == 0
    armed = arena.runtimes[0].trace[0]
    assert armed["candidates"] == menu  # same world, same menu
    assert armed["bandit_prior"]["ranked"][0] == dominant
    assert armed["prior"][0] == dominant
    assert sorted(armed["prior"]) == sorted(menu)  # full menu, reordered
    assert armed["chosen"] == dominant  # budget 1: the first-visited arm wins


# ---------------------------------------------------------- resume pin


async def test_bandit_resume_bit_identical(tmp_path) -> None:
    """The 40-turn resume pattern with the bandit armed (fresh EMPTY bandit
    both legs — exp-M20b's shape): the resumed twin is bit-identical in
    final hash, per-turn tool-call sequence, AND final learned state —
    which holds only because the pending decision rides the journal's
    bandit block (without it the first post-resume decision settles no
    pending, one update is lost, and the learned state diverges)."""
    spec = resume_spec_for("bandit-resume-id", max_turns=40)
    arena = Arena(tmp_path / "run", spec,
                  runtimes=resume_runtimes(bandit=Bandit()))
    summary = await arena.run()
    assert summary["final_turn"] == 40 and summary["violations_total"] == 0
    bot = arena.runtimes[0]
    assert bot.bandit.to_doc()["updates"] > 0  # online learning ran
    first_log = (tmp_path / "run" / "events.jsonl").read_text().splitlines()
    tail_calls = [json.loads(x) for x in first_log
                  if json.loads(x)["kind"] == "TOOL_CALL"
                  and json.loads(x)["turn"] >= 21
                  and json.loads(x)["player_id"] == 0]

    ckpt = CheckpointState.from_doc(json.loads(
        (tmp_path / "run" / "checkpoints" / "ckpt-turn-0020.json").read_text()))

    resumed = Arena(tmp_path / "run", spec,
                    runtimes=resume_runtimes(bandit=Bandit()))
    rsummary = await resumed.run(resume_state=ckpt)
    assert rsummary["final_turn"] == 40 and rsummary["violations_total"] == 0
    assert rsummary["final_state_hash"] == summary["final_state_hash"]

    second_log = (tmp_path / "run" / "events.jsonl").read_text().splitlines()
    tail2 = [json.loads(x) for x in second_log
             if json.loads(x)["kind"] == "TOOL_CALL"
             and json.loads(x)["turn"] >= 21 and json.loads(x)["player_id"] == 0]

    def key(r):
        return (r["turn"], r["tool"], r["args_digest"])

    assert [key(r) for r in tail2] == [key(r) for r in tail_calls], (
        "resumed planner with a bandit diverged from its uninterrupted twin")
    # the learned state (AND the pending decision, implicitly — a lost
    # settle would undercount updates) continues instead of resetting
    assert resumed.runtimes[0].bandit.to_doc() == bot.bandit.to_doc()
    assert resumed.runtimes[0].bandit.to_doc()["updates"] \
        == bot.bandit.to_doc()["updates"] > 0
    # the journal carries the bandit block on armed legs, beside the log
    journal_text = (tmp_path / "run" / "planner" / "p0-journal.jsonl"
                    ).read_text()
    assert "bandit" in journal_text and "last_option" in journal_text
