"""Offline behavioral contract; no provider, engine, desktop, or HTTP fixtures."""
from __future__ import annotations

import copy
import json

import pytest

from civ_arena.canonical import args_digest
from civ_arena.diplomacy import DiplomacyStore, ProtocolError, ServerContext
from civ_arena.diplomacy.protocol import parse_command


def ctx(player=0, turn=1, match="fixture"):
    return ServerContext(match, player, turn, f"lease-{turn}-{player}")


def store(players=(0, 1)):
    result = DiplomacyStore("fixture", players)
    result.begin_turn(ctx())
    return result


def proposal(key="o1", recipient=1):
    return {
        "op": "offer", "command_key": key,
        "audience": {"kind": "private", "recipients": [recipient]},
        "participants": [0, recipient], "expires_after_round": 3,
        "terms": {"version": 1, "enforcement": "voluntary",
                  "activation": "next_complete_round_boundary", "duration_rounds": 2,
                  "obligations": [{"id": "peace", "actor": 0,
                                   "kind": "refrain_from_attack", "beneficiary": recipient,
                                   "condition": {"kind": "always"},
                                   "evidence_policy": "recipient_visible_only"}]},
    }


def reference(offer, op="accept", key="a1"):
    return {"op": op, "command_key": key,
            **{k: offer[k] for k in ("offer_id", "revision", "terms_hash")}}


def message(key="m1", to=1, text="A claim, not an engine observation."):
    return {"op": "message", "command_key": key,
            "audience": {"kind": "private", "recipients": [to]}, "text": text}


def next_round(s, turn):
    for player in s.players:
        s.begin_turn(ctx(player, turn))
    return s.advance_round([[turn, p] for p in s.players])


def rejects_unchanged(s, fn, reason):
    before, records = s.state_digest(), s.records()
    with pytest.raises(ProtocolError, match=reason):
        fn()
    assert s.state_digest() == before
    assert s.records() == records


def test_exact_consent_delivery_activation_and_elapsed_not_compliance():
    s = store()
    offer = s.apply(ctx(), json.dumps(proposal()))["offer"]
    assert offer["terms_hash"] == args_digest(offer["consent"])
    assert offer["accepted_by"] == [0]
    assert not s.project(1)["offers"]
    s.begin_turn(ctx(1))
    accepted = s.apply(ctx(1), reference(offer))["offer"]
    assert accepted["status"] == "agreed"
    assert accepted["accepted_by"] == [0, 1]
    assert s.project(0)["offers"][0]["status"] == "open"
    s.advance_round([[1, 0], [1, 1]])
    s.begin_turn(ctx(0, 2))
    active = s.project(0)["offers"][0]
    assert (active["status"], active["first_round"], active["last_round"]) == ("active", 2, 3)
    next_round(s, 2)
    next_round(s, 3)
    s.begin_turn(ctx(0, 4))
    assert s.project(0)["offers"][0]["status"] == "elapsed"
    assert s.project(0)["offers"][0]["compliance"] == "unassessed"


def test_counteroffer_replaces_consent_and_stale_acceptance_rejects():
    s = store()
    first = s.apply(ctx(), proposal())["offer"]
    s.begin_turn(ctx(1))
    counter = proposal("counter")
    counter.update(reference(first, "counteroffer", "counter"))
    counter["audience"]["recipients"] = [0]
    counter["terms"]["duration_rounds"] = 3
    second = s.apply(ctx(1), counter)["offer"]
    assert second["revision"] == 2 and second["accepted_by"] == [1]
    assert second["terms_hash"] != first["terms_hash"]
    rejects_unchanged(s, lambda: s.apply(ctx(), reference(first)), "offer_unavailable")
    s.advance_round([[1, 0], [1, 1]])
    s.begin_turn(ctx(0, 2))
    agreed = s.apply(ctx(0, 2), reference(second))["offer"]
    assert agreed["status"] == "agreed" and agreed["accepted_by"] == [0, 1]
    assert s.records()[1]["result"]["offer"]["revision"] == 1


