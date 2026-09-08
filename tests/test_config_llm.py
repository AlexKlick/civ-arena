"""LLM config surface: the llm: block parses into LLMSpec or fails loudly."""

from __future__ import annotations

import pytest

from civ_arena.config import ConfigError, parse_config


def llm_doc(llm_block, policy="llm", match_id="m"):
    return {
        "match": {"match_id": match_id, "seed": 1},
        "agents": [
            {"agent_id": "a", "player_id": 0, "policy": policy,
             "llm": llm_block},
        ],
    }


VALID = {
    "base_url": "https://api.minimax.io/anthropic/v1",
    "api_key_env": "ANTHROPIC_AUTH_TOKEN_MINIMAX2",
    "model_id": "MiniMax-M3",
}


def test_llm_block_happy_parse_with_defaults():
    spec = parse_config(llm_doc(dict(VALID)))
    agent = spec.agents[0]
    assert agent.policy == "llm"
    assert agent.llm.base_url == VALID["base_url"]
    assert agent.llm.api_key_env == VALID["api_key_env"]
    assert agent.llm.model_id == VALID["model_id"]
    assert agent.llm.max_tokens == 4096
    assert agent.llm.max_tool_rounds == 16
    assert agent.llm.max_result_chars == 8000
    assert agent.llm.request_timeout_s == 120.0
    assert agent.llm.max_retries == 2
    assert agent.llm.max_requests_per_match == 2000


def test_llm_block_overrides():
    block = dict(VALID, max_tool_rounds=4, max_requests_per_match=10,
                 request_timeout_s=30)
    llm = parse_config(llm_doc(block)).agents[0].llm
    assert (llm.max_tool_rounds, llm.max_requests_per_match,
            llm.request_timeout_s) == (4, 10, 30.0)


def test_llm_policy_without_block_is_error():
    doc = {
        "match": {"match_id": "m", "seed": 1},
        "agents": [{"agent_id": "a", "player_id": 0, "policy": "llm"}],
    }
    with pytest.raises(ConfigError, match="requires an llm: block"):
        parse_config(doc)


def test_llm_block_on_scripted_policy_is_error():
    with pytest.raises(ConfigError, match="fail loudly"):
        parse_config(llm_doc(dict(VALID), policy="turtler"))


def test_literal_api_key_in_block_is_structurally_refused():
    block = dict(VALID, api_key="sk-cp-should-never-be-here")
    with pytest.raises(ConfigError, match="api_key_env"):
        parse_config(llm_doc(block))


def test_llm_block_missing_required_fields():
    for missing in ("base_url", "api_key_env", "model_id"):
        block = {k: v for k, v in VALID.items() if k != missing}
        with pytest.raises(ConfigError, match=missing):
            parse_config(llm_doc(block))


def test_llm_block_rejects_bad_base_url_and_numerics():
    with pytest.raises(ConfigError, match="http"):
        parse_config(llm_doc(dict(VALID, base_url="api.minimax.io")))
    for key, bad in [("max_tokens", 0), ("max_tool_rounds", -1),
                     ("max_result_chars", 0), ("max_retries", 0),
                     ("max_requests_per_match", 0),
                     ("request_timeout_s", 0), ("request_timeout_s", -5)]:
        with pytest.raises(ConfigError, match=key):
            parse_config(llm_doc(dict(VALID, **{key: bad})))


def test_model_string_still_display_only_alongside_llm():
    doc = llm_doc(dict(VALID))
    doc["agents"][0]["model"] = "display-hint"
    agent = parse_config(doc).agents[0]
    assert agent.model == "display-hint"
    assert agent.llm.model_id == "MiniMax-M3"


def test_own_economy_context_explicit_boolean_no_coupling():
    # default off
    assert parse_config(llm_doc(dict(VALID))).agents[0].llm \
        .own_economy_context is False
    # opt-in works WITHOUT adaptive_context (unlike the briefing) and on
    # ANY decision mode
    spec = parse_config(llm_doc(dict(VALID, own_economy_context=True)))
    assert spec.agents[0].llm.own_economy_context is True
    assert spec.agents[0].llm.adaptive_context is None
    for bad in ("yes", 1, None, []):
        with pytest.raises(ConfigError, match="own_economy_context"):
            parse_config(llm_doc(dict(VALID, own_economy_context=bad)))


def test_spectator_capture_requires_firetuner_and_explicit_boolean():
    def doc(**extra):
        base = llm_doc(dict(VALID))
        base["match"].update(extra)
        return base

    assert parse_config(doc()).spectator_capture is False
    spec = parse_config(doc(adapter="firetuner", spectator_capture=True))
    assert spec.spectator_capture is True
    with pytest.raises(ConfigError, match="spectator_capture"):
        parse_config(doc(spectator_capture=True))  # simulator adapter
    for bad in ("yes", 1, None):
        with pytest.raises(ConfigError, match="spectator_capture"):
            parse_config(doc(adapter="firetuner", spectator_capture=bad))


def test_live_visible_map_context_exact_values():
    def doc(live):
        base = llm_doc(dict(VALID))
        base["match"]["adapter"] = "firetuner"
        base["live"] = live
        return base

    assert parse_config(doc(None)).live is None
    assert parse_config(doc({})).live.visible_map_context == "gamecore"
    assert parse_config(doc({"visible_map_context": "ingame"})).live \
        .visible_map_context == "ingame"
    for bad in ("InGame", "lua", ""):
        with pytest.raises(ConfigError, match="visible_map_context"):
            parse_config(doc({"visible_map_context": bad}))
    with pytest.raises(ConfigError, match="unsupported keys"):
        parse_config(doc({"visible_map_context": "gamecore", "extra": 1}))
