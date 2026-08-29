"""The strategy store: typed claims, last-known beliefs, own-state facts —
all rebuildable from the event log alone (the DiaryStore trust-root pattern).
"""

from __future__ import annotations

from typing import Any

from civ_arena.canonical import canonical
from civ_arena.strategy.beliefs import BeliefStore
from civ_arena.strategy.claims import MAX_CLAIM_CHARS, MAX_GOALS_PER_PLAYER
from civ_arena.strategy.facts import Facts
from civ_arena.strategy.store import StrategyStore

# ---------------------------------------------------------------- helpers


def call_rec(
    tool: str, args: dict[str, Any], *, player_id: int = 0, turn: int = 1,
    agent_id: str = "roman", match_id: str = "m", gi: str = "g1", seq: int = 0,
) -> dict[str, Any]:
    return {
        "kind": "TOOL_CALL", "tool": tool, "args": args, "seq": seq,
        "player_id": player_id, "agent_id": agent_id, "turn": turn,
        "match_id": match_id, "game_instance_id": gi,
    }


def result_rec(
    tool: str, *, player_id: int = 0, turn: int = 1, agent_id: str = "roman",
    match_id: str = "m", gi: str = "g1", seq: int = 1, status: str = "accepted",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "kind": "TOOL_RESULT", "tool": tool, "seq": seq, "status": status,
        "player_id": player_id, "agent_id": agent_id, "turn": turn,
        "match_id": match_id, "game_instance_id": gi, **extra,
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