@pytest.mark.parametrize("alter", [
    lambda x: x.update(author=1),
    lambda x: x.update(player_id=1),
    lambda x: x["participants"].__setitem__(0, False),
    lambda x: x["audience"]["recipients"].__setitem__(0, True),
    lambda x: x["terms"].update(enforcement="hard"),
    lambda x: x["terms"].update(version=True),
    lambda x: x["terms"].update(duration_rounds=2.0),
    lambda x: x["terms"]["obligations"][0].update(actor=True),
    lambda x: x["terms"]["obligations"][0].update(actor=99),
    lambda x: x["terms"]["obligations"][0].update(evidence_policy="omniscient"),
    lambda x: x["terms"]["obligations"][0].update(kind="arbitrary_lua"),
    lambda x: x["terms"]["obligations"][0].update(condition={"kind": "secret_fact"}),
    lambda x: x.update(expires_after_round=1),
    lambda x: x.update(expires_after_round=True),
])
def test_untrusted_identity_schema_and_unsupported_terms_are_atomic(alter):
    s, command = store(), proposal()
    alter(command)
    rejects_unchanged(s, lambda: s.apply(ctx(), command), ".+")


@pytest.mark.parametrize("raw", [
    '{"op":"message","op":"offer"}', '{"x":NaN}', '{"x":Infinity}',
    '{"x":1.2}', '[]', '{', '{"x":"\\ud800"}', '[' * 1001 + '0' + ']' * 1001,
    '{"text":"' + 'x' * 5000 + '"}',
])
def test_strict_raw_json_rejects_ambiguous_or_unbounded_inputs(raw):
    with pytest.raises(ProtocolError):
        parse_command(raw)


def test_dict_cycles_and_deep_trees_reject_without_mutating():
    command = {"op": "message"}
    command["cycle"] = command
    with pytest.raises(ProtocolError, match="payload_complexity"):
        parse_command(command)


def test_server_context_is_not_model_json_and_cross_match_rejects():
    s = store()
    for invalid in (ctx(match="other"), ctx(True), ctx(99), ctx(turn=True), ctx(turn=2)):
        rejects_unchanged(s, lambda c=invalid: s.apply(c, proposal()), "invalid_server_context")
    rejects_unchanged(s, lambda: s.apply(ctx(1), message(to=0)), "turn_not_begun")
    rejects_unchanged(s, lambda: s.begin_turn(ServerContext("fixture", 0, 1, "changed")),
                      "lease_changed")


def test_private_membership_absent_content_counts_hashes_and_nonexistent_errors():
    s = store((0, 1, 2))
    s.begin_turn(ctx(2))
    before = s.project(2)
    s.apply(ctx(), message(text="SECRET-CLAIM"))
    offer = s.apply(ctx(), proposal())["offer"]
    assert s.project(2) == before
    assert "SECRET" not in json.dumps(s.project(2))
    assert not s.project(1)["messages"]
    s.begin_turn(ctx(1))
    assert s.project(1)["messages"][0]["text"] == "SECRET-CLAIM"
    for oid in (offer["offer_id"], "offer:missing:1"):
        bad = reference(offer)
        bad["offer_id"] = oid
        rejects_unchanged(s, lambda c=bad: s.apply(ctx(2), c), "resource_unavailable")
    view = s.project(1)
    assert "seq" not in view and "state_digest" not in view
    view["messages"][0]["text"] = "CHANGED"
    assert s.project(1)["messages"][0]["text"] == "SECRET-CLAIM"


def test_public_identifiers_do_not_disclose_private_counter_gaps():
    s = store((0, 1, 2))
    s.apply(ctx(), message())
    public = message("public")
    public["audience"] = {"kind": "public", "recipients": [1, 2]}
    result = s.apply(ctx(), public)
    assert result["message_id"] == "message:public:1"
    s.begin_turn(ctx(2))
    assert len(s.project(2)["messages"]) == 1
    assert s.project(2)["cursor"] == 1


def test_message_after_own_snapshot_waits_until_next_own_lease():
    s = store()
    s.begin_turn(ctx(1))
    s.apply(ctx(), message())
    assert s.begin_turn(ctx(1))["delivered"] == 0
    assert s.project(1)["messages"] == []
    s.advance_round([[1, 0], [1, 1]])
    assert s.begin_turn(ctx(1, 2))["delivered"] == 1
    assert s.project(1)["messages"][0]["truth"] == "unverified_statement"


def test_idempotency_across_turns_has_no_new_cost_or_records_and_conflict_rejects():
    s = store()
    command = proposal()
    first = s.apply(ctx(), command)
    digest, records = s.state_digest(), s.records()
    assert s.apply(ctx(), command) == first
    assert (s.state_digest(), s.records()) == (digest, records)
    command["terms"]["duration_rounds"] = 3
    rejects_unchanged(s, lambda: s.apply(ctx(), command), "command_key_conflict")
    next_round(s, 1)
    s.begin_turn(ctx(0, 2))
    records = s.records()
    assert s.apply(ctx(0, 2), proposal()) == first
    assert s.records() == records


