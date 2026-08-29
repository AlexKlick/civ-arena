"""The strategy store: typed claims, last-known beliefs, own-state facts —
all rebuildable from the event log alone (the DiaryStore trust-root pattern).
"""

from __future__ import annotations

import random
from typing import Any

from civ_arena.arena.coordinator import Arena
from civ_arena.arena.visibility import FOREIGN_UNIT_FIELDS
from civ_arena.canonical import canonical
from civ_arena.config import AgentSpec, MatchSpec
from civ_arena.game.adapter import ObserveKind
from civ_arena.replay import replay_run
from civ_arena.strategy.beliefs import BeliefStore
from civ_arena.strategy.claims import MAX_CLAIM_CHARS, MAX_GOALS_PER_PLAYER
from civ_arena.strategy.facts import Facts
from civ_arena.strategy.store import StrategyStore

# ---------------------------------------------------------------- helpers


def call_rec(
    tool: str, args: dict[str, Any], *, player_id: int = 0, turn: int = 1,
    agent_id: str = "roman", match_id: str = "m", gi: str = "g1", seq: int = 0,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "kind": "TOOL_CALL", "tool": tool, "args": args, "seq": seq,
        "player_id": player_id, "agent_id": agent_id, "turn": turn,
        "match_id": match_id, "game_instance_id": gi,
        "phase_player_id": player_id, "visibility_scope": "private_player",
        **extra,
    }


def result_rec(
    tool: str, *, player_id: int = 0, turn: int = 1, agent_id: str = "roman",
    match_id: str = "m", gi: str = "g1", seq: int = 1, status: str = "accepted",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "kind": "TOOL_RESULT", "tool": tool, "seq": seq, "status": status,
        "player_id": player_id, "agent_id": agent_id, "turn": turn,
        "match_id": match_id, "game_instance_id": gi,
        "phase_player_id": player_id, "visibility_scope": "private_player",
        **extra,
    }


def _fu(uid: str) -> dict[str, Any]:
    return {
        "unit_id": uid, "type": "WARRIOR", "coord": "2,0", "owner_id": 1,
        "hp_bucket": 2, "strength": 20, "ranged_strength": 0,
    }


def _fc(cid: str) -> dict[str, Any]:
    return {
        "city_id": cid, "name": "ANTium", "coord": "1,1", "owner_id": 1,
        "hp": 100, "population": 3,
    }


# ------------------------------------------------------------ claim writes


def test_goal_create_assigns_ids_and_amend_is_id_stable():
    store = StrategyStore()
    doc = store.apply_goal(0, {"text": "hold 3 cities"}, turn=5, seq=10)
    assert doc == {"status": "accepted", "tool": "set_goal",
                   "goal_id": "g1", "revision": 1}
    doc2 = store.apply_goal(0, {"text": "hold 4 cities", "goal_id": "g1",
                                "by_turn": 20, "metric": "cities",
                                "target": 4}, turn=7, seq=30)
    assert doc2 == {"status": "accepted", "tool": "set_goal",
                    "goal_id": "g1", "revision": 2}
    history = store.goals[0]["g1"]
    assert len(history) == 2
    assert history[0].valid_to_turn == 6  # closed the turn before the amend
    assert history[1].revision == 2 and history[1].valid_from_turn == 7
    # id-stable: references to g1 keep resolving to the current authority
    assert store.current_goals(0) == [history[1]]
    # per-player isolation
    assert store.apply_goal(1, {"text": "other"}, 7, 31)["goal_id"] == "g1"


def test_goal_same_turn_amend_keeps_one_turn_validity():
    store = StrategyStore()
    store.apply_goal(0, {"text": "a"}, turn=5, seq=1)
    store.apply_goal(0, {"text": "b", "goal_id": "g1"}, turn=5, seq=2)
    first, second = store.goals[0]["g1"]
    assert first.valid_from_turn == 5 and first.valid_to_turn == 5
    assert second.valid_from_turn == 5 and second.valid_to_turn == 0


def test_goal_rejection_matrix():
    store = StrategyStore()
    store.apply_goal(0, {"text": "seed"}, turn=5, seq=1)
    bad_args = [
        {"text": ""},                                # blank
        {"text": "   "},                             # whitespace-only
        {"text": "x" * (MAX_CLAIM_CHARS + 1)},       # oversized raw
        {"text": 5},                                 # wrong type
        {"text": "ok", "metric": "happiness"},       # unknown metric
        {"text": "ok", "status": "paused"},          # unknown status
        {"text": "ok", "confidence": 101},           # out of range
        {"text": "ok", "confidence": True},          # bool is not an int here
        {"text": "ok", "target": -1},                # negative target
        {"text": "ok", "by_turn": 4},                # deadline in the past
        {"text": "ok", "goal_id": "g9"},             # unknown amend target
    ]
    for args in bad_args:
        doc = store.apply_goal(0, args, turn=5, seq=99)
        assert doc["status"] == "rejected", args
        assert doc["rejection"] == "args_invalid", args
        assert doc["reason"], args  # the model gets a self-correcting reason
    assert len(store.current_goals(0)) == 1  # nothing landed


def test_goal_cap_enforced():
    store = StrategyStore()
    for i in range(MAX_GOALS_PER_PLAYER):
        assert store.apply_goal(
            0, {"text": f"goal {i}"}, turn=1, seq=i)["status"] == "accepted"
    assert store.apply_goal(
        0, {"text": "one too many"}, 1, 999)["status"] == "rejected"
    # amending an existing goal at cap is still fine
    assert store.apply_goal(
        0, {"text": "revised", "goal_id": "g1"}, 1, 1000)["status"] == "accepted"


def test_prediction_validation():
    store = StrategyStore()
    assert store.apply_prediction(
        0, {"text": "settler founded by t18", "review_turn": 18},
        turn=10, seq=5)["prediction_id"] == "p1"
    # a claim about the past is untestable
    assert store.apply_prediction(
        0, {"text": "retro", "review_turn": 9}, 10, 6)["status"] == "rejected"
    assert store.apply_prediction(
        0, {"text": "long subject", "review_turn": 11,
            "subject_id": "x" * 17}, 10, 7)["status"] == "rejected"
    # amend moves review forward, closes the prior record
    store.apply_prediction(
        0, {"text": "by t20 actually", "review_turn": 20,
            "prediction_id": "p1"}, turn=12, seq=8)
    first, second = store.predictions[0]["p1"]
    assert first.valid_to_turn == 11 and second.revision == 2


def test_lesson_about_must_reference_own_claim():
    store = StrategyStore()
    store.apply_goal(0, {"text": "grow"}, turn=1, seq=1)
    store.apply_prediction(1, {"text": "rival plan", "review_turn": 5}, 1, 2)
    assert store.apply_lesson(
        0, {"text": "worked", "about": "g1"}, 2, 3)["lesson_id"] == "l1"
    assert store.apply_lesson(
        0, {"text": "dangling", "about": "g9"}, 2, 4)["status"] == "rejected"
    # another player's claim id is not one of ours (p1 belongs to player 1)
    assert store.apply_lesson(
        0, {"text": "stolen reference", "about": "p1"}, 2,
        5)["status"] == "rejected"


def test_claims_are_canonical_safe():
    store = StrategyStore()
    store.apply_goal(0, {"text": "hold", "metric": "cities", "target": 3,
                         "by_turn": 9, "confidence": 80}, 1, 1)
    store.apply_prediction(0, {"text": "pred", "review_turn": 5,
                               "subject_id": "g1"}, 2, 2)
    store.apply_lesson(0, {"text": "lesson", "about": "g1"}, 3, 3)
    for goal in store.goals[0].values():
        for rec in goal:
            canonical(rec.to_doc())  # rejects floats/tuples — must not raise
    for pred in store.predictions[0].values():
        for rec in pred:
            canonical(rec.to_doc())
    for lesson in store.lessons[0]:
        canonical(lesson.to_doc())


