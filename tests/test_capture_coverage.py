"""NEXT-05 lane B: the field-family coverage matrix publisher.

The fixtures below are the ADMITTED minimal world shape (the closed key
set of world_capture.package / minimap.validate_world) wrapped in the
`audit="spectator_world"` HEARTBEAT envelope live_driver writes.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "capture_coverage.py"
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")

EXPECTED_CLASSES = ("major", "city_state", "barbarian", "free_city")
EXPECTED_FAMILIES = ("roster_identity", "economy_players", "cities",
                     "owned_tiles", "palette", "game_era", "grid", "fog_audit")


def _publisher():
    spec = importlib.util.spec_from_file_location("capture_coverage", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _roster_row(pid: int, kind: str, **extra) -> dict:
    row = {"player_id": pid, "civ_name": f"CIVILIZATION_{pid}",
           "leader": f"LEADER_{pid}", "kind": kind, "suzerain": -1}
    row.update(extra)
    return row


def _world(*, roster=None, players=(), cities=(), tiles=None, palette=None,
           game_era="ERA_ANCIENT", grid=(40, 40), read_ms=1.0,
           palette_confirmed=False, truncated=None) -> dict:
    if roster is None:
        # the ordinary board: one named major (pid 0)
        roster = [_roster_row(0, "major", is_major=True, is_barbarian=False,
                              alive=True)]
    world = {
        "schema": 1,
        "after_seat": 0,
        "contexts": {"roster": "gamecore", "tiles": "gamecore",
                     "palette": "ingame" if palette is not None else "absent"},
        "grid": {"w": grid[0], "h": grid[1]},
        "roster": list(roster),
        "players": list(players),
        "cities": list(cities),
        "owned_tiles_columns": tiles or {},
        "fog_audit": {"requested": 1, "engine_visible": 1,
                      "engine_not_visible": 0, "unavailable": 0,
                      "disagree_coords": []},
        "palette_confirmed": palette_confirmed,
        "truncated": truncated or {"tiles": False, "world": False},
        "read_ms": read_ms,
    }
    if palette is not None:
        world["palette"] = palette
    if game_era is not None:
        world["game_era"] = game_era
    return world


def _event(seq: int, world: dict, *, ts: str = "2026-09-09T00:00:00+00:00",
           audit: str = "spectator_world") -> dict:
    return {"seq": seq, "kind": "HEARTBEAT", "turn": 1,
            "match_id": "m", "game_instance_id": "gi",
            "phase_player_id": -1, "player_id": None, "agent_id": None,
            "visibility_scope": "spectator", "audit": audit,
            "after_seat": world.get("after_seat", 0), "world": world, "ts": ts}


TWO_MAJOR_WORLD = _world(
    roster=[_roster_row(0, "major", is_major=True, is_barbarian=False,
                        alive=True),
            _roster_row(1, "major", is_major=True, is_barbarian=False,
                        alive=True)],
    players=[{"player_id": 0, "civ_name": "CIVILIZATION_0", "gold": 100},
             {"player_id": 1, "civ_name": "CIVILIZATION_1", "gold": 100}],
    cities=[{"city_id": "c0:1", "owner": 0, "q": 0, "r": 0, "name": "ARENA",
             "population": 1, "is_capital": True, "is_major": True,
             "hp": 180, "max_hp": 200}],
    tiles={"0": [{"q": 0, "r": 0, "terrain": "TERRAIN_GRASS", "city": 1}]},
    palette={"0": {"primary": 5, "secondary": 6}})

CITY_STATE_WORLD = _world(
    roster=[_roster_row(0, "major", is_major=True, is_barbarian=False),
            _roster_row(12, "city_state", is_major=False, is_barbarian=False,
                        suzerain=0)],
    cities=[{"city_id": "c12:2", "owner": 12, "q": 3, "r": 1,
             "name": "GENOA", "is_capital": False, "is_major": False,
             "hp": 100, "max_hp": 100}],
    tiles={"12": [{"q": 3, "r": 1, "terrain": "TERRAIN_GRASS", "city": 2}]},
    palette={"12": {"primary": 7, "secondary": 8}})


def _matrix(events) -> dict:
    return _publisher().build_matrix(Path("runs/whatever"), events)


def _row(matrix, cls: str, family: str) -> dict:
    return next(r for r in matrix["rows"]
                if r["class"] == cls and r["field_family"] == family)


# -- fixtures are admitted world docs ------------------------------------------


def test_fixture_worlds_pass_the_viewer_validator():
    from civ_arena import minimap

    for world in (TWO_MAJOR_WORLD, CITY_STATE_WORLD):
        validated = minimap.validate_world(world)
        assert validated["schema"] == 1


# -- class attribution ----------------------------------------------------------


def test_city_state_roster_row_yields_city_state_rows_with_counts():
    events = [_event(0, TWO_MAJOR_WORLD),
              _event(1, CITY_STATE_WORLD, ts="2026-09-09T00:00:01+00:00")]
    matrix = _matrix(events)
    by_class = {e["class"]: e for e in matrix["entity_classes"]}
    # city_state is NAMED by the roster of the second world only
    assert by_class["city_state"]["observed_members"] == 1
    assert by_class["city_state"]["observed_in_worlds"] == 1
    assert by_class["major"]["observed_members"] == 2
    assert by_class["major"]["observed_in_worlds"] == 2
    # the city-state's city and territory ride the city_state rows
    cs_cities = _row(matrix, "city_state", "cities")
    assert cs_cities["source_cursor"]["events"] == 1
    assert cs_cities["source_cursor"]["seq_min"] == 1
    assert cs_cities["source_cursor"]["seq_max"] == 1
    assert cs_cities["sampling_time"]["first_ts"] == "2026-09-09T00:00:01+00:00"
    assert _row(matrix, "city_state", "owned_tiles")["source_cursor"]["events"] == 1
    # majors carry no city in the second world -> the cell counts only world 0
    assert _row(matrix, "major", "cities")["source_cursor"]["events"] == 1


def test_unobserved_class_is_a_zero_gap_row_never_inferred():
    matrix = _matrix([_event(0, TWO_MAJOR_WORLD)])
    free_city = next(e for e in matrix["entity_classes"]
                     if e["class"] == "free_city")
    assert free_city["observed_members"] == 0
    assert free_city["observed_in_worlds"] == 0
    assert free_city["named_from"] == "roster PLAYERROW kind"
    for family in EXPECTED_FAMILIES:
        row = _row(matrix, "free_city", family)
        assert row["source_cursor"] == {"seq_min": None, "seq_max": None,
                                        "events": 0}
        assert row["sampling_time"] == {"first_ts": None, "last_ts": None}
        assert row["read_ms"] == {"min": None, "median": None, "max": None}


def test_barbarian_named_only_from_roster_kind():
    barb = _world(roster=[_roster_row(63, "barbarian", is_barbarian=True)])
    matrix = _matrix([_event(0, barb)])
    assert next(e for e in matrix["entity_classes"]
                if e["class"] == "barbarian")["observed_members"] == 1
    # roster identity is the only family a bare barbarian populates
    assert _row(matrix, "barbarian", "roster_identity")["source_cursor"]["events"] == 1
    assert _row(matrix, "barbarian", "economy_players")["source_cursor"]["events"] == 0


def test_member_unnamed_by_the_same_events_roster_attributed_to_no_class():
    # an OVX player row whose pid the roster of THAT world never names
    orphan = _world(roster=[_roster_row(0, "major")],
                    players=[{"player_id": 9, "civ_name": "GHOST",
                              "gold": 5}])
    matrix = _matrix([_event(0, orphan)])
    major = _row(matrix, "major", "economy_players")
    assert major["source_cursor"]["events"] == 0
    assert major["unsupported_or_null"] == 0


# -- stats, truncation, unsupported ---------------------------------------------


def test_read_ms_stats_over_a_known_list():
    events = [_event(seq, _world(read_ms=value))
              for seq, value in enumerate([3.0, 1.0, 2.0])]
    stats = _row(_matrix(events), "major", "grid")["read_ms"]
    assert stats == {"min": 1.0, "median": 2.0, "max": 3.0}
    even = [_event(seq, _world(read_ms=value))
            for seq, value in enumerate([1.0, 2.0, 3.0, 4.0])]
    stats = _row(_matrix(even), "major", "grid")["read_ms"]
    assert stats == {"min": 1.0, "median": 2.5, "max": 4.0}


def test_truncated_carried_from_the_latest_contributing_event():
    tiles = {"0": [{"q": 0, "r": 0, "terrain": "TERRAIN_GRASS", "city": 1}]}
    events = [_event(0, _world(tiles=tiles,
                               truncated={"tiles": False, "world": False})),
              _event(1, _world(tiles=tiles,
                               truncated={"tiles": True, "world": True,
                                          "cities": 4}))]
    row = _row(_matrix(events), "major", "owned_tiles")
    assert row["truncated"] == {"tiles": True, "world": True, "cities": 4}
    assert row["source_cursor"]["events"] == 2
    # a family absent from the latest event keeps ITS OWN last carry
    assert _row(_matrix(events), "major", "palette")["truncated"] is None


def test_unsupported_or_null_counts_palette_confirmed_false():
    events = [_event(0, _world(palette={"0": {"primary": 5, "secondary": 6}},
                               palette_confirmed=False)),
              _event(1, _world(palette={"0": {"primary": 5, "secondary": 6}},
                               palette_confirmed=False))]
    row = _row(_matrix(events), "major", "palette")
    assert row["unsupported_or_null"] == 2
    assert row["context"] == "ingame"


def test_admitted_but_absent_keys_counted_from_the_payload():
    # roster level is admitted by the PLAYERROW wire and never emitted;
    # the OVX row below omits gold/gold_per_turn/science/... (Amendment-2)
    events = [_event(0, _world(
        roster=[_roster_row(0, "major")],
        players=[{"player_id": 0, "civ_name": "CIVILIZATION_0"}]))]
    # a bare roster row lacks is_major/is_barbarian/alive + the unread level
    assert _row(_matrix(events), "major", "roster_identity")[
        "unsupported_or_null"] == 4
    assert _row(_matrix(events), "major", "economy_players")[
        "unsupported_or_null"] == 10


def test_non_spectator_and_worldless_audits_are_ignored():
    worldless = {key: value for key, value in _event(1, _world()).items()
                 if key != "world"}
    failed = {key: value for key, value in _event(2, _world()).items()
              if key != "world"} | {"audit": "spectator_world_failed",
                                    "error": "TimeoutError: <redacted>"}
    status = {"seq": 3, "kind": "HEARTBEAT", "turn": 1,
              "audit": "engine_status", "ts": "2026-09-09T00:00:03+00:00"}
    matrix = _matrix([_event(0, _world()), worldless, failed, status])
    assert matrix["spectator_world_events"] == 2
    assert matrix["world_payload_missing"] == 1
    assert matrix["entity_classes"][0]["observed_in_worlds"] == 1


# -- shape invariants ------------------------------------------------------------


def test_all_four_classes_by_eight_families_in_stable_order():
    matrix = _matrix([_event(0, _world())])
    assert [(r["class"], r["field_family"]) for r in matrix["rows"]] == [
        (cls, family) for cls in EXPECTED_CLASSES
        for family in EXPECTED_FAMILIES]


def test_verifying_probes_point_at_files_that_exist():
    matrix = _matrix([_event(0, _world())])
    for row in matrix["rows"]:
        assert row["verifying_probe"]
        for token in row["verifying_probe"].split(";"):
            assert (REPO / token.strip()).exists(), row["verifying_probe"]


def test_families_carry_the_world_capture_accessors():
    matrix = _matrix([_event(0, _world())])
    accessors = {(r["class"], r["field_family"]): r["accessor"]
                 for r in matrix["rows"]}
    assert accessors[("major", "roster_identity")] == "SPECW|1 roster_read"
    assert accessors[("major", "economy_players")] == "OVX|2 overview_read"
    assert accessors[("major", "cities")] == "CITIES|2 cities_read"
    assert accessors[("major", "owned_tiles")] == "SPECW|1 owned_tiles_read"
    assert accessors[("major", "palette")] == "SPECW|1 palette_read"


# -- CLI ------------------------------------------------------------------------


def test_cli_writes_json_and_markdown(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    summary = {"match_id": "m", "identity": {"commit": "abc1234",
                                             "mod_sha256": "deadbeef"}}
    records = [_event(0, TWO_MAJOR_WORLD), _event(1, CITY_STATE_WORLD)]
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n")
    (run_dir / "summary.json").write_text(json.dumps(summary))
    out_json = tmp_path / "coverage_matrix.json"
    out_md = tmp_path / "coverage-matrix.md"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(run_dir),
         "-o", str(out_json), "--md", str(out_md)],
        capture_output=True, text=True, timeout=60.0)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    matrix = json.loads(out_json.read_text())
    assert matrix["schema"] == 1
    assert matrix["identity"]["commit"] == "abc1234"
    assert matrix["identity"]["mod_sha256"] == "deadbeef"
    assert "effective ruleset unverified" in matrix["identity"]["ruleset"]
    assert len(matrix["rows"]) == 32
    md = out_md.read_text()
    assert "## GAPS" in md and "palette_confirmed" in md
    assert "spectate-phase world carrier UNVERIFIED" in md
    assert "city_state" in md


# -- r1 fix lane: novel kinds, nulls, and markdown cells ------------------------


def test_novel_roster_kind_is_an_explicit_error_not_a_silent_gap():
    rogue = _world(roster=[_roster_row(0, "major", is_major=True),
                           _roster_row(7, "rebels")])
    matrix = _matrix([_event(0, rogue)])
    assert len(matrix["errors"]) == 1
    error = matrix["errors"][0]
    assert error["seq"] == 0 and error["pid"] == 7 and error["kind"] == "rebels"
    assert "'rebels'" in error["detail"] and "wire-admitted" in error["detail"]
    assert "barbarian" in error["detail"]
    # the unknown kind still names no class anywhere in the table
    assert [entry["class"] for entry in matrix["entity_classes"]] == \
        list(EXPECTED_CLASSES)
    rendered = _publisher().render_markdown(matrix)
    assert "## Errors" in rendered and "rebels" in rendered


def test_free_city_is_never_nameable_so_a_named_one_is_an_error():
    matrix = _matrix([_event(0, _world(roster=[_roster_row(3, "free_city")]))])
    assert [entry["kind"] for entry in matrix["errors"]] == ["free_city"]
    assert next(entry for entry in matrix["entity_classes"]
                if entry["class"] == "free_city")["observed_members"] == 1


def test_clean_run_reports_no_errors():
    matrix = _matrix([_event(0, TWO_MAJOR_WORLD), _event(1, CITY_STATE_WORLD)])
    assert matrix["errors"] == []


def test_null_valued_admitted_keys_count_as_unsupported():
    world = _world(
        roster=[_roster_row(0, "major", is_major=True, is_barbarian=None,
                            alive=None)],
        players=[{"player_id": 0, "gold": None, "science": 0}])
    matrix = _matrix([_event(0, world)])
    # level (never emitted) + the two nulls
    assert _row(matrix, "major", "roster_identity")["unsupported_or_null"] == 3
    # gold null + the other eight admitted-but-absent Amendment-2 keys
    assert _row(matrix, "major", "economy_players")["unsupported_or_null"] == 10


def test_real_falses_zeros_and_empty_values_stay_supported():
    world = _world(roster=[_roster_row(0, "major", is_major=False,
                                      is_barbarian=False, alive=False,
                                      suzerain=0)],
                   players=[{"player_id": 0, "gold": 0, "civics": [],
                             "researching": ""}])
    matrix = _matrix([_event(0, world)])
    assert _row(matrix, "major", "roster_identity")["unsupported_or_null"] == 1  # level
    assert _row(matrix, "major", "economy_players")["unsupported_or_null"] == 8


def test_markdown_cells_escape_pipes(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    records = [_event(0, TWO_MAJOR_WORLD)]
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n")
    out_md = tmp_path / "coverage-matrix.md"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(run_dir), "--md", str(out_md)],
        capture_output=True, text=True, timeout=60.0)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    md = out_md.read_text()
    # the accessor literal carries a pipe: escaped, never a column break
    assert "SPECW\\|1 roster_read" in md
    assert "SPECW|1 roster_read" not in md
    lines = md.splitlines()
    header = lines.index("| class | family | accessor | context | "
                         "observed/derived | events (seq) | first_ts | last_ts "
                         "| read_ms min/median/max | unsupported_or_null | "
                         "truncated |")
    separator = lines[header + 1]
    assert set(separator) <= {"|", "-"}
    width = len(_UNESCAPED_PIPE.findall(separator))
    for line in lines[header + 2:]:
        if not line.startswith("|"):
            break
        assert len(_UNESCAPED_PIPE.findall(line)) == width, line