@pytest.mark.parametrize("completed", [
    [[1, 0]], [[1, 0], [1, 0]], [[1, 1], [1, 0]], [[1, 0], [2, 1]],
    [[True, 0], [1, 1]], [[1, False], [1, 1]], [[2, 0], [2, 1]],
])
def test_partial_duplicate_out_of_order_rounds_do_not_advance(completed):
    s = store()
    s.begin_turn(ctx(1))
    rejects_unchanged(s, lambda: s.advance_round(completed), "incomplete_round")


def test_expired_offer_does_not_gain_consent_or_auto_renew():
    s = store()
    offer = s.apply(ctx(), proposal())["offer"]
    for turn in (1, 2, 3):
        next_round(s, turn)
    s.begin_turn(ctx(1, 4))
    assert s.project(1)["offers"][0]["status"] == "expired"
    rejects_unchanged(s, lambda: s.apply(ctx(1, 4), reference(offer)), "offer_unavailable")


@pytest.mark.parametrize("op,actor,expected", [("withdraw", 0, "withdrawn"),
                                             ("reject", 1, "rejected")])
def test_offer_close_states_and_stale_actions(op, actor, expected):
    s = store()
    offer = s.apply(ctx(), proposal())["offer"]
    s.begin_turn(ctx(1))
    result = s.apply(ctx(actor), reference(offer, op, "close"))
    assert result["offer"]["status"] == expected
    rejects_unchanged(s, lambda: s.apply(ctx(1), reference(offer)), "offer_unavailable")


def test_limits_apply_to_new_operations_not_retries_or_invalid_attempts():
    s = store()
    first = s.apply(ctx(), proposal())
    s.apply(ctx(), proposal("o2"))
    rejects_unchanged(s, lambda: s.apply(ctx(), proposal("o3")), "offer_limit")
    s.apply(ctx(), message("m1"))
    s.apply(ctx(), message("m2"))
    rejects_unchanged(s, lambda: s.apply(ctx(), message("m3")), "operation_limit")
    assert s.apply(ctx(), proposal()) == first


def test_message_history_is_bounded_and_omission_count_is_recipient_local():
    s = store()
    for turn in range(1, 6):
        s.begin_turn(ctx(0, turn))
        for n in range(4):
            s.apply(ctx(0, turn), message(f"m-{turn}-{n}"))
        s.begin_turn(ctx(1, turn))
        s.advance_round([[turn, 0], [turn, 1]])
    view = s.project(1)
    assert len(view["messages"]) == 16 and view["omitted_messages"] == 4
    assert view["messages"][0]["message_id"] == "message:0-1:5"


def test_replay_binds_every_context_command_result_and_projection():
    s = store()
    offer = s.apply(ctx(), proposal())["offer"]
    s.apply(ctx(), message())
    s.begin_turn(ctx(1))
    s.apply(ctx(1), reference(offer))
    next_round(s, 1)
    s.begin_turn(ctx(0, 2))
    rebuilt = DiplomacyStore.from_records("fixture", (0, 1), s.records())
    assert rebuilt.state_digest() == s.state_digest()
    assert rebuilt.records() == s.records()
    assert all(rebuilt.project(p) == s.project(p) for p in (0, 1))
    prefix = DiplomacyStore.from_records("fixture", (0, 1), s.records()[:1])
    assert prefix.project(0)["offers"] == []
    assert prefix.project(1)["messages"] == []


@pytest.mark.parametrize("tamper", [
    lambda r: r[1]["inputs"]["context"].update(author=1),
    lambda r: r[1]["inputs"]["context"].update(match_id="foreign"),
    lambda r: r[1]["result"]["offer"].update(terms_hash="forged"),
    lambda r: r[1]["result"]["offer"].update(revision=True),
    lambda r: r[1].update(seq=0),
    lambda r: r[1].update(seq=True),
    lambda r: r.append(copy.deepcopy(r[1])),
    lambda r: r[1]["inputs"]["command"].update(author=0),
])
def test_replay_rejects_tampered_results_namespaces_types_and_duplicates(tamper):
    s = store()
    s.apply(ctx(), proposal())
    records = s.records()
    tamper(records)
    with pytest.raises(ProtocolError):
        DiplomacyStore.from_records("fixture", (0, 1), records)


def test_returned_data_cannot_mutate_store_or_consent():
    s = store()
    command = proposal()
    result = s.apply(ctx(), command)
    before = s.state_digest()
    command["terms"]["duration_rounds"] = 10
    result["offer"]["consent"]["terms"]["duration_rounds"] = 11
    records = s.records()
    records[1]["result"]["offer"]["consent"]["terms"]["duration_rounds"] = 12
    assert s.state_digest() == before


