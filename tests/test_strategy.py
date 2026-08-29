"""The strategy store: typed claims, last-known beliefs, own-state facts —
all rebuildable from the event log alone (the DiaryStore trust-root pattern).
"""

from __future__ import annotations

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
    store.apply_goal(0, {"text": "vague intention", "by_turn": 5}, 1, 3)
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
    assert {g.goal_id for g in scoring.due_goals(store, 0, 5)} == {"g1", "g2", "g3"}
    # overdue stays visible: a skipped review does not silently resolve
    assert {g.goal_id for g in scoring.due_goals(store, 0, 7)} == {"g1", "g2", "g3"}
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


def test_render_memory_budget_drop_order_keeps_review():
    from civ_arena.strategy.view import MEMORY_BUDGET, render_memory

    store = StrategyStore()
    # maximal noise: resolved goals + lessons + sightings + active goals,
    # plus a due review that must survive everything
    for i in range(1, 9):
        store.apply_goal(0, {"text": f"resolved number {i} " + "x" * 90,
                             "status": "done"}, 1, i)
        store.apply_lesson(0, {"text": f"lesson number {i} " + "y" * 90},
                           2, 20 + i)
        store.beliefs.see(0, 2, 20 + i, foreign_units=[_fu(f"u{i}")])
    for i in range(10, 16):
        store.apply_goal(0, {"text": f"active goal {i} " + "z" * 80},
                         1, 30 + i)
    store.apply_goal(0, {"text": "the review that must survive",
                         "by_turn": 3, "metric": "cities", "target": 1}, 1, 40)
    store.facts.note(0, 2, {"own_cities": 2})

    text = render_memory(store, 0, 3)
    assert len(text) <= MEMORY_BUDGET
    assert "the review that must survive" in text  # REVIEW DUE never dropped
    assert "MISSED" not in text  # met: cities 2 >= 1
    assert "MET (cities=2)" in text
    # the drop order claims the noisiest sections first
    assert "RESOLVED" not in text or "LESSONS" not in text or len(text) < 2000
    every_line_clipped = all(
        len(line) <= 120 for line in text.splitlines())
    assert every_line_clipped


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
