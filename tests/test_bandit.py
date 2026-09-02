"""M20b: the contextual fixed-point bandit — online learning that acts.

Pinned: the state is fixed-point integers only (canonical to_doc /
from_doc round-trip, loud refusals on floats and impossible stats);
ranking is exact integer cross-multiplication (the M19b rank() precedent)
over a FULL permutation of the given menu with a deterministic untried
tail; context buckets are the M19c mining ladders over the search
abstract doc (below-threshold drifts share a context, crossings split
it, and the rung counts equal mining.feature_items' item counts on the
same doc); the state bound evicts smallest-n POST-STATE (insert+credit
first — the incoming context can itself be the evictee; ties ->
lexicographically smallest) deterministically; context keys are the
canonical six-component form only (aliases refuse, B5); the journal's
bandit block is strict (a partial pending RAISES and disarms the leg,
never a silent one-update loss; a non-dict block disarms too, B6); a
rewind-reuse onto a snapshot that predates bandit state rewinds the
learned state to the constructor's initial snapshot (B4); the runtime
chain fails soft (a poisoned bandit degrades to unprimed and the match
completes hash-identical to the unarmed twin); same seed -> identical
traces; the resume pin holds WITH the bandit armed because the bandit
state AND the pending decision ride the journal's bandit block; an init
state that dominates a context actually ACTS — the search's first visit
goes to the dominant option at sub-menu budget; and the experiment
runner's paired report gates inference on validity (dirty/short/
duplicate rows suppress the p-value entirely, B1) and labels BOTH
descriptive conventions (decidable-only and all-pairs means, B2).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
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
    CONTEXT_AXES,
    MAX_CONTEXTS,
    Bandit,
    context_bucket,
)
from civ_arena.planner.journal import PlannerJournal
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
    # B5 — context keys are the CANONICAL six-component form only:
    # aliases ("00,..", "-0,.."), wrong arity, and non-canonical re-emits
    # refuse, so two spellings of one context can never collide in state
    good_rec = {"economy": {"n": 1, "adv_sum": 2}}
    for alias in ("00,0,0,0,0,0", "-0,0,0,0,0,0", "+1,0,0,0,0,0",
                  "0,0,0,0,0", "0,0,0,0,0,0,0", " 0,0,0,0,0,0"):
        with pytest.raises(ValueError):
            Bandit({"schema": 1, "contexts": {alias: good_rec}})
    Bandit({"schema": 1, "contexts": {"0,0,0,0,0,0": good_rec}})  # canonical


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

    # B3 — the bound is a POST-STATE rule: insert + credit FIRST, then
    # evict, so an update to a NEW context that ends up smallest (here a
    # first trial against a table of n=2 contexts) evicts THE NEW CONTEXT
    post = Bandit()
    for i in range(MAX_CONTEXTS):
        post.update(tuple([i] + [0] * 5), "economy", 1)
        post.update(tuple([i] + [0] * 5), "economy", 1)  # every context n=2
    assert len(post) == MAX_CONTEXTS
    post.update(tuple([999] + [0] * 5), "rush", 7)  # new context -> n=1
    post_keys = post.to_doc()["contexts"]
    assert len(post_keys) == MAX_CONTEXTS
    assert "999,0,0,0,0,0" not in post_keys  # the newcomer was smallest
    assert "0,0,0,0,0,0" in post_keys and "511,0,0,0,0,0" in post_keys
    # and the update still counted (credit happened before eviction)
    assert post.updates == MAX_CONTEXTS * 2 + 1
    assert post.abs_adv_sum == MAX_CONTEXTS * 2 + 7


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


# ----------------------------------------------- B4: rewind-aware reuse


async def test_bandit_rewind_reuse_drops_future_learned_state(tmp_path):
    """B4 pin — the exact reported repro: an armed runtime resumes an OLD
    (unarmed-leg) journal and runs to 40, so the journal's snapshots at
    and below turn 20 predate bandit state. Rewinding that SAME runtime
    back to 21 must rewind the learned state to the constructor's INITIAL
    snapshot with no pending — never keep the turn-21..40 learning."""
    # leg 1: UNARMED to 20 — journal turns 1..20 carry no bandit block
    arena1 = Arena(tmp_path / "run", resume_spec_for("bandit-rewind", 20),
                   runtimes=resume_runtimes(budget=2))
    s1 = await arena1.run()
    assert s1["final_turn"] == 20 and s1["violations_total"] == 0

    ckpt = CheckpointState.from_doc(json.loads(
        (tmp_path / "run" / "checkpoints" / "ckpt-turn-0020.json").read_text()))

    # leg 2: armed runtime resumes the old journal and runs to 40, learning
    arena2 = Arena(tmp_path / "run", resume_spec_for("bandit-rewind", 40),
                   runtimes=resume_runtimes(budget=2, bandit=Bandit()))
    s2 = await arena2.run(resume_state=ckpt)
    assert s2["final_turn"] == 40 and s2["violations_total"] == 0
    rt = arena2.runtimes[0]
    learned = rt.bandit.to_doc()
    assert learned["updates"] > 0  # the future learned state exists to lose

    # rewind-reuse to 21: the selected snapshot (turn 20) predates bandit
    # state -> the constructor's INITIAL snapshot + no pending, never the
    # turn-40 learned state
    rt._restore_from_journal(21)
    assert rt.bandit.to_doc() == Bandit().to_doc()
    assert rt.bandit.to_doc()["updates"] == 0
    assert rt._bandit_pending is None
    assert rt.bandit.to_doc() != learned


# --------------------------------------- B5/B6: journal block validation


_ABSENT = object()  # sentinel: snapshot carries no bandit key at all


def _rt_with_journal_block(tmp_path, block, name: str) -> PlannerRuntime:
    """An armed runtime whose journal's latest snapshot (turn 4) carries
    the given bandit block (or none, for _ABSENT) — the
    test_case_artifact_mismatch harness shape."""
    rt = PlannerRuntime(0, 7, bandit=Bandit())
    j = PlannerJournal(tmp_path / name / "p0-journal.jsonl")
    snapshot: dict[str, Any] = {
        "turn": 4,
        "belief": {
            "own_player": {"player_id": 0, "civ_name": "ROME", "gold": 50,
                           "researched": [], "researching": ""},
            "public_players": {1: {"civ_name": "KOREA", "alive": True}},
            "own_units": {}, "own_cities": {}, "foreign_units": {},
            "foreign_cities": {}, "tiles": {}, "turn": 4},
        "active": "rush", "chosen_at_turn": 4,
    }
    if block is not _ABSENT:
        snapshot["bandit"] = block
    j.append(4, snapshot)
    rt.journal = j
    return rt


def _state_block(**pending: Any) -> dict[str, Any]:
    return {"schema": 1, "contexts": {}, "updates": 0, "abs_adv_sum": 0,
            **pending}


def test_bandit_journal_block_validation(tmp_path) -> None:
    """B5+B6: the journal's bandit block is validated strictly at restore.
    A PRESENT-but-malformed block (non-dict, alias/arity context keys, a
    PARTIAL pending) disarms the bandit for the leg — never a crash, never
    a silent one-update loss; a snapshot with no block at all predates
    bandit state and rewinds to the initial snapshot (B4, pinned above)."""
    # the pending parser itself: coherent nulls, full validity, strictness
    parse = PlannerRuntime._bandit_pending_from_doc
    assert parse(_state_block()) is None  # four coherent nulls
    full = _state_block(last_decision_turn=3, last_context=[0, 0, 0, 1, 0, 0],
                        last_option="rush", last_value=310)
    assert parse(full) == {"turn": 3, "context": (0, 0, 0, 1, 0, 0),
                           "option": "rush", "value": 310}
    for bad in (
        {"last_decision_turn": 3, "last_context": [0] * CONTEXT_AXES,
         "last_option": "rush", "last_value": None},          # partial
        {"last_decision_turn": 3, "last_context": [0] * 5,
         "last_option": "rush", "last_value": 1},             # arity
        {"last_decision_turn": True, "last_context": [0] * CONTEXT_AXES,
         "last_option": "rush", "last_value": 1},             # bool turn
        {"last_decision_turn": 3, "last_context": [0] * CONTEXT_AXES,
         "last_option": "rush", "last_value": True},          # bool value
        {"last_decision_turn": 3, "last_context": [0] * CONTEXT_AXES,
         "last_option": 7, "last_value": 1},                  # non-str option
        {"last_decision_turn": 3, "last_context": ["0"] * CONTEXT_AXES,
         "last_option": "rush", "last_value": 1},             # non-int ctx
    ):
        with pytest.raises(ValueError):
            parse(_state_block(**bad))

    # a valid block restores state + pending exactly
    rt = _rt_with_journal_block(tmp_path, full, "valid")
    rt._restore_from_journal(5)
    assert rt.bandit is not None and rt.bandit.to_doc()["updates"] == 0
    assert rt._bandit_pending == {"turn": 3, "context": (0, 0, 0, 1, 0, 0),
                                  "option": "rush", "value": 310}
    # coherent nulls restore state with NO pending
    rt = _rt_with_journal_block(tmp_path, _state_block(), "nulls")
    rt._restore_from_journal(5)
    assert rt.bandit is not None and rt._bandit_pending is None

    # malformed blocks DISARM the leg (fail-soft at the restore boundary)
    for name, block in (
        ("nondict", ["not", "a", "mapping"]),
        ("alias-key", {"schema": 1, "contexts":
                       {"00,0,0,0,0,0": {"rush": {"n": 1, "adv_sum": 1}}},
                       "updates": 0, "abs_adv_sum": 0}),       # B5
        ("arity-key", {"schema": 1, "contexts":
                       {"0,0,0,0,0": {"rush": {"n": 1, "adv_sum": 1}}},
                       "updates": 0, "abs_adv_sum": 0}),       # B5
        ("partial-pending", _state_block(
            last_decision_turn=3, last_context=[0] * CONTEXT_AXES,
            last_option=None, last_value=None)),               # B6
        ("half-pending", _state_block(
            last_decision_turn=3, last_context=None, last_option="rush",
            last_value=None)),                                 # B6
        ("bad-schema", {"schema": 2, "contexts": {}}),
    ):
        rt = _rt_with_journal_block(tmp_path, block, name)
        rt._restore_from_journal(5)
        assert rt.bandit is None, f"{name} block must disarm the bandit"
        assert rt._bandit_pending is None

    # a snapshot with NO bandit key is not malformed — it predates bandit
    # state (the B4 initial-snapshot rewind, unit-level here)
    rt = _rt_with_journal_block(tmp_path, _ABSENT, "absent")
    rt._bandit_pending = {"turn": 39, "context": (9, 9, 9, 9, 9, 9),
                          "option": "rush", "value": 5}
    rt._restore_from_journal(5)
    assert rt.bandit is not None
    assert rt.bandit.to_doc() == Bandit().to_doc()  # initial snapshot
    assert rt._bandit_pending is None


# ------------------------------------- B1/B2: experiment report validity


def _load_script(module_name: str):
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(__file__).resolve().parent.parent / "scripts" / f"{module_name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _exp_row(arm: str, seed: int, side: int, diff: int, *, turns: int = 40,
             viol: int = 0) -> dict[str, Any]:
    armed = arm == "bandit"
    return {
        "match_id": f"synthetic-{arm}-s{seed}-p{side}", "arm": arm,
        "seed": seed, "side": side, "budget": 4, "turns": turns,
        "violations": viol, "planner_rejections": 0,
        "value_differential": diff, "decisions": 3,
        "bandit_updates": 2 if armed else 0,
        "bandit_abs_adv_sum": 20 if armed else 0,
        "mean_abs_advantage": 10 if armed else 0,
        "bandit_contexts": 1 if armed else 0,
        "chosen": [], "wall_ms": 1,
    }


def _exp_rows() -> list[dict[str, Any]]:
    """Three complete pairs with diffs +30 / -12 / 0: decidable-only
    mean=median=+9, all-pairs mean=+6."""
    rows: list[dict[str, Any]] = []
    for i, bandit_diff in enumerate((30, -12, 0)):
        seed = 400_003 + i * 7919
        rows.append(_exp_row("bandit", seed, i % 2, bandit_diff))
        rows.append(_exp_row("base", seed, i % 2, 0))
    return rows


def test_bandit_experiment_report_gates_inference() -> None:
    """B1: dirty rows, short-horizon rows, and not-exactly-once arms each
    REFUSE inference — the reason prints, no p-value and no win count is
    computed over the polluted rows; a clean corpus infers normally."""
    mod = _load_script("bandit_experiment")
    clean = mod.paired_report(_exp_rows(), 40)
    assert "INFERENCE SUPPRESSED" not in clean
    assert "one-sided exact binomial p (H: bandit>base)" in clean
    assert "bandit wins 1 / base wins 1 / ties 1" in clean

    dirty = _exp_rows()
    dirty[0]["violations"] = 1
    text = mod.paired_report(dirty, 40)
    assert "INFERENCE SUPPRESSED" in text and "dirty row" in text
    assert "binomial p" not in text and "bandit wins" not in text

    short = _exp_rows()
    short[1]["turns"] = 39
    text = mod.paired_report(short, 40)
    assert "INFERENCE SUPPRESSED" in text and "horizon" in text
    assert "binomial p" not in text

    duplicate = _exp_rows() + [_exp_row("bandit", 400_003, 0, 5)]
    text = mod.paired_report(duplicate, 40)
    assert "INFERENCE SUPPRESSED" in text and "exactly-once" in text
    assert "binomial p" not in text


def test_bandit_experiment_report_descriptive_stats() -> None:
    """B2: BOTH labeled conventions report — decidable-only mean/median/
    min/max (the exp3 house convention) and the all-pairs mean (ties
    count as 0)."""
    text = _load_script("bandit_experiment").paired_report(_exp_rows(), 40)
    assert "paired diff decidable-only mean=+9 median=+9 min=-12 max=+30" \
        in text
    assert "paired diff all-pairs mean=+6 (ties count as 0)" in text


# ------------------------------------------------- all-ties edge (B2)


def test_bandit_experiment_report_all_ties() -> None:
    """A gate-clean all-ties corpus: no decidable pairs (no p-value), but
    the all-pairs mean still reports (0 over ties) and nothing suppresses."""
    rows = [_exp_row(arm, 400_003, side, 7)
            for side in (0, 1) for arm in ("bandit", "base")]
    text = _load_script("bandit_experiment").paired_report(rows, 40)
    assert "INFERENCE SUPPRESSED" not in text
    assert "binomial p" not in text
    assert "bandit wins 0 / base wins 0 / ties 2" in text
    assert "all-pairs mean=+0" in text