def test_consent_hash_binds_match_expiry_parties_and_every_term():
    baseline = store().apply(ctx(), proposal())["offer"]["terms_hash"]
    variants = []
    for field, value in (("expires_after_round", 4),):
        altered = proposal()
        altered[field] = value
        variants.append(store().apply(ctx(), altered)["offer"]["terms_hash"])
    altered = proposal()
    altered["terms"]["duration_rounds"] = 3
    variants.append(store().apply(ctx(), altered)["offer"]["terms_hash"])
    other_match = DiplomacyStore("other")
    other_match.begin_turn(ctx(match="other"))
    variants.append(other_match.apply(ctx(match="other"), proposal())["offer"]["terms_hash"])
    other_party = store((0, 1, 2))
    variants.append(other_party.apply(ctx(), proposal(recipient=2))["offer"]["terms_hash"])
    assert baseline not in variants and len(set(variants)) == len(variants)


def test_report_and_statement_are_precise_terms_but_no_compliance_is_invented():
    s, command = store(), proposal()
    command["terms"]["obligations"] = [
        {"id": "report", "actor": 0, "kind": "send_report", "topic": "east",
         "to": [1], "due_after_activation_rounds": 2, "condition": {"kind": "always"},
         "evidence_policy": "protocol_delivery"},
        {"id": "intent", "actor": 1, "kind": "statement", "text": "I plan to scout east.",
         "condition": {"kind": "always"}, "evidence_policy": "unadjudicated"},
    ]
    offer = s.apply(ctx(), command)["offer"]
    s.begin_turn(ctx(1))
    s.apply(ctx(1), reference(offer))
    report = message("report", text="There is a dragon to the east.")
    report["topic"] = "east"
    s.apply(ctx(), report)
    next_round(s, 1)
    s.begin_turn(ctx(1, 2))
    view = s.project(1)
    assert view["messages"][0]["truth"] == "unverified_statement"
    assert view["offers"][0]["compliance"] == "unassessed"


def test_open_offer_limit_is_channel_local_and_bound_is_atomic():
    s = store((0, 1, 2))
    for turn in range(1, 5):
        s.begin_turn(ctx(0, turn))
        for n in range(2):
            command = proposal(f"o-{turn}-{n}")
            command["expires_after_round"] = 10
            s.apply(ctx(0, turn), command)
        next_round(s, turn)
    s.begin_turn(ctx(0, 5))
    command = proposal("ninth")
    command["expires_after_round"] = 10
    rejects_unchanged(s, lambda: s.apply(ctx(0, 5), command), "open_offer_limit")
    command = proposal("other-channel", recipient=2)
    command["expires_after_round"] = 10
    assert s.apply(ctx(0, 5), command)["status"] == "accepted"


def test_active_treaty_limit_is_atomic(monkeypatch):
    import civ_arena.diplomacy.protocol as protocol

    monkeypatch.setattr(protocol, "MAX_ACTIVE_TREATIES", 1)
    s = store()
    first = s.apply(ctx(), proposal())["offer"]
    second = s.apply(ctx(), proposal("second"))["offer"]
    s.begin_turn(ctx(1))
    s.apply(ctx(1), reference(first))
    rejects_unchanged(s, lambda: s.apply(ctx(1), reference(second, key="second-accept")),
                      "active_treaty_limit")


def test_forged_early_accept_and_withdraw_of_active_treaty_reject():
    s = store()
    offer = s.apply(ctx(), proposal())["offer"]
    rejects_unchanged(s, lambda: s.apply(ctx(), reference(offer)), "already_consented")
    s.begin_turn(ctx(1))
    s.apply(ctx(1), reference(offer))
    rejects_unchanged(s, lambda: s.apply(ctx(), reference(offer, "withdraw", "withdraw")),
                      "offer_unavailable")


def test_oversize_consented_terms_and_duplicate_obligations_reject_atomically():
    s, command = store(), proposal()
    command["terms"]["obligations"] *= 2
    rejects_unchanged(s, lambda: s.apply(ctx(), command), "invalid_obligation_id")
    command = proposal()
    command["terms"]["obligations"] = [
        {"id": f"text-{n}", "actor": 0, "kind": "statement", "text": "x" * 600,
         "condition": {"kind": "always"}, "evidence_policy": "unadjudicated"}
        for n in range(2)
    ]
    rejects_unchanged(s, lambda: s.apply(ctx(), command), "consent_size")


