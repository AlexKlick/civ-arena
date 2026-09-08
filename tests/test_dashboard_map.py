"""Dashboard map routes preserve custody, scope, and causal receipt boundaries."""

import http.client
import json
import re
import threading

import pytest

from civ_arena import dashboard as d
from civ_arena import minimap
from test_minimap import bind, packet, source


def records():
    return [
        {
            "seq": 0,
            "kind": "MATCH_START",
            "turn": 0,
            "player_id": 0,
            "match_id": "test-match",
            "game_instance_id": "test-instance",
        },
        source(packet(seq=1)),
        source(packet(1, seq=2, tile="900,900")),
        source(packet(seq=3, turn=2, tile="1,0")),
    ]


def write(root, events=None, tail=b""):
    run = root / "fixture"
    run.mkdir(exist_ok=True)
    (run / "events.jsonl").write_bytes(
        b"".join((json.dumps(e) + "\n").encode() for e in events or records()) + tail
    )
    return run


def test_materializes_only_selected_player_at_or_before_selected_turn(tmp_path):
    write(tmp_path)
    result = d.DashboardStore(tmp_path).load_map("fixture", player=0, turn=1)
    assert len(result["seats"]) == 1
    assert len(result["seats"][0]["snapshots"]) == 1
    assert "900,900" not in minimap.canonical(result) and "1,0" not in minimap.canonical(result)
    assert result["dashboard_source"]["requested_turn"] == 1
    assert result["event_binding"]["status"] == "matched_context_hash"


def test_other_private_packet_changes_do_not_change_player_export(tmp_path):
    events = records()
    run = write(tmp_path, events)
    store = d.DashboardStore(tmp_path)
    original = store.load_map("fixture", player=0, turn=2)
    other = packet(1, seq=2, tile="800,800")
    other["projected_state"]["own_units"][0]["type"] = "OTHER_PRIVATE_STATE"
    events[2] = source(bind(other))
    (run / "events.jsonl").unlink()
    write(tmp_path, events)
    assert store.load_map("fixture", player=0, turn=2) == original


def test_global_sequence_cutoff_excludes_late_rows_with_stale_turn_labels(tmp_path):
    events = records()
    late = source(packet(seq=4, turn=1, tile="700,700"))
    events.append(late)
    write(tmp_path, events)
    result = d.DashboardStore(tmp_path).load_map("fixture", player=0, turn=1)
    assert "700,700" not in minimap.canonical(result)
    assert result["seats"][0]["snapshots"][-1]["seq"] == 1


def test_spectator_combines_asynchronous_snapshots_without_inventing_intervening_state(tmp_path):
    write(tmp_path)
    result = d.DashboardStore(tmp_path).load_map("fixture", spectator=True, turn=2)
    assert [s["snapshots"][-1]["turn"] for s in result["seats"]] == [2, 1]
    assert result["scope"] == "combined_observation_preview"


def test_partial_tail_is_excluded_and_explicit(tmp_path):
    write(tmp_path, tail=b'{"seq":4,"turn":3')
    result = d.DashboardStore(tmp_path).load_map("fixture", player=0, turn=2)
    assert result["dashboard_source"]["partial_trailing_record_ignored"] is True
    assert any("incomplete trailing" in line for line in result["limits"])


@pytest.mark.parametrize("tail", [b"{bad}\n", b'{"seq":4,"x":NaN}\n', b'{"seq":4,"seq":4}\n'])
def test_malformed_complete_record_refuses_map(tmp_path, tail):
    write(tmp_path, tail=tail)
    with pytest.raises(ValueError):
        d.DashboardStore(tmp_path).load_map("fixture", player=0, turn=2)


def test_redaction_keeps_all_rows_and_separates_source_and_display_digests(tmp_path, monkeypatch):
    monkeypatch.setenv("MAP_TEST_SECRET", "fixture-private-label")
    p = packet(seq=1)
    p["projected_state"]["own_units"] = [
        {
            "unit_id": f"u0:{i}",
            "coord": "0,0",
            "type": "fixture-private-label",
            "private_key": "another-private-value",
        }
        for i in range(64)
    ]
    events = records()[:1] + [source(bind(p))]
    write(tmp_path, events)
    result = d.DashboardStore(tmp_path).load_map("fixture", player=0, turn=1)
    encoded = minimap.canonical(result)
    assert "fixture-private-label" not in encoded and "another-private-value" not in encoded
    assert sum(a["kind"] == "own_units" for a in result["seats"][0]["snapshots"][0]["actors"]) == 64
    assert result["digest"] != result["dashboard_source"]["raw_player_scoped_bundle_sha256"]
    assert (
        json.loads(
            re.search(
                r'<script id="data" type="application/json">(.*?)</script>',
                minimap.render(result),
                re.DOTALL,
            )[1]
        )
        == result
    )