# ----------------------------------------------------------------- beliefs


def test_foreign_entities_persist_and_lru_caps():
    beliefs = BeliefStore()
    beliefs.see(0, turn=1, seq=1, foreign_units=[_fu("u5")])
    # seen at t1, never again — still there, staleness carried
    view = beliefs.view(0)
    assert view[0]["entity_id"] == "u5"
    assert view[0]["last_seen_turn"] == 1
    assert view[0]["fields"]["type"] == "WARRIOR"
    # 33 more DISTINCT units (u6..u38): 34 total evicts the two oldest
    for i in range(6, 39):
        beliefs.see(0, turn=1, seq=i, foreign_units=[_fu(f"u{i}")])
    ids = {e["entity_id"] for e in beliefs.view(0, limit=100)}
    assert len(ids) == 32 and "u5" not in ids and "u6" not in ids
    # re-seen entities move to the recent end and survive the next eviction
    beliefs.see(0, turn=2, seq=99, foreign_units=[_fu("u7")])
    beliefs.see(0, turn=2, seq=100, foreign_units=[_fu("u39")])
    ids = {e["entity_id"] for e in beliefs.view(0, limit=100)}
    assert "u7" in ids and "u8" not in ids and len(ids) == 32


def test_belief_view_orders_most_recent_first():
    beliefs = BeliefStore()
    beliefs.see(0, turn=3, seq=5, foreign_cities=[_fc("c9")])
    beliefs.see(0, turn=7, seq=9, foreign_units=[_fu("u2")])
    beliefs.see(0, turn=5, seq=7, foreign_units=[_fu("u3")])
    order = [e["entity_id"] for e in beliefs.view(0)]
    assert order == ["u2", "u3", "c9"]


# ------------------------------------------------------------------- facts


def test_facts_latest_sample_at_or_before_turn():
    facts = Facts()
    facts.note(0, 3, {"own_cities": 2})
    facts.note(0, 5, {"own_cities": 3, "gold": 40})
    facts.note(0, 5, {"gold": 55})  # same turn, later observation wins
    assert facts.value(0, "own_cities", 4) == 2
    assert facts.value(0, "own_cities", 5) == 3
    assert facts.value(0, "gold", 5) == 55
    assert facts.value(0, "gold", 4) is None
    assert facts.value(1, "gold", 5) is None


# ---------------------------------------------------------------- from_log


def _live_store() -> StrategyStore:
    """Claims + observations applied the way the referee will apply them
    (created_seq = the claim TOOL_CALL's seq; sighting seq = the observation
    TOOL_RESULT's seq — exactly what from_log derives from the records)."""
    store = StrategyStore()
    store.apply_goal(0, {"text": "hold 3 cities", "metric": "cities",
                         "target": 3, "by_turn": 20}, turn=1, seq=4)
    store.apply_prediction(0, {"text": "rival walls by t12",
                               "review_turn": 12}, turn=1, seq=6)
    store.apply_lesson(0, {"text": "check production every turn",
                           "about": "g1"}, turn=2, seq=10)
    store.note_observation(0, 1, 3, "get_overview",
                           {"gold": 60, "techs": 1, "researching": "MINING"})
    store.note_observation(0, 1, 9, "get_units",
                           {"own_units": 3, "foreign_units": [_fu("u8")]})
    store.note_observation(0, 2, 13, "get_cities",
                           {"own_cities": 1, "own_population": 3,
                            "foreign_cities": [_fc("c4")]})
    return store


def _log_records() -> list[dict[str, Any]]:
    return [
        call_rec("get_overview", {}, seq=2),
        result_rec("get_overview", seq=3, observed={"gold": 60, "techs": 1,
                                                    "researching": "MINING"}),
        call_rec("set_goal", {"text": "hold 3 cities", "metric": "cities",
                              "target": 3, "by_turn": 20}, seq=4),
        result_rec("set_goal", seq=5),
        call_rec("record_prediction", {"text": "rival walls by t12",
                                       "review_turn": 12}, seq=6),
        result_rec("record_prediction", seq=7),
        call_rec("get_units", {}, seq=8),
        result_rec("get_units", seq=9, observed={
            "own_units": 3, "foreign_units": [_fu("u8")]}),
        call_rec("record_lesson", {"text": "check production every turn",
                                   "about": "g1"}, turn=2, seq=10),
        result_rec("record_lesson", turn=2, seq=11),
        call_rec("get_cities", {}, turn=2, seq=12),
        result_rec("get_cities", turn=2, seq=13, observed={
            "own_cities": 1, "own_population": 3, "foreign_cities": [_fc("c4")]}),
    ]


def test_from_log_equals_live_store():
    rebuilt = StrategyStore.from_log(_log_records())
    assert rebuilt == _live_store()  # claims, beliefs AND facts, structurally


def test_from_log_prefixes_rebuild_incrementally():
    records = _log_records()
    live = _live_store()
    # through the lesson pair (indices 0..9): goals, prediction, u8 belief
    # and the lesson are in; the c4 sighting (indices 10..11) is not yet
    partial = StrategyStore.from_log(records[:10])
    assert partial.goals == live.goals
    assert partial.beliefs.entries[0]["u8"]["last_seen_turn"] == 1
    assert "c4" not in partial.beliefs.entries[0]
    assert len(partial.lesson_list(0)) == 1
    assert partial.facts.value(0, "own_cities", 2) is None  # cities not yet seen


def test_from_log_requires_namespace_identity_for_claims():
    records = _log_records()
    # the accepted result belongs to ANOTHER player: the claim must not apply
    records[5] = {**records[5], "player_id": 1, "agent_id": "korea"}
    rebuilt = StrategyStore.from_log(records)
    assert rebuilt.predictions == {}
    assert rebuilt.goals == _live_store().goals  # untouched claim still applies


def test_from_log_ignores_rejected_and_dangling_claims():
    records = _log_records()
    # a rejected set_goal pair
    records.append(call_rec("set_goal", {"text": "nope"}, turn=3, seq=14))
    records.append(result_rec("set_goal", turn=3, seq=15, status="rejected"))
    # a dangling call torn off by a crash (no adjacent result)
    records.append(call_rec("record_lesson", {"text": "torn"}, turn=3, seq=16))
    rebuilt = StrategyStore.from_log(records)
    assert len(rebuilt.lesson_list(0)) == 1
    assert len(rebuilt.current_goals(0)) == 1


def test_from_log_ignores_rejected_observations():
    records = _log_records()
    records.append(call_rec("get_units", {}, turn=3, seq=14))
    records.append(result_rec("get_units", turn=3, seq=15, status="rejected",
                              observed={"own_units": 9,
                                        "foreign_units": [_fu("u99")]}))
    rebuilt = StrategyStore.from_log(records)
    assert "u99" not in rebuilt.beliefs.entries[0]
    assert rebuilt.facts.value(0, "own_units", 3) == 3  # not the rejected 9


def test_from_log_pre_m11_records_rebuild_empty_valid():
    """Logs written before the strategy plane have no `observed` digests and
    no claim tools: the rebuild is an empty but perfectly valid store."""
    records = [
        call_rec("get_units", {}, seq=2),
        result_rec("get_units", seq=3),  # no observed field at all
        call_rec("end_turn", {}, seq=4),
        result_rec("end_turn", seq=5),
    ]
    store = StrategyStore.from_log(records)
    assert store == StrategyStore()


# ------------------------------------------------- referee-level digests