@pytest.mark.parametrize("probe_channel_full", [False, True])
def test_hidden_channel_saturation_cannot_change_identical_recipient_admission(
        monkeypatch, probe_channel_full):
    import civ_arena.diplomacy.protocol as protocol

    # Three private channels, one public channel, three own-begin partitions,
    # and one round partition: 16 slots reserve exactly two in each partition.
    monkeypatch.setattr(protocol, "MAX_RECORDS", 16)
    results, views = [], []
    for hidden_channel_full in (False, True):
        s = store((0, 1, 2))
        s.begin_turn(ctx(1))
        s.begin_turn(ctx(2))
        if hidden_channel_full:
            s.apply(ctx(), message("hidden-1"))
            s.apply(ctx(), message("hidden-2"))
            rejects_unchanged(s, lambda s=s: s.apply(ctx(), message("hidden-3")), "record_limit")
        if probe_channel_full:
            s.apply(ctx(2), message("visible-1", to=0))
            s.apply(ctx(2), message("visible-2", to=0))
        views.append(s.project(2))
        before, records = s.state_digest(), s.records()
        try:
            result = s.apply(ctx(2), message("probe", to=0))
            results.append(result["status"])
        except ProtocolError as exc:
            results.append(str(exc))
            assert s.state_digest() == before and s.records() == records
        assert DiplomacyStore.from_records("fixture", s.players, s.records()).state_digest() == (
            s.state_digest())
    assert views[0] == views[1]
    assert results == (["record_limit"] * 2 if probe_channel_full else ["accepted"] * 2)


def test_hidden_channel_saturation_does_not_consume_public_or_lifecycle_capacity(monkeypatch):
    import civ_arena.diplomacy.protocol as protocol

    monkeypatch.setattr(protocol, "MAX_RECORDS", 16)
    s = store((0, 1, 2))
    s.begin_turn(ctx(1))
    s.begin_turn(ctx(2))
    s.apply(ctx(), message("hidden-1"))
    s.apply(ctx(), message("hidden-2"))
    public = message("public")
    public["audience"] = {"kind": "public", "recipients": [0, 1]}
    assert s.apply(ctx(2), public)["status"] == "accepted"
    assert s.advance_round([[1, 0], [1, 1], [1, 2]])["round"] == 1
    assert s.begin_turn(ctx(2, 2))["status"] == "accepted"
    assert s.project(2)["messages"][0]["message_id"] == "message:public:1"


def test_maximum_roster_partitions_preserve_exact_global_memory_bound(monkeypatch):
    import civ_arena.diplomacy.protocol as protocol

    # 8 own-begin + 28 private + 1 public + 1 round = 38 partitions.
    # Filling every partition must still fit the hard 38-record test bound.
    monkeypatch.setattr(protocol, "MAX_RECORDS", 38)
    s = store(tuple(range(8)))
    for player in s.players:
        s.begin_turn(ctx(player))
    outgoing = {p: 0 for p in s.players}
    for low in s.players:
        for high in s.players:
            if low >= high:
                continue
            author, recipient = (low, high) if high - low <= 4 else (high, low)
            outgoing[author] += 1
            s.apply(ctx(author), message(f"pair-{low}-{high}", to=recipient))
    author = next(p for p in s.players if outgoing[p] < 4)
    public = message("public")
    public["audience"] = {"kind": "public", "recipients": [p for p in s.players if p != author]}
    s.apply(ctx(author), public)
    s.advance_round([[1, p] for p in s.players])
    assert len(s.records()) == 38
    assert DiplomacyStore.from_records("fixture", s.players, s.records()).state_digest() == (
        s.state_digest())
    rejects_unchanged(s, lambda: s.begin_turn(ctx(0, 2)), "record_limit")


def test_capacity_configuration_is_fixed_at_construction_and_no_borrowing(monkeypatch):
    import civ_arena.diplomacy.protocol as protocol

    # Two-player configuration: five partitions, two slots each, one unallocated.
    monkeypatch.setattr(protocol, "MAX_RECORDS", 11)
    s = store()
    s.apply(ctx(), message("one"))
    s.apply(ctx(), message("two"))
    assert len(s.records()) == 3  # Other reservations and remainder cannot be borrowed.
    rejects_unchanged(s, lambda: s.apply(ctx(), message("three")), "record_limit")
    monkeypatch.setattr(protocol, "MAX_RECORDS", 10000)
    rejects_unchanged(s, lambda: s.apply(ctx(), message("three")), "record_limit")
    s.begin_turn(ctx(1))
    assert len(s.project(1)["messages"]) == 2
