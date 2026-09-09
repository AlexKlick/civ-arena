"""NEXT-05 lane B: the sealed-recording config pair.

The LIVE config is the qualification recording (both seats MiniMax-M3,
`spectator_capture: true`); the REHEARSAL twin carries the identical
match block over the planner/turtler pair so the `--fake` dispatch can
prove the capture shape without touching a provider. These tests pin
the parse-level contract the lane ships against (config.py:302,
:420-427).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from civ_arena.config import ConfigError, parse_config

REPO = Path(__file__).resolve().parents[1]
LIVE = REPO / "configs" / "live-hotseat-spectator-003.yaml"
REHEARSAL = REPO / "configs" / "live-hotseat-spectator-003-rehearsal.yaml"


def _doc(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_live_sealed_config_parses_with_spectator_capture():
    spec = parse_config(_doc(LIVE))
    assert spec.match_id == "live-hotseat-spectator-003"
    assert spec.spectator_capture is True
    assert spec.adapter == "firetuner"
    assert spec.completeness_gate is True
    assert spec.max_turns == 30
    assert [a.policy for a in spec.agents] == ["llm", "llm"]
    assert [a.player_id for a in spec.agents] == [0, 1]
    # both seats ride the MiniMax provider path, key from the pinned env
    for agent in spec.agents:
        assert agent.llm is not None
        assert agent.llm.base_url == "https://api.minimax.io/anthropic/v1"
        assert agent.llm.api_key_env == "ANTHROPIC_AUTH_TOKEN_MINIMAX2"
        assert agent.llm.model_id == "MiniMax-M3"


def test_spectator_capture_requires_firetuner():
    doc = _doc(LIVE)
    doc["match"]["adapter"] = "simulator"
    with pytest.raises(ConfigError,
                       match="spectator_capture requires adapter firetuner"):
        parse_config(doc)


def test_spectator_capture_must_be_an_explicit_boolean():
    doc = _doc(LIVE)
    doc["match"]["spectator_capture"] = "true"
    with pytest.raises(ConfigError, match="explicit boolean"):
        parse_config(doc)


def test_rehearsal_twin_parses_with_capture_and_no_llm_blocks():
    doc = _doc(REHEARSAL)
    spec = parse_config(doc)
    assert spec.match_id == "live-hotseat-spectator-003-rehearsal"
    assert spec.spectator_capture is True
    assert spec.adapter == "firetuner"
    # the fake tuner fakes the TUNER, not the provider: planner/turtler
    # only, and never an llm: block
    assert [a.policy for a in spec.agents] == ["planner", "turtler"]
    assert all(a.llm is None for a in spec.agents)
    assert "llm:" not in REHEARSAL.read_text(encoding="utf-8")


def test_rehearsal_match_block_is_the_live_twin():
    live, rehearsal = _doc(LIVE)["match"], _doc(REHEARSAL)["match"]
    assert rehearsal["seed"] == live["seed"]
    assert rehearsal["spectator_capture"] is True
    without_id = lambda m: {k: v for k, v in m.items() if k != "match_id"}  # noqa: E731
    assert without_id(rehearsal) == without_id(live)