def test_roster_survives_redaction_and_stays_seat_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("MAP_TEST_SECRET", "CIVILIZATION_SECRETLAND")
    p = packet(seq=1)
    p["projected_state"]["public"] = {
        "players": [
            {"player_id": 0, "civ_name": "CIVILIZATION_SECRETLAND", "alive": True},
            {"player_id": 1, "civ_name": "CIVILIZATION_SUMERIA", "alive": True},
        ],
        "turn": 1,
    }
    write(tmp_path, records()[:1] + [source(bind(p))])
    result = d.DashboardStore(tmp_path).load_map("fixture", player=0, turn=1)
    roster = result["seats"][0]["snapshots"][0]["public_players"]
    assert [row["player_id"] for row in roster] == [0, 1]
    assert roster[1]["civ_name"] == "CIVILIZATION_SUMERIA"
    assert "CIVILIZATION_SECRETLAND" not in minimap.canonical(result)
    assert '"public_players"' in minimap.render(result)


def test_research_options_survive_redaction_without_truncation(tmp_path, monkeypatch):
    monkeypatch.setenv("MAP_TEST_SECRET", "PRIVATE_TECH")
    p = packet(seq=1)
    p["projected_state"]["you"]["researching"] = "PRIVATE_TECH_ALPHA"
    p["projected_state"]["you"]["researched"] = [f"TECH_{i}" for i in range(50)]
    p["projected_state"]["research_options"] = [
        {"tech_id": f"PRIVATE_TECH_{i}", "cost": i} for i in range(50)
    ]
    p["projected_state"]["option_sources"] = {"research": "observed"}
    write(tmp_path, records()[:1] + [source(bind(p))])
    result = d.DashboardStore(tmp_path).load_map("fixture", player=0, turn=1)
    research = result["seats"][0]["snapshots"][0]["research"]
    assert len(research["options"]) == 50 and len(research["researched"]) == 50
    assert research["options"][7] == {"tech_id": "[redacted]_7", "cost": 7}
    assert research["researching"] == "[redacted]_ALPHA"
    assert research["options_source"] == "observed"
    assert "PRIVATE_TECH" not in minimap.canonical(result)
    assert '"research"' in minimap.render(result)


def test_redaction_that_corrupts_coordinates_fails_instead_of_relocating(tmp_path, monkeypatch):
    monkeypatch.setenv("MAP_TEST_SECRET", "0,0")
    write(tmp_path)
    with pytest.raises(ValueError, match="coordinate"):
        d.DashboardStore(tmp_path).load_map("fixture", player=0, turn=1)


def test_invalid_context_digest_is_never_repaired_for_display(tmp_path):
    events = records()
    payload = json.loads(events[1]["strategy_payload_json"])
    payload["context_sha256"] = "tampered"
    events[1]["strategy_payload_json"] = json.dumps(payload)
    write(tmp_path, events)
    with pytest.raises(ValueError, match="hash"):
        d.DashboardStore(tmp_path).load_map("fixture", player=0, turn=1)


def test_missing_packets_and_oversized_logs_are_unavailable(tmp_path, monkeypatch):
    write(tmp_path, records()[:1])
    store = d.DashboardStore(tmp_path)
    with pytest.raises(ValueError, match="no retained"):
        store.load_map("fixture", player=0, turn=1)
    monkeypatch.setattr(d, "MAX_LOG_BYTES", 1)
    with pytest.raises(d.InvalidRun, match="read limit"):
        store.load_map("fixture", player=0, turn=1)


def test_map_read_refuses_symlink_artifact(tmp_path):
    run = write(tmp_path)
    raw = (run / "events.jsonl").read_bytes()
    (run / "events.jsonl").unlink()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(raw)
    (run / "events.jsonl").symlink_to(elsewhere)
    with pytest.raises(OSError):
        d.DashboardStore(tmp_path).load_map("fixture", player=0, turn=1)


def test_http_map_scope_csp_readonly_and_unavailable_view(tmp_path):
    write(tmp_path)
    server = d.create_server(tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        for path, status in [
            ("/map?id=fixture&player=0&turn=1", 200),
            ("/map?id=fixture&spectator=1&turn=2", 200),
            ("/map?id=fixture&turn=1", 404),
            ("/map?id=fixture&player=0&spectator=1&turn=1", 404),
            ("/map?id=fixture&player=0&player=1&turn=1", 404),
            ("/map?id=fixture&spectator=0&turn=1", 404),
            ("/map?id=fixture&player=5&turn=1", 422),
            ("/map?id=../outside&player=0&turn=1", 422),
        ]:
            conn.request("GET", path)
            response = conn.getresponse()
            body = response.read()
            assert response.status == status, (path, response.status)
            if status == 200:
                policy = response.getheader("Content-Security-Policy")
                assert "frame-ancestors 'self'" in policy and "script-src 'sha256-" in policy
                assert "connect-src 'none'" in policy
            elif status == 422:
                assert b"Observed map unavailable" in body
        conn.request("POST", "/map?id=fixture&player=0&turn=1")
        r = conn.getresponse()
        r.read()
        assert r.status == 405
        conn.request("GET", "/map?id=fixture&player=0&turn=1", headers={"Host": "attacker.invalid"})
        r = conn.getresponse()
        r.read()
        assert r.status == 403
        conn.request("HEAD", "/map?id=fixture&player=0&turn=1")
        r = conn.getresponse()
        assert r.status == 200 and r.read() == b""
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
