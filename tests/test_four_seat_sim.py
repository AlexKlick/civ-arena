"""Four-seat sim: golden 2-seat hashes, 4-seat layout, determinism, resume.

The golden test below was captured from UNMODIFIED code (commit a59cb44) BEFORE
the 4-seat refactor landed. It is the bit-identity guarantee for every existing
2-seat config: if these literals ever move, the refactor changed history.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from civ_arena.arena.coordinator import Arena
from civ_arena.canonical import log_prefix_hash, state_hash
from civ_arena.config import AgentSpec, ConfigError, MatchSpec, parse_config
from civ_arena.game.conformance import assert_adapter_conformance
from civ_arena.game.sim.layouts import (
    CIV_NAMES_4,
    STARTS_4,
    duel_start,
    hex_line_greedy,
)
from civ_arena.game.sim.simulator import SimulatorAdapter
from civ_arena.game.sim.state import TERRAIN, SimState, neighbors, tile_key
from conftest import cmd, free_neighbor, own_units

GOLDEN_2SEAT = {
    947381: "36cd5136354ef4332681b2be805d4b1882675da917bcb0705202e8a604188b03",
    21: "2d8658d6c5d4649abc66cc65028d03b26174297d7b06cdd86e2915a1d03fd8a7",
    2024: "358e6cbef747027c8394ad812a60a1bd0340628ee8524bfba61c6ff57777c8d6",
    999983: "b2fbf330160af8ed019a4edf64f03d90826d6798a68fe7b3546e4a855e2348bf",
}

FOUR_SEAT_CONFIG_TEMPLATE = """
match:
  match_id: {match_id}
  seed: 947381
  max_turns: {max_turns}
  checkpoint_every: 5
  player_count: 4
  adapter: simulator
  watchdog_mode: flag_and_continue
  violation_limit: 5

agents:
  - agent_id: rome
    player_id: 0
    policy: expansionist
    seed: 11
  - agent_id: korea
    player_id: 1
    policy: turtler
    seed: 22
  - agent_id: egypt
    player_id: 2
    policy: expansionist
    seed: 33
  - agent_id: mongol
    player_id: 3
    policy: turtler
    seed: 44

