"""Production receipts stay historical, scoped, and causally joined."""

import json
import re
from copy import deepcopy

import pytest

from civ_arena import dashboard_map, minimap
from civ_arena.canonical import args_digest
from civ_arena.dashboard import Redactor
from civ_arena.productive_map import project
from test_minimap import packet, source


def pair(pid=0, seq=2, turn=1, kind="district"):
    item = "DISTRICT_CAMPUS" if kind == "district" else "PROJECT_ENHANCE_DISTRICT_CAMPUS"
    args = {"city_id": f"c{pid}:1", "item_id": item}
    if kind == "district":
        args["dest"] = "1,0"
    common = dict(
        match_id="test-match",
        game_instance_id="test-instance",
        player_id=pid,
        agent_id=f"a{pid}",
        turn=turn,
        phase_player_id=pid,
        visibility_scope="private_player",
        tool="set_city_production",
        idempotency_key="request-key",
    )
    call = dict(common, seq=seq, kind="TOOL_CALL", args=args, args_digest=args_digest(args))
    receipt = {
        "kind": kind,
        "item_id": item,
        "city_id": args["city_id"],
        "dest": args.get("dest"),
        "production_hash": 209,
        "plot_index": 42 if kind == "district" else -1,
        "district_index": 7 if kind == "district" else -1,
        "verification": "subsequent_exact_queue_and_placement_read",
        "coverage": "separate_observation_not_mod_digest_or_mutation_ledger",
        "completion_proven": False,
    }
    receipt["observed"] = {
        k: receipt[k] for k in ("production_hash", "plot_index", "district_index")
    }
    receipt["observed"].update(
        owner_id=pid if kind == "district" else -1,
        belongs_to_city=kind == "district",
        consequence_free=kind == "district",
    )
    result = dict(
        common,
        seq=seq + 1,
        kind="TOOL_RESULT",
        status="accepted",
        duplicate=False,
        rejection=None,
        result_doc={"tool": "set_city_production", "production_readback": receipt},
    )
    return [call, result]


def events():
    return [
        dict(
            seq=0,
            kind="MATCH_START",
            turn=0,
            player_id=0,
            match_id="test-match",
            game_instance_id="test-instance",
        ),
        source(packet(seq=1)),
        *pair(),
    ]


def bundle(rows, player=0, spectator=False, turn=1):
    return dashboard_map.materialize(
        ("\n".join(json.dumps(e) for e in rows) + "\n").encode(),
        player=None if spectator else player,
        spectator=spectator,
        turn=turn,
        redactor=Redactor(),
    )


def test_exact_admissions_have_historical_geometry_and_custody_only():
    rows = events() + pair(seq=4, kind="project")
    seat = bundle(rows)["seats"][0]
    district, project_row = seat["productive_actions"]
    assert district["admission"]["coord"] == "1,0"
    assert district["admission"]["label"] == "placement observed"
    assert project_row["admission"]["coord"] is None
    assert project_row["admission"]["label"] == "queued"
    assert district["call"]["event_sha256"] == minimap.digest(rows[2])
    assert district["result"]["event_sha256"] == minimap.digest(rows[3])
    assert district["admission"]["completion_proven"] is False
    assert seat["snapshots"][0]["seq"] == 1
    assert len(seat["snapshots"][0]["terrain"]) == 1  # no invented terrain for destination
    assert seat["productive_cutoff"] == {"seq": 5, "turn": 1}


@pytest.mark.parametrize(
    "field",
    [
        "idempotency_key",
        "agent_id",
        "turn",
        "phase_player_id",
        "visibility_scope",
        "tool",
        "match_id",
        "game_instance_id",
    ],
)
def test_mismatched_identity_never_pairs_into_a_marker(field):
    call, result = pair()
    result[field] = "different"
    assert project([call, result], 0)[0]["admission"] is None


