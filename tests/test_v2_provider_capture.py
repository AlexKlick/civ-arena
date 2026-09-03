"""CAR-109 provider proposal capture: attribution, redaction, and accounting."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from civ_arena.agents.llm.client import ModelReply
from civ_arena.canonical import canonical, sha256_hex
from civ_arena.experiments.provider_capture import (
    MAX_TOKENS,
    POST_CEILING,
    PROVIDERS,
    SUBMIT_TOOL,
    build_global_pilot_gate_v2,
    candidate_catalog_v2,
    capture_attempt_v2,
    capture_prompt_v2,
    provider_policy_v2,
    source_identity_v2,
    write_attempt_v2,
)
from civ_arena.experiments.turn_core import (
    build_fixture_manifest_v2,
    fixture_recipes_v2,
    observed_fixture_v2,
)
from civ_arena.v2.contracts import TurnProposalV2
from civ_arena.v2.schemas import ContractError

SOURCE = {"commit": "a" * 40, "tree": "b" * 40}


class _FakeClient:
    def __init__(self, reply: ModelReply) -> None:
        self.reply = reply
        self.posts_sent = 0

    async def create(self, **_kwargs: Any) -> ModelReply:
        self.posts_sent += 1
        return self.reply

    async def aclose(self) -> None:
        return None


def _reply(model: str, choices: list[int]) -> ModelReply:
    return ModelReply(
        content=[
            {
                "type": "tool_use",
                "id": "toolu_fixture",
                "name": SUBMIT_TOOL,
                "input": {"choices": choices},
            }
        ],
        stop_reason="tool_use",
        model=model,
        input_tokens=321,
        output_tokens=17,
    )


@pytest.mark.asyncio
async def test_capture_persists_only_digests_usage_and_normalized_proposal(
    tmp_path: Path,
) -> None:
    spec = PROVIDERS[0]
    manifest = await build_fixture_manifest_v2((fixture_recipes_v2()[0],))
    fixture = manifest["body"]["fixtures"][0]
    observation, legal = await observed_fixture_v2(fixture)
    catalog = candidate_catalog_v2(observation, legal)
    mandatory_choices = [
        row["choice"]
        for row in catalog
        if row["action_kind"] in {kind.value for kind in observation.mandatory_action_kinds}
    ]
    end_choice = next(
        row["choice"] for row in catalog if row["action_kind"] == "end_turn"
    )
    client = _FakeClient(_reply(spec.model_id, [*mandatory_choices, end_choice]))
    attempt = await capture_attempt_v2(
        spec=spec,
        client=client,
        fixture=fixture,
        attempt_number=1,
        fixture_manifest_sha256=manifest["manifest_sha256"],
        source=SOURCE,
    )

    assert attempt["success"] is True
    assert attempt["model_attributed"] is True
    assert attempt["post_delta"] == 1
    assert attempt["provider"]["max_tokens"] == MAX_TOKENS
    assert attempt["provider"]["post_ceiling"] == POST_CEILING
    assert "content" not in attempt
    assert "response_body" not in attempt
    assert "api_key_env" not in json.dumps(attempt)
    proposal = TurnProposalV2.from_doc(attempt["proposal"])
    assert proposal.policy_id == provider_policy_v2(spec).descriptor_id
    assert proposal.observation_id == observation.observation_id
    assert proposal.intents[-1].action_kind.value == "end_turn"

    write_attempt_v2(tmp_path, spec, attempt)
    stored = json.loads(
        (tmp_path / spec.provider_id / "attempts/001.json").read_text(encoding="utf-8")
    )
    assert stored == attempt


@pytest.mark.asyncio
async def test_capture_blocks_model_misattribution_without_persisting_reply_text() -> None:
    spec = PROVIDERS[0]
    manifest = await build_fixture_manifest_v2((fixture_recipes_v2()[0],))
    fixture = manifest["body"]["fixtures"][0]
    client = _FakeClient(_reply("different-model", [0, 1]))
    attempt = await capture_attempt_v2(
        spec=spec,
        client=client,
        fixture=fixture,
        attempt_number=1,
        fixture_manifest_sha256=manifest["manifest_sha256"],
        source=SOURCE,
    )
    assert attempt["success"] is False
    assert attempt["failure_code"] == "invalid_provider_proposal"
    assert attempt["model_attributed"] is False
    assert attempt["proposal"] is None
    assert attempt["returned_model"] == "different-model"


@pytest.mark.asyncio
async def test_capture_prompt_is_observation_only_and_identity_free() -> None:
    manifest = await build_fixture_manifest_v2((fixture_recipes_v2()[0],))
    fixture = manifest["body"]["fixtures"][0]
    observation, legal = await observed_fixture_v2(fixture)
    prompt = capture_prompt_v2(observation, candidate_catalog_v2(observation, legal))
    assert fixture["fixture_id"] not in prompt
    assert "api_key" not in prompt
    assert "private_state" not in prompt
    assert observation.observation_id in prompt


def test_global_pilot_requires_exactly_twenty_attributable_posts() -> None:
    manifest_sha = "f" * 64
    summaries = []
    for spec in PROVIDERS:
        body = {
            "source": SOURCE,
            "stage": "pilot",
            "fixture_manifest_sha256": manifest_sha,
            "passed": True,
            "attempts": 10,
            "posts": 10,
            "successes": 10,
            "provider": spec.public_doc(),
        }
        summaries.append(
            {
                "schema": 2,
                "capture_sha256": sha256_hex(canonical(body)),
                "body": body,
            }
        )
    gate = build_global_pilot_gate_v2(manifest_sha, summaries)
    assert gate["body"]["attempts"] == 20
    assert gate["body"]["posts"] == 20
    assert gate["body"]["passed"] is True

    summaries[0]["body"]["posts"] = 9
    summaries[0]["capture_sha256"] = sha256_hex(canonical(summaries[0]["body"]))
    blocked = build_global_pilot_gate_v2(manifest_sha, summaries)
    assert blocked["body"]["passed"] is False
    assert blocked["body"]["status"] == "BLOCKED"


def test_provider_source_identity_requires_full_git_shas() -> None:
    assert source_identity_v2("a" * 40, "b" * 40) == SOURCE
    with pytest.raises(ContractError, match="full Git SHA"):
        source_identity_v2("short", "b" * 40)