chaos: []
"""


def test_duel_start_2seat_golden() -> None:
    """2-seat start docs must hash identically to the pre-refactor capture."""
    for seed, expected in GOLDEN_2SEAT.items():
        assert state_hash(duel_start(seed)) == expected


def test_duel_start_4seat_shape() -> None:
    doc = duel_start(947381, 4)
    state = SimState.from_doc(doc)
    assert sorted(state.doc["players"]) == ["0", "1", "2", "3"]
    names = {p["civ_name"] for p in state.doc["players"].values()}
    assert names == set(CIV_NAMES_4.values())
    assert set(doc["revealed"]) == {"0", "1", "2", "3"}
    for player_id, start in STARTS_4.items():
        assert len(own_units(state, player_id, "SETTLER")) == 2, (
            f"seat {player_id} must open with 2 settlers")
        placed = {tile_key(u["q"], u["r"]) for u in own_units(state, player_id)}
        assert tile_key(*start) in placed, f"seat {player_id} start unoccupied"
    assert len(state.units) == 20
    ordered = sorted(state.units.values(), key=lambda u: int(u["unit_id"][1:]))
    assert [u["unit_id"] for u in ordered] == [f"u{i}" for i in range(1, 21)]


def test_4seat_all_starts_connected() -> None:
    """BFS over passable land from each start must reach the other three."""
    doc = duel_start(947381, 4)
    tiles = doc["tiles"]

    def reachable(start: tuple[int, int]) -> set[tuple[int, int]]:
        seen = {start}
        frontier = [start]
        while frontier:
            q, r = frontier.pop()
            for nq, nr in neighbors(q, r):
                t = tiles.get(tile_key(nq, nr))
                if t and TERRAIN[t["terrain"]]["move"] > 0 and (nq, nr) not in seen:
                    seen.add((nq, nr))
                    frontier.append((nq, nr))
        return seen

    for pid, start in STARTS_4.items():
        reach = reachable(start)
        for other_pid, other in STARTS_4.items():
            if other_pid != pid:
                assert other in reach, (
                    f"start {pid} cannot reach start {other_pid} by land")


def test_duel_start_4seat_deterministic() -> None:
    a = state_hash(duel_start(947381, 4))
    assert a == state_hash(duel_start(947381, 4))
    assert a != state_hash(duel_start(999983, 4))


def test_hex_line_greedy_adjacent_and_exact() -> None:
    for a, b in [
        ((6, 0), (0, -6)), ((0, -6), (-6, 0)), ((-6, 0), (0, 6)), ((0, 6), (6, 0)),
        ((-3, 1), (3, -1)),
    ]:
        line = hex_line_greedy(a, b)
        assert line[0] == a and line[-1] == b
        assert len(line) == sum(1 for _ in line)  # sanity: list
        for (q1, r1), (q2, r2) in zip(line, line[1:], strict=False):
            assert (q2, r2) in set(neighbors(q1, r1)), "steps must be adjacent"


def four_seat_spec(match_id: str, max_turns: int = 20,
                   seed: int = 947381) -> MatchSpec:
    return MatchSpec(
        match_id=match_id, seed=seed, max_turns=max_turns, adapter="simulator",
        watchdog_mode="flag_and_continue", violation_limit=5,
        checkpoint_every=5, player_count=4,
        agents=[
            AgentSpec(agent_id="rome", player_id=0, policy="expansionist", seed=11),
            AgentSpec(agent_id="korea", player_id=1, policy="turtler", seed=22),
            AgentSpec(agent_id="egypt", player_id=2, policy="expansionist", seed=33),
            AgentSpec(agent_id="mongol", player_id=3, policy="turtler", seed=44),
        ],
    )


async def test_4seat_full_match_deterministic(tmp_path) -> None:
    runs = []
    for i in (1, 2):
        arena = Arena(tmp_path / f"run{i}", four_seat_spec(f"four-{i}"))
        runs.append(await arena.run())
    s1, s2 = runs
    assert s1["final_state_hash"] == s2["final_state_hash"]
    assert s1["scores"] == s2["scores"]
    assert set(s1["scores"]) == {"ROME", "KOREA", "EGYPT", "MONGOL"}
    assert s1["violations_total"] == 0 and s2["violations_total"] == 0


def _run_match(config: Path, run_root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "civ_arena.match", str(config),
         "--run-dir", str(run_root), *extra],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        timeout=240.0,
    )


def test_4seat_kill_resume_identical(tmp_path) -> None:
    config = tmp_path / "four-resume.yaml"
    config.write_text(FOUR_SEAT_CONFIG_TEMPLATE.format(
        match_id="four-resume", max_turns=16))
    run_root = tmp_path / "runs"

    crashed = _run_match(config, run_root, "--crash-after-turn", "8")
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr[-500:]
    ckpt = run_root / "four-resume" / "checkpoints" / "ckpt-turn-0005.json"
    assert ckpt.exists(), "turn-5 checkpoint must exist before the crash point"

    resumed = _run_match(config, run_root, "--resume")
    assert resumed.returncode == 0, resumed.stderr[-800:]
    resumed_summary = json.loads(
        (run_root / "four-resume" / "summary.json").read_text())

    clean = _run_match(config, tmp_path / "clean")
    assert clean.returncode == 0, clean.stderr[-800:]
    clean_summary = json.loads(
        (tmp_path / "clean" / "four-resume" / "summary.json").read_text())

    assert resumed_summary["final_state_hash"] == clean_summary["final_state_hash"]
    assert resumed_summary["scores"] == clean_summary["scores"]
    assert resumed_summary["final_turn"] == clean_summary["final_turn"] == 16

    ckpt_doc = json.loads(ckpt.read_text())
    clean_records = [
        json.loads(line)
        for line in (tmp_path / "clean" / "four-resume" / "events.jsonl")
        .read_text().splitlines()
    ]
    assert log_prefix_hash(clean_records[: ckpt_doc["seq"]]) == (
        ckpt_doc["log_prefix_sha256"])


async def test_conformance_4seat() -> None:
    async def _make(seed: int = 1) -> SimulatorAdapter:
        adapter = SimulatorAdapter()
        await adapter.setup({"seed": seed, "player_count": 4})
        return adapter

    preview = await _make(1)
    warrior = own_units(preview.state, 0, "WARRIOR")[0]
    dest = free_neighbor(preview.state, warrior["q"], warrior["r"])
    unit_id = warrior["unit_id"]
    await preview.teardown()

    probes = [
        {"cmd": cmd("move_unit", {"unit_id": unit_id, "dest": tile_key(*dest)}),
         "expect": "accepted"},
        # foreign unit (u6 is player 1's first unit on any seat count)
        {"cmd": cmd("move_unit", {"unit_id": "u6", "dest": "0,0"}), "expect": "rejected"},
        {"cmd": cmd("move_unit", {"unit_id": unit_id, "dest": "99,99"}),
         "expect": "rejected"},
        {"cmd": cmd("fortify", {"unit_id": unit_id}), "expect": "accepted"},
    ]
    await assert_adapter_conformance(lambda: _make(1), probes, player_count=4)


def test_config_player_count_validation() -> None:
    base_agents = [
        {"agent_id": f"a{i}", "player_id": i, "policy": "turtler"}
        for i in range(4)
    ]

    def doc(match_extra: dict, agents: list[dict]) -> dict:
        return {"match": {"match_id": "m", "seed": 1, **match_extra},
                "agents": agents}

    # explicit 4 with contiguous roster parses
    spec = parse_config(doc({"player_count": 4}, base_agents))
    assert spec.player_count == 4

    # count/roster mismatches
    with pytest.raises(ConfigError):
        parse_config(doc({"player_count": 4}, base_agents[:2]))
    gapped = [dict(a) for a in base_agents]
    gapped[2]["player_id"] = 4
    with pytest.raises(ConfigError):
        parse_config(doc({"player_count": 4}, gapped))
    with pytest.raises(ConfigError):
        parse_config(doc({"player_count": 3}, base_agents))

    # bilateral policies refused at 4 seats
    planner_agents = [dict(a) for a in base_agents]
    planner_agents[0]["policy"] = "planner"
    with pytest.raises(ConfigError):
        parse_config(doc({"player_count": 4}, planner_agents))

    # absent key => today's behavior exactly (default 2, no cross-checks)
    legacy = parse_config(doc({}, base_agents[:2]))
    assert legacy.player_count == 2
    four_unmarked = parse_config(doc({}, base_agents))
    assert four_unmarked.player_count == 2