@pytest.mark.parametrize(
    "fault",
    [
        "args_digest",
        "dest",
        "owner",
        "hash",
        "boolean_hash",
        "plot_index",
        "city_id",
        "completion",
        "extra",
        "missing",
        "rejected",
        "duplicate",
        "wrong_kind",
        "geometry",
        "rejection",
    ],
)
def test_invalid_or_unconfirmed_receipts_are_journal_only(fault):
    call, result = pair()
    receipt = result["result_doc"]["production_readback"]
    if fault == "args_digest":
        call["args_digest"] = "wrong"
    if fault == "dest":
        receipt["dest"] = "999,999"
    if fault == "owner":
        receipt["observed"]["owner_id"] = 1
    if fault == "hash":
        receipt["observed"]["production_hash"] += 1
    if fault == "boolean_hash":
        receipt["observed"]["production_hash"] = True
    if fault == "plot_index":
        receipt["observed"]["plot_index"] += 1
    if fault == "city_id":
        receipt["city_id"] = "c1:1"
    if fault == "completion":
        receipt["completion_proven"] = True
    if fault == "extra":
        receipt["invented"] = "x"
    if fault == "missing":
        receipt.pop("verification")
    if fault == "rejected":
        result["status"] = "rejected"
    if fault == "duplicate":
        result["duplicate"] = True
    if fault == "wrong_kind":
        receipt["kind"] = "project"
    if fault == "geometry":
        call["args"]["dest"] = receipt["dest"] = "01,0"
        call["args_digest"] = args_digest(call["args"])
    if fault == "rejection":
        result["rejection"] = "not_your_city"
    rows = project([call, result], 0)
    assert len(rows) == 1 and rows[0]["admission"] is None
    assert "coord" not in rows[0]


def test_missing_nonadjacent_and_foreign_results_never_create_markers():
    call, result = pair()
    assert project([call], 0)[0]["status"] == "unconfirmed"
    result["seq"] += 1
    assert project([call, result], 0)[0]["admission"] is None
    result["seq"] -= 1
    result["player_id"] = 1
    assert project([call, result], 0)[0]["admission"] is None


def test_other_private_receipts_cannot_affect_export_or_digest():
    rows = events() + [source(packet(1, seq=4, tile="900,900"))] + pair(1, seq=5)
    original = bundle(rows)
    changed = deepcopy(rows)
    changed[-1]["result_doc"]["secret"] = "OTHER_PRIVATE_DATA"
    changed[-2]["args"]["dest"] = "888,888"
    assert bundle(changed) == original
    assert "c1:1" not in minimap.canonical(original)
    assert len(bundle(rows, spectator=True)["seats"]) == 2


def test_global_future_turn_prefix_cuts_receipts_with_stale_labels():
    rows = events() + [source(packet(seq=4, turn=2))] + pair(seq=5, turn=1)
    assert len(bundle(rows, turn=1)["seats"][0]["productive_actions"]) == 1


def test_call_without_result_at_prefix_is_not_admission():
    rows = events()[:3] + [source(packet(seq=3, turn=2))]
    entry = bundle(rows, turn=1)["seats"][0]["productive_actions"][0]
    assert entry["status"] == "unconfirmed" and entry["admission"] is None


def test_rendered_payload_preserves_scope_hashes_and_inert_labels(monkeypatch):
    rows = events()
    rows[2]["agent_id"] = rows[3]["agent_id"] = "</script><script>bad()</script>"
    raw = bundle(rows)
    html = minimap.render(raw)
    payload = json.loads(
        re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.DOTALL)[1]
    )
    assert payload == raw
    assert "</script><script>bad()" not in html
    monkeypatch.setenv("PRODUCTIVE_TEST_SECRET", "1,0")
    with pytest.raises(ValueError, match="coordinate"):
        bundle(rows)


def test_numeric_identity_coercion_cannot_join_a_result():
    call, result = pair()
    result['turn'] = 1.0
    assert project([call, result], 0)[0]['admission'] is None
