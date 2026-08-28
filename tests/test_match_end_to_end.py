"""End-to-end: two scripted agents, 100 turns, full tool coverage, clean watchdog."""

from __future__ import annotations

from collections import Counter

from civ_arena.agents.runtime import AgentProfile, build_runtime
from civ_arena.arena.coordinator import Arena
from civ_arena.config import AgentSpec, ChaosSpec, MatchSpec, parse_config

ALL_TOOLS = {
    "get_overview", "get_units", "get_cities", "get_visible_map",
    "get_available_research", "get_available_production", "move_unit", "attack",
    "fortify", "found_city", "set_research", "set_city_production", "purchase",
    "end_turn",
}


def duel_spec(match_id: str = "e2e-duel", max_turns: int = 100) -> MatchSpec:
    return MatchSpec(
        match_id=match_id, seed=424242, max_turns=max_turns, adapter="simulator",
        watchdog_mode="flag_and_continue", violation_limit=5,
        checkpoint_every=5,
        agents=[
            AgentSpec(agent_id="roman", player_id=0, policy="expansionist", seed=11),
            AgentSpec(agent_id="korea", player_id=1, policy="turtler", seed=22),
        ],
    )


async def test_two_scripted_agents_complete_100_turns(tmp_path):
    arena = Arena(tmp_path / "run", duel_spec())
    summary = await arena.run()

    assert summary["final_turn"] == 100
    assert summary["aborted"] is None
    assert summary["violations_total"] == 0, "clean run must have zero violations"
    assert summary["final_state_hash"]
    assert set(summary["scores"]) == {"ROME", "KOREA"}

    # criterion 9: per-agent attributable telemetry, cross-checked vs the log
    tel = summary["telemetry"]
    assert set(tel) == {"roman", "korea"}
    recount: Counter = Counter()
    accepted: Counter = Counter()
    for rec in arena.log.records():
        if rec["kind"] == "TOOL_RESULT":
            recount[f"{rec['agent_id']}:{rec.get('tool')}"] += 1
            if rec.get("status") == "accepted":
                accepted[f"{rec['agent_id']}:{rec.get('tool')}"] += 1
    for agent, doc in tel.items():
        assert doc["total_calls"] == sum(
            n for key, n in recount.items() if key.startswith(f"{agent}:"))

    # criterion 1: every one of the 14 tools exercised by the match
    called_tools = {key.split(":", 1)[1] for key in recount}
    assert called_tools == ALL_TOOLS, (
        f"missing tools: {sorted(ALL_TOOLS - called_tools)}"
    )
    # combat actually happened (not just attempted)
    attack_keys = [k for k in accepted if k.endswith(":attack")]
    assert sum(accepted[k] for k in attack_keys) >= 1, "an attack must be accepted"

    # checkpoints were written
    ckpts = list((tmp_path / "run" / "checkpoints").glob("ckpt-turn-*.json"))
    assert len(ckpts) == 100 // 5


async def test_chaos_match_flags_but_completes(tmp_path):

    spec = MatchSpec(
        match_id="e2e-chaos", seed=424242, max_turns=30, adapter="simulator",
        watchdog_mode="flag_and_continue", violation_limit=50,
        checkpoint_every=5,
        agents=[
            AgentSpec(agent_id="roman", player_id=0, policy="expansionist", seed=11),
            AgentSpec(agent_id="korea", player_id=1, policy="turtler", seed=22),
        ],
        chaos=[
            ChaosSpec("move_uncommanded_unit", offset=3),
            ChaosSpec("flip_production", offset=6),
            ChaosSpec("steal_gold", offset=10),
        ],
    )
    arena = Arena(tmp_path / "run", spec)
    summary = await arena.run()
    assert summary["final_turn"] == 30
    assert summary["violations_total"] >= 3
    assert summary["aborted"] is None


def test_config_model_block_parsed_and_ignored():
    doc = {
        "match": {"match_id": "m", "seed": 1},
        "agents": [
            {"agent_id": "a", "player_id": 0, "policy": "turtler",
             "model": "local-qwen"},
        ],
    }
    spec = parse_config(doc)
    assert spec.agents[0].model == "local-qwen"
    runtime = build_runtime(AgentProfile(
        agent_id="a", player_id=0, policy="turtler", seed=1,
        model="local-qwen"))
    assert runtime.profile.model == "local-qwen"