async def test_observation_digest_logged_and_live_fed(tmp_path):
    from conftest import teleport
    from test_hostile_agent import hostile_setup

    adapter, log, referee, _session, ctx, _lease = await hostile_setup(tmp_path)
    # seed 4: players start far apart; walk an enemy warrior into our sight
    teleport(adapter.state, "u8", -2, 0)
    units = await referee.observe(ctx, ObserveKind.UNITS)
    visible_foreign = [u for u in units if u.get("owner_id") != 0]
    assert {u["unit_id"] for u in visible_foreign} == {"u8"}

    result = next(r for r in log.records()
                  if r["kind"] == "TOOL_RESULT" and r.get("tool") == "get_units")
    digest = result["observed"]
    assert digest["own_units"] == 5
    assert [u["unit_id"] for u in digest["foreign_units"]] == ["u8"]
    # the belief's sighting provenance IS the record carrying the digest
    belief = referee.strategy.beliefs.entries[0]["u8"]
    assert belief["last_seen_seq"] == result["seq"]
    assert set(belief["fields"]) <= FOREIGN_UNIT_FIELDS  # never more than seen

    await referee.observe(ctx, ObserveKind.OVERVIEW)
    assert referee.strategy.facts.value(0, "gold", 1) is not None
    # live store == rebuild over the same prefix, structurally
    assert StrategyStore.from_log(log.records()) == referee.strategy


async def test_foreign_entity_persists_after_leaving_sight(tmp_path):
    from conftest import teleport
    from test_hostile_agent import hostile_setup

    adapter, _log, referee, _session, ctx, _lease = await hostile_setup(tmp_path)
    teleport(adapter.state, "u8", -2, 0)
    units = await referee.observe(ctx, ObserveKind.UNITS)
    assert any(u["unit_id"] == "u8" for u in units)
    # the enemy walks home, out of our sight: ABSENT from the projection...
    teleport(adapter.state, "u8", 3, -2)
    units = await referee.observe(ctx, ObserveKind.UNITS)
    assert not any(u["unit_id"] == "u8" for u in units)
    # ...but the last-known belief persists, staleness-labeled by turn
    belief = referee.strategy.beliefs.entries[0]["u8"]
    assert belief["fields"]["coord"] == "-2,0"
    assert belief["last_seen_turn"] == 1


async def test_untracked_observations_carry_no_digest(tmp_path):
    from test_hostile_agent import hostile_setup

    _adapter, log, referee, _session, ctx, _lease = await hostile_setup(tmp_path)
    await referee.observe(ctx, ObserveKind.VISIBLE_MAP)
    await referee.observe(ctx, ObserveKind.AVAILABLE_RESEARCH)
    for rec in log.records():
        if rec["kind"] == "TOOL_RESULT":
            assert "observed" not in rec, rec.get("tool")


