"""Spectate-mode config surface: the `spectate:` block + empty-roster gate.

A spectate run rosters NO driven agents — the human plays seat 0 at the
keyboard against the engine's own AI and the harness only observes. The
block therefore carries the operator identity (the attribution key), the
observed players, snapshot scope, and AUDIT-ONLY pacing budgets. The
contract mirrors the policy blocks: optional at parse, fail-loud on any
conflict, and a spectate config with a non-empty `agents:` roster is a
configuration error, not a silent ignore.
"""

import dataclasses

import pytest

from civ_arena.config import ConfigError, SpectateSpec, parse_config


def _doc(spectate: dict | None, agents: list | None = None, **match) -> dict:
    doc = {
        "match": {
            "match_id": "spectate-cfg-test",
            "seed": 1,
            "adapter": "firetuner",
            **match,
        },
    }
    if spectate is not None:
        doc["spectate"] = spectate
    doc["agents"] = agents if agents is not None else []
    return doc


def test_spectate_block_parses_with_defaults() -> None:
    spec = parse_config(_doc({"operator": "alexk"}))
    assert spec.spectate == SpectateSpec(
        operator="alexk", human_seat=0, observed_players=(0, 1),
        snapshot_scope="full", turn_budget_s=3600.0, poll_s=2.0,
        heartbeat_s=60.0,
    )


def test_spectate_block_parses_full() -> None:
    spec = parse_config(_doc({
        "operator": "alexk", "human_seat": 0,
        "observed_players": [0, 1, 2, 3], "snapshot_scope": "ambient",
        "turn_budget_s": 1800.0, "poll_s": 1.0, "heartbeat_s": 30.0,
    }))
    assert spec.spectate is not None
    assert spec.spectate.observed_players == (0, 1, 2, 3)
    assert spec.spectate.snapshot_scope == "ambient"
    assert spec.spectate.turn_budget_s == 1800.0


def test_spectate_block_must_be_mapping() -> None:
    with pytest.raises(ConfigError, match="spectate block must be a mapping"):
        parse_config(_doc(["not-a-mapping"]))  # type: ignore[arg-type]


def test_spectate_requires_operator() -> None:
    with pytest.raises(ConfigError, match="operator"):
        parse_config(_doc({"human_seat": 0}))


@pytest.mark.parametrize("operator", ["", ".hidden", "a/b", "x" * 65, "no dots."])
def test_spectate_operator_charset(operator: str) -> None:
    with pytest.raises(ConfigError, match="operator"):
        parse_config(_doc({"operator": operator}))


@pytest.mark.parametrize("scope", ["everything", "FULL", "", "census-extra"])
def test_spectate_unknown_scope(scope: str) -> None:
    with pytest.raises(ConfigError, match="snapshot_scope"):
        parse_config(_doc({"operator": "alexk", "snapshot_scope": scope}))


@pytest.mark.parametrize("field", ["turn_budget_s", "poll_s", "heartbeat_s"])
@pytest.mark.parametrize("value", [0, -1, -0.5, float("inf"), float("nan")])
def test_spectate_budgets_must_be_positive_finite(field: str, value: float) -> None:
    with pytest.raises(ConfigError, match=field):
        parse_config(_doc({"operator": "alexk", field: value}))


def test_spectate_turn_budget_upper_bound() -> None:
    with pytest.raises(ConfigError, match="turn_budget_s"):
        parse_config(_doc({"operator": "alexk", "turn_budget_s": 86400.5}))


@pytest.mark.parametrize("players", [[0, 0], [1, 1, 2], [0, 1, 1]])
def test_spectate_observed_players_distinct(players: list[int]) -> None:
    with pytest.raises(ConfigError, match="observed_players"):
        parse_config(_doc({
            "operator": "alexk", "human_seat": 0,
            "observed_players": players,
        }))


@pytest.mark.parametrize("players", [[-1, 0], [0, -3]])
def test_spectate_observed_players_non_negative(players: list[int]) -> None:
    with pytest.raises(ConfigError, match="observed_players"):
        parse_config(_doc({
            "operator": "alexk", "human_seat": 0,
            "observed_players": players,
        }))


def test_spectate_human_seat_must_be_observed() -> None:
    with pytest.raises(ConfigError, match="human_seat"):
        parse_config(_doc({
            "operator": "alexk", "human_seat": 2,
            "observed_players": [0, 1],
        }))


def test_spectate_human_seat_not_bool() -> None:
    with pytest.raises(ConfigError, match="human_seat"):
        parse_config(_doc({"operator": "alexk", "human_seat": True}))


def test_spectate_refuses_driven_agents() -> None:
    with pytest.raises(ConfigError, match="rosters no driven agents"):
        parse_config(_doc(
            {"operator": "alexk"},
            agents=[{
                "agent_id": "a0", "player_id": 0, "policy": "turtler",
            }],
        ))


def test_no_spectate_still_requires_agent() -> None:
    with pytest.raises(ConfigError, match="at least one agent"):
        parse_config(_doc(None))


def test_spectate_requires_firetuner_adapter() -> None:
    doc = _doc({"operator": "alexk"})
    doc["match"]["adapter"] = "simulator"
    with pytest.raises(ConfigError, match="firetuner"):
        parse_config(doc)


def test_spectate_absent_leaves_spec_none() -> None:
    spec = parse_config(_doc(None, agents=[{
        "agent_id": "a0", "player_id": 0, "policy": "turtler",
    }]))
    assert spec.spectate is None


def test_spectate_spec_round_trips_through_asdict() -> None:
    # implementation_identity serializes the whole MatchSpec via
    # dataclasses.asdict — the nested frozen spec must survive it
    spec = parse_config(_doc({"operator": "alexk"}))
    doc = dataclasses.asdict(spec)
    assert doc["spectate"]["operator"] == "alexk"
    assert list(doc["spectate"]["observed_players"]) == [0, 1]


def test_llm_wire_log_flag() -> None:
    doc = _doc(None, agents=[{
        "agent_id": "a0", "player_id": 0, "policy": "llm",
        "llm": {
            "base_url": "https://api.example.com", "api_key_env": "KEY_ENV",
            "model_id": "m", "wire_log": True,
        },
    }])
    spec = parse_config(doc)
    assert spec.agents[0].llm is not None
    assert spec.agents[0].llm.wire_log is True


@pytest.mark.parametrize("value", ["true", 1, "yes", None])
def test_llm_wire_log_must_be_bool(value: object) -> None:
    doc = _doc(None, agents=[{
        "agent_id": "a0", "player_id": 0, "policy": "llm",
        "llm": {
            "base_url": "https://api.example.com", "api_key_env": "KEY_ENV",
            "model_id": "m", "wire_log": value,
        },
    }])
    with pytest.raises(ConfigError, match="wire_log"):
        parse_config(doc)


def test_llm_wire_log_defaults_false() -> None:
    doc = _doc(None, agents=[{
        "agent_id": "a0", "player_id": 0, "policy": "llm",
        "llm": {
            "base_url": "https://api.example.com", "api_key_env": "KEY_ENV",
            "model_id": "m",
        },
    }])
    spec = parse_config(doc)
    assert spec.agents[0].llm is not None
    assert spec.agents[0].llm.wire_log is False