async def test_replayed_observations_rebuild_identical_store(tmp_path):
    """Live and replayed logs carry identical digests — the replay machinery
    re-derives the same projections over the same re-executed state."""
    spec = MatchSpec(
        match_id="strategy-replay", seed=424242, max_turns=3, adapter="simulator",
        watchdog_mode="flag_and_continue", violation_limit=5, checkpoint_every=1,
        agents=[
            AgentSpec(agent_id="roman", player_id=0, policy="expansionist",
                      seed=11),
            AgentSpec(agent_id="korea", player_id=1, policy="turtler", seed=22),
        ],
    )
    arena = Arena(tmp_path / "run", spec)
    await arena.run()
    # the scripted bots observe every turn, so the store is non-trivial
    assert arena.referee.strategy.facts.samples, "scripted bots must have observed"
    result = await replay_run(tmp_path / "run", spec, tmp_path / "replay")
    assert result["identical"], (
        f"replay diverged at comparable-event {result['first_divergence']}")

    import json

    replay_records = [
        json.loads(line)
        for line in (tmp_path / "replay" / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    live_store = StrategyStore.from_log(arena.log.records())
    replay_store = StrategyStore.from_log(replay_records)
    assert live_store == replay_store
    assert live_store.beliefs.entries  # the comparison is over real sightings


# ------------------------------------------------- claim tools (referee)


async def test_claim_tools_accepted_logged_and_rebuildable(tmp_path):
    from test_hostile_agent import hostile_setup

    _adapter, log, referee, _session, ctx, _lease = await hostile_setup(tmp_path)
    doc = await referee.set_goal(ctx, "hold 3 cities", by_turn=20,
                                 metric="cities", target=3)
    assert doc == {"status": "accepted", "tool": "set_goal", "goal_id": "g1",
                   "revision": 1}
    amend = await referee.set_goal(ctx, "hold 4 cities", goal_id="g1",
                                   by_turn=24, metric="cities", target=4)
    assert amend["revision"] == 2
    assert (await referee.record_prediction(
        ctx, "rival walls by t12", 12))["prediction_id"] == "p1"
    assert (await referee.record_lesson(
        ctx, "check production every turn", "g1"))["lesson_id"] == "l1"

    # args are logged RAW — exactly what the caller sent (Python defaults
    # filled by the signature count as sent for direct calls; the runtime
    # always sends the full kwargs set it was given)
    call = [r for r in log.records()
            if r["kind"] == "TOOL_CALL" and r.get("tool") == "set_goal"][-1]
    assert call["args"]["text"] == "hold 4 cities"
    assert call["args"]["by_turn"] == 24
    # provenance: created_seq IS the TOOL_CALL's own seq
    assert referee.strategy.goals[0]["g1"][-1].created_seq == call["seq"]
    # live store == rebuild, through the shared apply_claim path
    assert StrategyStore.from_log(log.records()) == referee.strategy
    # telemetry parity: one call counted per emitted pair
    results = [r for r in log.records() if r["kind"] == "TOOL_RESULT"]
    assert referee.telemetry.snapshot()["roman"]["total_calls"] == len(results)


async def test_claim_tools_require_valid_lease(tmp_path):
    from test_hostile_agent import hostile_setup

    _adapter, log, referee, _session, ctx, lease = await hostile_setup(tmp_path)
    lease.release()
    doc = await referee.set_goal(ctx, "too late")
    assert doc["status"] == "rejected" and doc["rejection"] == "lease_expired"
    kinds = [(r["kind"], r.get("rejection")) for r in log.records()]
    assert ("UNAUTHORIZED_TOOL_CALL", "lease_expired") in kinds
    assert ("TOOL_RESULT", "lease_expired") in kinds
    assert referee.strategy.goals == {}  # nothing landed


async def test_claim_rejections_never_land_in_store(tmp_path):
    from test_hostile_agent import hostile_setup

    _adapter, log, referee, _session, ctx, _lease = await hostile_setup(tmp_path)
    assert (await referee.set_goal(ctx, "ok"))["status"] == "accepted"
    for doc in [
        await referee.set_goal(ctx, "x" * 281),               # oversized raw
        await referee.set_goal(ctx, "ok", metric="happiness"),  # bad metric
        await referee.set_goal(ctx, "ok", by_turn=0, confidence=101),
        await referee.record_prediction(ctx, "retro", 0),     # review in past
        await referee.record_lesson(ctx, "dangling", "g9"),   # unknown about
    ]:
        assert doc["status"] == "rejected" and doc["rejection"] == "args_invalid"
    # one accepted goal, nothing else; rejections are on the record
    assert len(referee.strategy.current_goals(0)) == 1
    rejected = [r for r in log.records()
                if r["kind"] == "TOOL_RESULT" and r.get("status") == "rejected"
                and r.get("rejection") == "args_invalid"]
    assert len(rejected) == 5


async def test_claim_tools_never_touch_game_state(tmp_path):
    from test_hostile_agent import hostile_setup

    adapter, log, referee, _session, ctx, _lease = await hostile_setup(tmp_path)
    before = adapter.state_hash()
    await referee.set_goal(ctx, "note")
    await referee.record_prediction(ctx, "pred", 2)
    await referee.record_lesson(ctx, "lesson")
    await referee.get_strategy(ctx)
    assert adapter.state_hash() == before
    for rec in log.records():
        if rec["kind"] == "TOOL_RESULT" and rec.get("tool") in (
                "set_goal", "record_prediction", "record_lesson",
                "get_strategy"):
            for key in ("after_state_hash", "before_state_hash", "receipts",
                        "mutations"):
                assert key not in rec, f"{rec['tool']} must not carry {key}"


async def test_get_strategy_round_trips_the_view(tmp_path):
    from conftest import teleport
    from test_hostile_agent import hostile_setup

    adapter, log, referee, _session, ctx, _lease = await hostile_setup(tmp_path)
    teleport(adapter.state, "u8", -2, 0)
    await referee.observe(ctx, ObserveKind.UNITS)
    await referee.set_goal(ctx, "watch the north", metric="units", target=5)
    doc = await referee.get_strategy(ctx)
    assert doc["status"] == "accepted"
    # the same docs the store serves the renderer — one shape, no drift
    assert doc["strategy"] == referee.strategy.view_for(0)
    assert doc["strategy"]["goals"][0]["goal_id"] == "g1"
    assert doc["strategy"]["beliefs"][0]["entity_id"] == "u8"
    # the payload is NOT lifted into the log record (derivable, like any
    # observation payload)
    result = next(r for r in log.records()
                  if r["kind"] == "TOOL_RESULT" and r.get("tool") ==
                  "get_strategy")
    assert result["result_doc"] is None


# ------------------------------------------- claim tools through the model


async def test_oversized_model_claim_is_malformed_and_replay_stable(tmp_path):
    from fakes import FakeModel, use
    from test_llm_runtime import _run, make_arena

    fake = FakeModel(script=[
        [use("set_goal", {"text": "x" * 281, "by_turn": 20})],
        [use("end_turn")],
    ])
    arena, _fake = await _run(make_arena(tmp_path, fake, max_turns=1))
    # bounded BEFORE any log record: zero events, replay cannot diverge
    kinds = [(r["kind"], r.get("tool")) for r in arena.log.records()]
    assert ("TOOL_CALL", "set_goal") not in kinds
    assert ("TOOL_RESULT", "set_goal") not in kinds
    tel = arena.telemetry.snapshot()["roman"]
    assert tel["model_errors"] == {"llm_malformed_args": 1}
    result = await replay_run(tmp_path / "run", arena.spec, tmp_path / "replay")
    assert result["identical"]


# ------------------------------------------------------------- scoring


def _scored_store() -> StrategyStore:
    store = StrategyStore()
    store.facts.note(0, 3, {"own_cities": 2, "gold": 40})
    store.facts.note(0, 5, {"own_cities": 3, "gold": 55})
    store.apply_goal(0, {"text": "hold 3 cities", "by_turn": 5,
                         "metric": "cities", "target": 3}, 1, 1)
    store.apply_goal(0, {"text": "save 500 gold", "by_turn": 5,
                         "metric": "gold", "target": 500}, 1, 2)
    store.apply_goal(0, {"text": "vague intention"}, 1, 3)  # no deadline
    store.apply_prediction(0, {"text": "rival walls soon", "review_turn": 5,
                               "subject_id": "c4"}, 1, 4)
    store.apply_prediction(0, {"text": "3 cities by t5", "review_turn": 5,
                               "metric": "cities", "target": 3}, 1, 5)
    store.apply_prediction(0, {"text": "not due yet", "review_turn": 9}, 1, 6)
    return store


def test_verdicts_met_missed_self_assess():
    from civ_arena.strategy import scoring

    store = _scored_store()
    facts = store.facts
    met, missed, vague = store.current_goals(0)
    assert scoring.verdict(met, facts, 0, 5) == "met"       # cities 3 >= 3
    assert scoring.verdict(missed, facts, 0, 5) == "missed"  # gold 55 < 500
    assert scoring.verdict(vague, facts, 0, 5) == "self_assess"  # no metric
    # a subject the arena cannot see is never auto-scored
    rival, scored, _later = store.current_predictions(0)
    assert scoring.verdict(rival, facts, 0, 5) == "self_assess"
    assert scoring.verdict(scored, facts, 0, 5) == "met"
    # metric value resolves through the facts key mapping, latest <= turn
    assert scoring.metric_value(facts, 0, "cities", 4) == 2
    assert scoring.metric_value(facts, 0, "cities", 5) == 3
    assert scoring.metric_value(facts, 0, "cities", 2) is None


def test_due_selection_overdue_and_open():
    from civ_arena.strategy import scoring

    store = _scored_store()
    assert {g.goal_id for g in scoring.due_goals(store, 0, 5)} == {"g1", "g2"}
    # overdue stays visible: a skipped review does not silently resolve
    assert {g.goal_id for g in scoring.due_goals(store, 0, 7)} == {"g1", "g2"}
    assert {p.prediction_id
            for p in scoring.due_predictions(store, 0, 5)} == {"p1", "p2"}
    # nothing due before the deadlines
    assert scoring.due_goals(store, 0, 4) == []
    assert scoring.due_predictions(store, 0, 4) == []


def test_metrics_follow_observation_not_ambient():
    """A captured own city drops out of the next observation digest, so the
    metric drops — the store never folds referee-scope AMBIENT economy."""
    store = StrategyStore()
    store.apply_goal(0, {"text": "hold 2 cities", "by_turn": 6,
                         "metric": "cities", "target": 2}, 1, 1)
    store.note_observation(0, 4, 10, "get_cities",
                           {"own_cities": 2, "own_population": 5,
                            "foreign_cities": []})
    store.note_observation(0, 6, 20, "get_cities",
                           {"own_cities": 1, "own_population": 3,
                            "foreign_cities": []})
    from civ_arena.strategy import scoring

    assert scoring.verdict(store.current_goals(0)[0], store.facts, 0, 6) \
        == "missed"


# ------------------------------------------------------------- renderer


def test_render_memory_sections_and_identity_freedom():
    from civ_arena.strategy.view import render_memory

    store = _scored_store()
    store.apply_lesson(0, {"text": "check production every turn",
                           "about": "g1"}, 4, 30)
    store.beliefs.see(0, 3, 9, foreign_units=[_fu("u8")])
    text = render_memory(store, 0, 5)
    assert "REVIEW DUE THIS TURN" in text
    assert "g1" in text and "MET (cities=3)" in text
    assert "MISSED (gold=55)" in text
    assert "SELF-ASSESS" in text
    assert "GOALS (active)" in text
    assert "LAST SEEN (may be stale)" in text
    assert "u8 WARRIOR @ 2,0 hp2 (t3)" in text
    assert "LESSONS" in text
    # identity-free: no agent/match/lease vocabulary ever reaches a prompt
    low = text.lower()
    for secret in ("player_id", "agent_id", "match_id", "lease",
                   "game_instance"):
        assert secret not in low
    # empty store renders nothing — the header omits the block entirely
    assert render_memory(StrategyStore(), 0, 5) == ""


# --------------------------------------------- memory view through the model


async def test_memory_view_flows_into_turn_header(tmp_path):
    from fakes import FakeModel, use
    from test_llm_runtime import _run, make_arena

    fake = FakeModel(script=[
        [use("get_cities"), use("set_goal", {
            "text": "have a city", "by_turn": 1, "metric": "cities",
            "target": 1}), use("end_turn")],
        [use("end_turn")],
    ])
    arena, _fake = await _run(make_arena(tmp_path, fake, max_turns=2))
    header2 = fake.requests[1]["messages"][0]["content"]
    assert "REVIEW DUE THIS TURN" in header2
    assert "g1" in header2
    assert "cities=0" in header2  # no city founded turn 1: honest verdict
    assert "Your diary:" in header2  # the diary still rides the header
    # and the match still replays model-free with claims in the log
    result = await replay_run(tmp_path / "run", arena.spec, tmp_path / "replay")
    assert result["identical"]


# ------------------------------------------------------ arena + resume


class StrategyBot:
    """Observes, writes one goal/prediction/lesson per turn, then ends."""

    def __init__(self) -> None:
        self.rng = random.Random(0)
        self.turn = 0

    def begin_turn(self, turn: int) -> None:
        self.turn = int(turn)

    async def take_turn(self, facade: Any) -> None:
        await facade.get_units()
        await facade.get_overview()
        await facade.set_goal(f"goal set on turn {self.turn}",
                              by_turn=self.turn, metric="units", target=1)
        await facade.record_prediction(f"prediction of turn {self.turn}",
                                       self.turn)
        await facade.record_lesson(f"lesson of turn {self.turn}")
        await facade.end_turn()


class EndBot:
    """Never writes a claim — whatever the resumed arena holds came from the
    from_log REBUILD, not from this runtime."""

    def __init__(self) -> None:
        self.rng = random.Random(0)

    async def take_turn(self, facade: Any) -> None:
        await facade.end_turn()


def _strategy_spec(match_id: str, max_turns: int) -> MatchSpec:
    return MatchSpec(
        match_id=match_id, seed=424242, max_turns=max_turns,
        adapter="simulator", watchdog_mode="flag_and_continue",
        violation_limit=5, checkpoint_every=1,
        agents=[
            AgentSpec(agent_id="roman", player_id=0, policy="expansionist",
                      seed=11),
            AgentSpec(agent_id="korea", player_id=1, policy="turtler",
                      seed=22),
        ],
    )


async def test_strategy_survives_resume(tmp_path):
    import json

    from civ_arena.arena.checkpoints import CheckpointState

    spec = _strategy_spec("strategy-resume", 4)
    arena = Arena(tmp_path / "run", spec,
                  runtimes={0: StrategyBot(), 1: StrategyBot()})
    await arena.run()
    # the arena owns ONE store shared with the referee (one trust root)
    assert arena.referee.strategy is arena.strategy
    assert len(arena.strategy.current_goals(0)) == 4  # one per turn

    ckpt = CheckpointState.from_doc(json.loads(
        (tmp_path / "run" / "checkpoints" / "ckpt-turn-0002.json").read_text()))
    prefix_store = StrategyStore.from_log(arena.log.records()[: ckpt.seq])
    assert len(prefix_store.current_goals(0)) == 2

    # resume with runtimes that never write: the store the resumed arena
    # holds can ONLY have come from the from_log rebuild in _resume_from
    resumed = Arena(tmp_path / "run", spec,
                    runtimes={0: EndBot(), 1: EndBot()})
    summary = await resumed.run(resume_state=ckpt)
    assert summary["final_turn"] == 4
    assert resumed.referee.strategy is resumed.strategy
    assert len(resumed.strategy.current_goals(0)) == 2
    texts = {g.text for g in resumed.strategy.current_goals(0)}
    assert texts == {"goal set on turn 1", "goal set on turn 2"}
    # observation-derived state from the prefix survived too (no foreign
    # sightings exist at this seed yet — beliefs stay empty, correctly)
    assert resumed.strategy.facts.samples
    assert resumed.strategy.facts.value(0, "own_units", 2) == 5


async def test_resume_equals_uninterrupted_memory_view(tmp_path):
    """The invariant that rules out snapshot-approximation beliefs: a turn-3
    header produced after a crash-resume is byte-identical to one from an
    uninterrupted run — the rebuilt store must be EXACTLY the live one."""
    from fakes import FakeModel, use
    from test_llm_runtime import _run, make_arena

    script = [[
        use("get_overview"),
        use("set_goal", {"text": "grow the army", "by_turn": 2,
                         "metric": "units", "target": 3}),
        use("record_prediction", {"text": "scouting pays off",
                                  "review_turn": 2}),
        use("end_turn"),
    ]]

    clean, clean_fake = await _run(make_arena(
        tmp_path / "clean", FakeModel(script=list(script)), max_turns=3,
        match_id="header-parity"))
    header_of = lambda fake, turn: next(  # noqa: E731
        req["messages"][0]["content"] for req in fake.requests
        if req["messages"][0]["content"].startswith(f"Turn {turn} begins."))
    clean_header3 = header_of(clean_fake, 3)
    assert "REVIEW DUE THIS TURN" in clean_header3  # the view is non-trivial

    crashed, _fake = await _run(make_arena(
        tmp_path / "crashed", FakeModel(script=list(script)), max_turns=3,
        match_id="header-parity"))

    import json

    from civ_arena.arena.checkpoints import CheckpointState

    ckpt = CheckpointState.from_doc(json.loads(
        (tmp_path / "crashed" / "run" / "checkpoints" / "ckpt-turn-0002.json")
        .read_text()))
    resumed_fake = FakeModel(script=list(script))
    from civ_arena.agents.llm.runtime import LLMAgentRuntime
    from civ_arena.agents.runtime import AgentProfile

    # rebuild an injected LLM runtime over the crashed run dir, mirroring
    # make_arena's post-hoc wiring, then resume from turn 2
    spec = crashed.spec
    llm = next(a for a in spec.agents if a.policy == "llm").llm
    profile = AgentProfile(agent_id="roman", player_id=0, policy="llm",
                           seed=11, llm=llm)
    rt = LLMAgentRuntime.build(profile, client=resumed_fake)
    resumed = Arena(tmp_path / "crashed" / "run", spec, runtimes={0: rt})
    rt.telemetry = resumed.telemetry
    rt.diary = resumed.diary
    rt.strategy = resumed.strategy
    await resumed.run(resume_state=ckpt)
    resumed_header3 = header_of(resumed_fake, 3)
    assert resumed_header3 == clean_header3


# ------------------------------------------- Codex round 1 pins (11 findings)


async def test_own_city_digest_is_own_not_foreign(tmp_path):
    """Codex R1 #1: own-city projections carry `owner` (deep-copied raw
    doc), foreign ones carry `owner_id` — the split must normalize both, or
    every own city lands in the foreign list with FULL fields (allowlist
    violation) and own_cities/own_population read 0."""
    from test_hostile_agent import hostile_setup

    _adapter, log, referee, _session, ctx, _lease = await hostile_setup(tmp_path)
    doc = await ctx.referee.execute(ctx, "found_city", {"unit_id": "u1"})
    assert doc["status"] == "accepted", doc
    cities = await referee.observe(ctx, ObserveKind.CITIES)
    assert any(c.get("owner", c.get("owner_id")) == 0 for c in cities)
    result = next(r for r in log.records()
                  if r["kind"] == "TOOL_RESULT" and r.get("tool") ==
                  "get_cities" and r.get("status") == "accepted")
    digest = result["observed"]
    assert digest["own_cities"] == 1
    assert digest["own_population"] >= 1
    assert digest["foreign_cities"] == []  # the own city is NOT a belief
    assert referee.strategy.beliefs.entries.get(0, {}) == {}
    assert referee.strategy.facts.value(0, "own_cities", 1) == 1
    assert StrategyStore.from_log(log.records()) == referee.strategy


async def test_digest_rejects_non_canonical_values():
    """Codex R1 #8: a non-canonical digest value (a float from a hostile
    adapter on the live leg) must fail at BUILD time — before any log
    record is fsync'd, not at the next checkpoint prefix hash. Only FOREIGN
    entries embed fields, so the poison rides a foreign entity."""
    import pytest

    from civ_arena.canonical import CanonicalError
    from civ_arena.strategy.digest import observation_digest

    poisoned = [{"unit_id": "u9", "owner_id": 1, "hp": 1.5}]
    with pytest.raises(CanonicalError):
        observation_digest("units", poisoned, 0)


def test_from_log_requires_typed_namespace():
    """Codex R1 #2: equal-None identity pairs must not authorize a claim,
    and an untyped/referee-scope observation result must not inject facts."""
    records = _log_records()
    # strip identity to None on BOTH sides of the set_goal pair
    stripped = []
    for rec in records:
        if rec.get("tool") == "set_goal":
            rec = {**rec, "agent_id": None, "match_id": None,
                   "game_instance_id": None}
        stripped.append(rec)
    assert StrategyStore.from_log(stripped).goals == {}

    injected = _log_records() + [
        result_rec("get_overview", seq=20, observed={"gold": 999},
                   visibility_scope="referee"),
        result_rec("get_overview", seq=21, observed={"gold": 999},
                   agent_id=None),
    ]
    store = StrategyStore.from_log(injected)
    # the legitimate turn-1 sample survived; the injected 999 did not land
    assert store.facts.value(0, "gold", 9) == 60
    assert 999 not in {v for t in store.facts.samples.get(0, {}).values()
                       for v in t.values()}


def test_verdict_is_scored_as_of_the_deadline():
    """Codex R1 #3: later observations must never flip a verdict
    retroactively — a goal missed at its deadline stays missed."""
    from civ_arena.strategy import scoring

    store = StrategyStore()
    store.apply_goal(0, {"text": "save gold", "by_turn": 5,
                         "metric": "gold", "target": 100}, 1, 1)
    store.facts.note(0, 5, {"gold": 50})
    store.facts.note(0, 6, {"gold": 150})
    goal = store.current_goals(0)[0]
    assert scoring.verdict(goal, store.facts, 0, 5) == "missed"
    assert scoring.verdict(goal, store.facts, 0, 6) == "missed"  # sticky


def test_lesson_about_due_prediction_resolves_it():
    """Codex R1 #4: recording the instructed lesson IS the verdict — it
    closes the due prediction's review; amending re-opens it."""
    from civ_arena.strategy import scoring

    store = StrategyStore()
    store.apply_prediction(0, {"text": "walls by t5", "review_turn": 3}, 1, 1)
    assert scoring.due_predictions(store, 0, 3)
    doc = store.apply_lesson(0, {"text": "no walls seen", "about": "p1"},
                             3, 2)
    assert doc == {"status": "accepted", "tool": "record_lesson",
                   "lesson_id": "l1", "resolved": "p1"}
    assert scoring.due_predictions(store, 0, 3) == []
    assert scoring.due_predictions(store, 0, 9) == []  # stays closed
    # a lesson about a NOT-yet-due prediction resolves nothing
    store.apply_prediction(0, {"text": "later claim", "review_turn": 8}, 3, 3)
    doc = store.apply_lesson(0, {"text": "too early", "about": "p2"}, 3, 4)
    assert "resolved" not in doc
    assert scoring.due_predictions(store, 0, 8)  # still due when it lands
    # amend re-opens with the new revision (review_turn can only move
    # forward: a past review turn is rejected at authorship)
    store.apply_prediction(0, {"text": "revised", "review_turn": 5,
                               "prediction_id": "p1"}, 4, 5)
    assert scoring.due_predictions(store, 0, 4) == []  # not due until t5
    assert scoring.due_predictions(store, 0, 5)


def test_rejected_amend_creates_no_bucket():
    """Codex R1 #5: a rejected amendment must not mutate derived state —
    live {0:{}} vs rebuilt {} would break structural equality."""
    store = StrategyStore()
    assert store.apply_goal(0, {"text": "ok", "goal_id": "g9"},
                            1, 1)["status"] == "rejected"
    assert store.apply_prediction(0, {"text": "ok", "review_turn": 2,
                                      "prediction_id": "p9"},
                                  1, 2)["status"] == "rejected"
    assert store.apply_lesson(0, {"text": "ok", "about": "g9"},
                              1, 3)["status"] == "rejected"
    assert store.goals == {} and store.predictions == {}
    assert store.lessons == {}


def test_dropped_goal_frees_a_cap_slot():
    """Codex R1 #6: the cap counts undropped goals — dropping one frees its
    slot (the id is never reused), exactly as the error text promises."""
    store = StrategyStore()
    for i in range(MAX_GOALS_PER_PLAYER):
        store.apply_goal(0, {"text": f"g{i}"}, 1, i)
    assert store.apply_goal(
        0, {"text": "full"}, 1, 99)["status"] == "rejected"
    assert store.apply_goal(
        0, {"text": "bye", "goal_id": "g1", "status": "dropped"},
        1, 100)["status"] == "accepted"
    assert store.apply_goal(
        0, {"text": "new slot"}, 1, 101)["goal_id"] == "g33"  # ids monotone


def test_subject_annotation_does_not_block_scoring():
    """Codex R1 #7: a metric measures the player's own state regardless of
    the subject annotation — subject_id is a label, not a scoring veto."""
    from civ_arena.strategy import scoring

    store = StrategyStore()
    store.apply_prediction(0, {"text": "about g1: gold grows",
                               "review_turn": 4, "subject_id": "g1",
                               "metric": "gold", "target": 10}, 1, 1)
    store.facts.note(0, 4, {"gold": 20})
    assert scoring.verdict(store.current_predictions(0)[0],
                           store.facts, 0, 4) == "met"


async def test_oversized_reference_field_is_malformed_zero_events(tmp_path):
    """Codex R1 #9: the 16-char bound on reference fields is enforced at
    the runtime, BEFORE any hash/fsync/record."""
    from fakes import FakeModel, use
    from test_llm_runtime import _run, make_arena

    fake = FakeModel(script=[
        [use("record_lesson", {"text": "ok", "about": "x" * 17})],
        [use("end_turn")],
    ])
    arena, _ = await _run(make_arena(tmp_path, fake, max_turns=1))
    kinds = [(r["kind"], r.get("tool")) for r in arena.log.records()]
    assert ("TOOL_CALL", "record_lesson") not in kinds
    assert arena.telemetry.snapshot()["roman"]["model_errors"] == {
        "llm_malformed_args": 1}


async def test_injected_runtime_gets_memory_without_manual_wiring(tmp_path):
    """Codex R2 #8 (superseding the R1 #10 pin, which was vacuous —
    make_arena patched rt.strategy itself): construct the runtime and arena
    DIRECTLY, assert the arena wired the store before any turn runs, then
    prove the memory view flows without a single manual assignment."""
    from civ_arena.agents.llm.runtime import LLMAgentRuntime
    from civ_arena.agents.runtime import AgentProfile
    from civ_arena.config import LLMSpec
    from fakes import FakeModel, use
    from test_llm_runtime import FAKE_LLM

    fake = FakeModel(script=[
        [use("set_goal", {"text": "grow", "by_turn": 9}),
         use("end_turn")],
        [use("end_turn")],
    ])
    spec = MatchSpec(
        match_id="wired-injected", seed=424242, max_turns=2,
        adapter="simulator", watchdog_mode="flag_and_continue",
        violation_limit=5, checkpoint_every=1,
        agents=[
            AgentSpec(agent_id="roman", player_id=0, policy="llm", seed=11,
                      llm=LLMSpec(**{
                          "base_url": FAKE_LLM.base_url,
                          "api_key_env": FAKE_LLM.api_key_env,
                          "model_id": FAKE_LLM.model_id})),
            AgentSpec(agent_id="korea", player_id=1, policy="turtler",
                      seed=22),
        ],
    )
    profile = AgentProfile(agent_id="roman", player_id=0, policy="llm",
                           seed=11, llm=FAKE_LLM)
    rt = LLMAgentRuntime.build(profile, client=fake)  # NO strategy passed
    arena = Arena(tmp_path / "run", spec, runtimes={0: rt})
    # the ARENA wired it (bind_services), not this test
    assert rt.strategy is arena.strategy
    assert rt.diary is arena.diary
    await arena.run()
    header2 = fake.requests[1]["messages"][0]["content"]
    assert "grow" in header2


def test_goal_reactivation_reconsumes_a_cap_slot():
    """Codex R2 #1: drop/create/reactivate must not grow past the cap — the
    amend path re-checks when a dropped goal comes back to life."""
    store = StrategyStore()
    for i in range(MAX_GOALS_PER_PLAYER):
        store.apply_goal(0, {"text": f"g{i}"}, 1, i)
    store.apply_goal(0, {"text": "dropped", "goal_id": "g1",
                         "status": "dropped"}, 1, 100)
    assert store.apply_goal(0, {"text": "slot"}, 1,
                            101)["status"] == "accepted"  # 32 undropped
    # the 33rd undropped goal — via reactivation — is refused
    assert store.apply_goal(
        0, {"text": "zombie", "goal_id": "g1", "status": "active"},
        1, 102)["status"] == "rejected"


def test_from_log_rejects_result_side_and_bool_identity():
    """Codex R2 #2: the RESULT record carries its own namespace — a
    referee-scope result must not authorize an adjacent valid call, and a
    JSON true must not alias player 1 as a bool-int."""
    records = _log_records()
    # valid call, referee-scope result: the claim must not apply
    doctored = []
    for rec in records:
        if rec.get("kind") == "TOOL_RESULT" and rec.get("tool") == \
                "set_goal":
            rec = {**rec, "visibility_scope": "referee"}
        doctored.append(rec)
    assert StrategyStore.from_log(doctored).goals == {}
    # bool player_id: type(...) is int rejects True (which == 1)
    boolified = []
    for rec in records:
        if "player_id" in rec:
            rec = {**rec, "player_id": True, "phase_player_id": True}
        boolified.append(rec)
    store = StrategyStore.from_log(boolified)
    assert store.goals == {}
    assert store.facts.samples == {}


async def test_digest_rejects_conflicting_ownership_keys():
    """Codex R2 #3: an entry carrying disagreeing owner/owner_id keys is a
    broken policy/adapter — embedding it wholesale would leak full own
    fields into the foreign list, so the split fails loudly instead."""
    import pytest

    from civ_arena.strategy.digest import observation_digest

    hybrid = [{"city_id": "c9", "owner": 0, "owner_id": 1,
               "population": 3, "buildings": ["WALLS"]}]
    with pytest.raises(ValueError):
        observation_digest("cities", hybrid, 0)


def test_overflow_due_goals_stay_visible_in_goals():
    """Codex R3 #1/#5 (superseding the R2 #4 pin, whose count assertion
    double-counted): with more due claims than the REVIEW item cap, every
    claim id stays visible somewhere — full lines for the earliest six
    (goals and predictions interleaved by deadline, so predictions are
    never starved out by goals), the overflow ids in a summary line and in
    GOALS."""
    from civ_arena.strategy.view import render_memory

    store = StrategyStore()
    store.facts.note(0, 3, {"cities": 2, "gold": 5})
    for i in range(1, 9):  # eight due goals, deadline t3
        store.apply_goal(0, {"text": f"due {i}", "by_turn": 3}, 1, i)
    # predictions with EARLIER deadlines interleave into the slots
    store.apply_prediction(0, {"text": "earliest", "review_turn": 2}, 1, 20)
    store.apply_prediction(0, {"text": "also early", "review_turn": 2}, 1, 21)
    text = render_memory(store, 0, 3)
    # predictions are visible (deadline-sorted ahead of the t3 goals)
    assert "p1" in text and "p2" in text
    assert "prediction p1" in text  # full line, not just the summary ids
    # every due goal id appears somewhere: full lines, GOALS, or summary
    for i in range(1, 9):
        assert f"g{i}" in text, f"due goal g{i} vanished from the view"
    # the overflow is summarized explicitly
    assert "more due:" in text


def test_many_due_goals_do_not_starve_predictions():
    """Codex R3 #1: goals must not fill all six review slots ahead of
    every prediction — interleaving is by deadline."""
    from civ_arena.strategy.view import render_memory

    store = StrategyStore()
    store.facts.note(0, 5, {"cities": 2})
    for i in range(1, 7):  # six due goals, deadline t5
        store.apply_goal(0, {"text": f"goal {i}", "by_turn": 5}, 1, i)
    store.apply_prediction(0, {"text": "pred due earlier", "review_turn": 4},
                           1, 20)
    text = render_memory(store, 0, 5)
    assert "prediction p1" in text  # deadline t4 beats the t5 goals


def test_from_log_rejects_bool_phase_identity_only():
    """Codex R3 #2: phase_player_id=true with player_id=1 passes plain
    equality (True == 1) — the typed check must reject it."""
    records = _log_records()
    doctored = []
    for rec in records:
        if rec.get("kind") in ("TOOL_CALL", "TOOL_RESULT") \
                and rec.get("tool") == "set_goal":
            rec = {**rec, "player_id": 1, "phase_player_id": True}
        doctored.append(rec)
    assert StrategyStore.from_log(doctored).goals == {}


async def test_hook_collision_and_readonly_property_survive(tmp_path):
    """Codex R3 #3/#4: a runtime with an UNRELATED bind_services signature
    is skipped (not called with alien kwargs), and a runtime whose diary is
    a read-only property resumes cleanly — no legacy attribute assignment
    after the log is truncated."""
    import json

    from civ_arena.arena.checkpoints import CheckpointState

    class AlienHookBot:
        def __init__(self) -> None:
            self.rng = random.Random(0)

        def bind_services(self, services: list) -> None:  # unrelated signature
            raise AssertionError("must never be called")

        async def take_turn(self, facade: Any) -> None:
            await facade.end_turn()

    class ReadonlyDiaryBot:
        """bind_services opts in for strategy only; diary is read-only."""

        def __init__(self) -> None:
            self.rng = random.Random(0)
            self._diary = "frozen"
            self.strategy = None

        @property
        def diary(self) -> str:
            return self._diary

        def bind_services(self, *, diary=None, strategy=None) -> None:
            if strategy is not None:
                self.strategy = strategy

        async def take_turn(self, facade: Any) -> None:
            await facade.end_turn()

    spec = _strategy_spec("hook-collision", 2)
    arena = Arena(tmp_path / "run", spec,
                  runtimes={0: AlienHookBot(), 1: ReadonlyDiaryBot()})
    await arena.run()  # construction + run: no hook collision, no raise

    ckpt = CheckpointState.from_doc(json.loads(
        (tmp_path / "run" / "checkpoints" / "ckpt-turn-0001.json")
        .read_text()))
    resumed = Arena(tmp_path / "run", spec,
                    runtimes={0: AlienHookBot(), 1: ReadonlyDiaryBot()})
    await resumed.run(resume_state=ckpt)  # resume: still no raise
    readonly = resumed.runtimes[1]
    assert readonly.diary == "frozen"  # never assigned behind its back
    assert readonly.strategy is resumed.strategy  # hooked, though


def test_displayed_value_is_bound_to_the_deadline():
    """Codex R2 #5: the value shown next to a sticky verdict is fetched at
    the same deadline the verdict was — 'MISSED (gold=150)' next to a goal
    that had 50 at its deadline would contradict itself."""
    from civ_arena.strategy.view import render_memory

    store = StrategyStore()
    store.apply_goal(0, {"text": "save gold", "by_turn": 5,
                         "metric": "gold", "target": 100}, 1, 1)
    store.facts.note(0, 5, {"gold": 50})
    store.facts.note(0, 6, {"gold": 150})
    text = render_memory(store, 0, 6)
    assert "MISSED (gold=50)" in text
    assert "gold=150" not in text


def test_no_deadline_metric_goal_is_self_assess():
    """Codex R2 #6: the arena scores when due — a metric goal with no
    deadline is never due, so it is self-assessed regardless of metric."""
    from civ_arena.strategy import scoring

    store = StrategyStore()
    store.apply_goal(0, {"text": "someday rich", "metric": "gold",
                         "target": 500}, 1, 1)
    store.facts.note(0, 9, {"gold": 900})
    assert scoring.verdict(store.current_goals(0)[0], store.facts, 0, 9) \
        == "self_assess"
    assert scoring.deadline_turn(store.current_goals(0)[0], 9) is None


# ------------------------------------------- Codex round 4 pins


def test_review_lines_are_clipped_and_render_terminates():
    """Codex R4 #1: the R3 rework dropped per-row clipping — 280-char claim
    texts made the budget arithmetic lie and the last-resort loop spin.
    Every rendered line is at most LINE_CLIP, and the pathological store
    (max-length texts everywhere) renders to a bounded view."""
    from civ_arena.strategy.view import LINE_CLIP, MEMORY_BUDGET, render_memory

    store = StrategyStore()
    store.facts.note(0, 3, {"cities": 2})
    for i in range(7):  # 7 due, max-length texts
        store.apply_prediction(0, {"text": "p" * 280, "review_turn": 3},
                               1, i + 1)
    for i in range(6):
        store.apply_goal(0, {"text": "g" * 280, "by_turn": 4}, 1, 20 + i)
    for i in range(6):
        store.beliefs.see(0, 2, 30 + i, foreign_units=[
            {**_fu(f"u{i}"), "type": "T" * 60}])
    text = render_memory(store, 0, 3)
    assert len(text) <= MEMORY_BUDGET
    assert all(len(line) <= LINE_CLIP for line in text.splitlines())


def test_all_64_due_prediction_ids_stay_visible():
    """Codex R4 #2: a single clipped summary line could not carry 64
    overflow ids — the tail ids appeared nowhere. Summaries are chunked;
    every due id is visible somewhere."""
    from civ_arena.strategy.view import render_memory

    store = StrategyStore()
    store.facts.note(0, 5, {"cities": 2})
    for i in range(64):  # the prediction cap, all due at once
        store.apply_prediction(0, {"text": f"claim {i}", "review_turn": 3},
                               1, i + 1)
    text = render_memory(store, 0, 3)
    for i in range(1, 65):
        assert f"p{i}" in text, f"due prediction p{i} vanished"
    assert "more due:" in text


def test_worst_legal_assembly_fits_budget_after_stage_drops():
    """Codex R4 #1 (the structural half): with the section caps as shipped,
    the worst LEGAL assembly fits the budget once RESOLVED/LESSONS drop —
    the last-resort loop is unreachable, as the in-code budget proof
    claims. If a cap changes without re-doing the arithmetic, this fails."""
    from civ_arena.strategy import view as V

    store = StrategyStore()
    store.facts.note(0, 3, {"cities": 2})
    for i in range(7):
        store.apply_prediction(0, {"text": "d" * 280, "review_turn": 3},
                               1, i + 1)
    for i in range(6):
        store.apply_goal(0, {"text": "g" * 280, "by_turn": 3}, 1, 10 + i)
    review_lines, _goal_ids, _n_full = V._review_due(store, 0, 3)
    goals = ["x" * V.LINE_CLIP] * V.GOAL_CAP
    seen = ["x" * V.LINE_CLIP] * V.SEEN_CAP
    lessons = ["x" * V.LINE_CLIP] * V.LESSON_TAIL
    resolved = ["x" * V.LINE_CLIP] * V.RESOLVED_TAIL
    worst = V._assemble([["REVIEW DUE THIS TURN", review_lines],
                         ["GOALS (active)", goals],
                         ["LAST SEEN (may be stale)", seen]])
    assert len(worst) <= V.MEMORY_BUDGET, (
        f"cap arithmetic broken: worst post-drop assembly {len(worst)} > "
        f"{V.MEMORY_BUDGET} — see the budget proof in view.py")
    assert lessons and resolved  # both exist only to be dropped first


def test_service_binder_validates_the_exact_call():
    """Codex R4 #3: parameter-name sniffing is not opt-in proof. An extra
    required kwarg or positional-only params are skipped (they would raise
    at the call); a **services wrapper IS a valid opt-in (skipping it
    would leave stale stores across resume)."""
    from civ_arena.arena.coordinator import _service_binder

    class ExtraRequired:
        def bind_services(self, *, tenant, diary=None,
                          strategy=None) -> None:  # extra required kwarg
            raise AssertionError("must never be called")

    class PositionalOnly:
        def bind_services(self, diary, strategy, /) -> None:
            raise AssertionError("must never be called")

    class KwargsWrapper:
        def __init__(self) -> None:
            self.got: dict = {}

        def bind_services(self, **services) -> None:
            self.got = services

    class Proper:
        def __init__(self) -> None:
            self.got: dict = {}

        def bind_services(self, *, diary=None, strategy=None) -> None:
            self.got = {"diary": diary, "strategy": strategy}

    assert _service_binder(ExtraRequired()) is None
    assert _service_binder(PositionalOnly()) is None
    wrapper = KwargsWrapper()
    binder = _service_binder(wrapper)
    assert binder is not None
    binder(diary="d", strategy="s")
    assert wrapper.got == {"diary": "d", "strategy": "s"}
    proper = Proper()
    binder = _service_binder(proper)
    assert binder is not None
    binder(diary="d", strategy="s")
    assert proper.got == {"diary": "d", "strategy": "s"}
    assert _service_binder(object()) is None


def test_render_memory_budget_drops_stages_in_order(monkeypatch):
    """Codex R4 (superseding the R1 #11 stage fixtures): with the caps as
    shipped, no LEGAL store can exceed the budget (that invariant is pinned
    by test_worst_legal_assembly_fits_budget_after_stage_drops). The drop
    machinery is therefore exercised under a SHRUNK budget — the stages and
    their order are pinned without inventing cap-violating stores."""
    from civ_arena.strategy import view as V

    store = StrategyStore()
    store.facts.note(0, 3, {"own_cities": 2, "gold": 5})
    # exactly 32 living goals (the cap): 25 due + 3 done + 4 active
    for i in range(25):
        store.apply_goal(
            0, {"text": f"due {i} " + "d" * 200, "by_turn": 3,
                "metric": "cities", "target": 1}, 1, i)
    for i in range(3):
        store.apply_goal(0, {"text": f"done {i} " + "r" * 100,
                             "status": "done"}, 1, 40 + i)
    for i in range(3):
        store.apply_lesson(0, {"text": f"lesson {i} " + "l" * 100},
                           2, 50 + i)
    for i in range(6):
        store.beliefs.see(0, 2, 50 + i, foreign_units=[
            {**_fu(f"u{i}"), "type": "S" * 90}])
    for i in range(4):
        store.apply_goal(0, {"text": f"active {i} " + "a" * 100},
                         1, 60 + i)

    def sections_at(budget: int) -> set[str]:
        monkeypatch.setattr(V, "MEMORY_BUDGET", budget)
        text = V.render_memory(store, 0, 3)
        assert len(text) <= budget
        return {s for s in ("REVIEW DUE THIS TURN", "GOALS (active)",
                            "LAST SEEN", "LESSONS", "RESOLVED") if s in text}

    # the fixed drop order, at empirically verified transition budgets:
    # RESOLVED first, then LESSONS, then LAST SEEN — REVIEW and GOALS last
    assert sections_at(2400) == {"REVIEW DUE THIS TURN", "GOALS (active)",
                                 "LAST SEEN", "LESSONS"}
    assert sections_at(2100) == {"REVIEW DUE THIS TURN", "GOALS (active)",
                                 "LAST SEEN"}
    assert sections_at(1550) == {"REVIEW DUE THIS TURN", "GOALS (active)"}
    # REVIEW DUE survives every stage with all six full items intact
    # (their verdict suffix clips at LINE_CLIP with 200-char texts — the
    # verdict rendering itself is pinned in the sections test)
    monkeypatch.setattr(V, "MEMORY_BUDGET", 1550)
    text = V.render_memory(store, 0, 3)
    assert text.count("\n- goal g") == 6
