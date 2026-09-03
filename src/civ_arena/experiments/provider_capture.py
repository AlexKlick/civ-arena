"""Redacted, attributable proposal capture for CAR-M1 model strata.

Only normalized ``TurnProposalV2`` documents and cryptographic digests of the
prompt/parsed response are durable.  API keys, headers, raw HTTP bodies, and
response text have no field in an attempt artifact.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from civ_arena.agents.llm.client import (
    MiniMaxMessagesClient,
    ModelReply,
    ModelUnavailable,
    text_of,
    tool_uses,
)
from civ_arena.canonical import canonical, sha256_hex
from civ_arena.config import LLMSpec
from civ_arena.experiments.turn_core import (
    EXPERIMENT_ID,
    observed_fixture_v2,
    validate_fixture_manifest_v2,
    write_immutable_json,
)
from civ_arena.v2.contracts import (
    PLAYER_ACTION_KINDS_V2,
    ActionIntentV2,
    ActionKindV2,
    EntityRefV2,
    LegalActionSetV2,
    LegalActionV2,
    ObservationV2,
    PolicyDescriptorV2,
    PolicyKindV2,
    TurnProposalV2,
)
from civ_arena.v2.schemas import ContractError

MAX_TOKENS = 1024
POST_CEILING = 120
PILOT_FIXTURES = 10
CAPTURE_SCHEMA = 2
SUBMIT_TOOL = "submit_turn_proposal"
FORBIDDEN_DURABLE_KEYS = frozenset(
    {
        "api_key",
        "api_key_env",
        "authorization",
        "cookie",
        "headers",
        "password",
        "raw_http_body",
        "response_body",
        "secret",
    }
)


@dataclass(frozen=True)
class ProviderCaptureSpecV2:
    provider_id: str
    base_url: str
    api_key_env: str
    model_id: str
    auth_style: str = "x-api-key"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", self.provider_id):
            raise ContractError("provider_id must be a safe lowercase slug")
        if self.auth_style not in {"x-api-key", "bearer"}:
            raise ContractError("provider auth style is unsupported")
        if not self.model_id:
            raise ContractError("provider capture requires a model id")

    def public_doc(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "base_url": self.base_url,
            "model_id": self.model_id,
            "auth_style": self.auth_style,
            "max_tokens": MAX_TOKENS,
            "post_ceiling": POST_CEILING,
        }

    def llm_spec(self) -> LLMSpec:
        return LLMSpec(
            base_url=self.base_url,
            api_key_env=self.api_key_env,
            model_id=self.model_id,
            max_tokens=MAX_TOKENS,
            max_tool_rounds=1,
            max_result_chars=8_000,
            request_timeout_s=120.0,
            max_retries=0,
            max_requests_per_match=POST_CEILING,
        )


PROVIDERS = (
    ProviderCaptureSpecV2(
        "minimax-m3",
        "https://api.minimax.io/anthropic/v1",
        "ANTHROPIC_AUTH_TOKEN_MINIMAX2",
        "MiniMax-M3",
    ),
    ProviderCaptureSpecV2(
        "glm-5-3",
        "https://api.z.ai/api/anthropic/v1",
        "ANTHROPIC_AUTH_TOKEN_ZAI",
        "glm-5.3",
    ),
)


class CaptureClient(Protocol):
    posts_sent: int

    async def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply: ...

    async def aclose(self) -> None: ...


def source_identity_v2(commit: str, tree: str) -> dict[str, str]:
    for label, value in (("commit", commit), ("tree", tree)):
        if (
            not isinstance(value, str)
            or len(value) != 40
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ContractError(f"provider source {label} must be a full Git SHA")
    return {"commit": commit, "tree": tree}


def provider_policy_v2(spec: ProviderCaptureSpecV2) -> PolicyDescriptorV2:
    return PolicyDescriptorV2.create(
        policy_kind=PolicyKindV2.LLM,
        policy_version="car-m1-provider-capture-v2",
        provider=spec.provider_id,
        model=spec.model_id,
        registered_action_kinds=PLAYER_ACTION_KINDS_V2,
    )


def _template(action: LegalActionV2) -> dict[str, Any]:
    return {
        "action_kind": action.action_kind.value,
        "actor": action.actor.to_doc(),
        "target": action.target.to_doc() if action.target is not None else None,
        "parameters": dict(action.parameters),
    }


def candidate_catalog_v2(
    observation: ObservationV2,
    legal: LegalActionSetV2,
) -> list[dict[str, Any]]:
    """Build a small choice catalog entirely from player-observable inputs."""

    candidates: list[dict[str, Any]] = []
    mandatory_by_kind: dict[ActionKindV2, LegalActionV2] = {}
    for action in legal.actions:
        if action.mandatory:
            mandatory_by_kind.setdefault(action.action_kind, action)
    for action in sorted(mandatory_by_kind.values(), key=lambda item: item.action_id):
        candidates.append(_template(action))

    candidates.append(
        {
            "action_kind": ActionKindV2.END_TURN.value,
            "actor": observation.observing_player.to_doc(),
            "target": None,
            "parameters": {},
        }
    )
    optional_kinds = (
        ActionKindV2.FORTIFY,
        ActionKindV2.MOVE_UNIT,
        ActionKindV2.ATTACK,
        ActionKindV2.FOUND_CITY,
        ActionKindV2.PURCHASE,
    )
    for kind in optional_kinds:
        action = next((item for item in legal.actions if item.action_kind is kind), None)
        if action is not None:
            candidates.append(_template(action))
    return [
        {"choice": index, **candidate}
        for index, candidate in enumerate(candidates)
    ]


def capture_prompt_v2(
    observation: ObservationV2,
    candidates: Sequence[Mapping[str, Any]],
) -> str:
    prompt_doc = {
        "schema": CAPTURE_SCHEMA,
        "task": (
            "Choose one complete turn proposal using only the observation and "
            "candidate templates. Include exactly one candidate for every mandatory "
            "action kind, optionally one nonterminal action, and end_turn LAST. "
            "Call submit_turn_proposal with only the ordered integer choices."
        ),
        "observation": observation.to_doc(),
        "candidates": [dict(item) for item in candidates],
    }
    return canonical(prompt_doc)


def _submit_tool(candidate_count: int) -> dict[str, Any]:
    return {
        "name": SUBMIT_TOOL,
        "description": "Submit the ordered candidate indexes for this turn.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "choices": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0, "maximum": candidate_count - 1},
                    "minItems": 1,
                    "maxItems": candidate_count,
                    "uniqueItems": True,
                }
            },
            "required": ["choices"],
        },
    }


def _choice_doc(reply: ModelReply) -> Mapping[str, Any]:
    uses = [use for use in tool_uses(reply) if use.get("name") == SUBMIT_TOOL]
    if uses:
        value = uses[0].get("input")
        if isinstance(value, dict):
            return value
        raise ContractError("provider submit tool input is not an object")
    text = text_of(reply).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ContractError("provider response contains no proposal object")
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        raise ContractError("provider response proposal is not JSON") from None
    if not isinstance(value, dict):
        raise ContractError("provider response proposal is not an object")
    return value


def normalize_reply_v2(
    reply: ModelReply,
    observation: ObservationV2,
    legal: LegalActionSetV2,
    policy: PolicyDescriptorV2,
) -> TurnProposalV2:
    candidates = candidate_catalog_v2(observation, legal)
    raw = _choice_doc(reply)
    if set(raw) != {"choices"}:
        raise ContractError("provider proposal has unknown or missing fields")
    choices = raw["choices"]
    if (
        not isinstance(choices, list)
        or not choices
        or any(type(choice) is not int for choice in choices)
        or len(set(choices)) != len(choices)
        or any(not 0 <= choice < len(candidates) for choice in choices)
    ):
        raise ContractError("provider proposal choices are invalid")
    selected = [candidates[choice] for choice in choices]
    if selected[-1]["action_kind"] != ActionKindV2.END_TURN.value:
        raise ContractError("provider proposal must place end_turn last")
    selected_kinds = {item["action_kind"] for item in selected}
    mandatory = {item.value for item in observation.mandatory_action_kinds}
    if not mandatory.issubset(selected_kinds):
        raise ContractError("provider proposal omits a mandatory action kind")

    intents = [
        ActionIntentV2.create(
            ActionKindV2(item["action_kind"]),
            EntityRefV2.from_doc(item["actor"]),
            target=(
                EntityRefV2.from_doc(item["target"])
                if item["target"] is not None
                else None
            ),
            parameters=item["parameters"],
            proposal_index=index,
        )
        for index, item in enumerate(selected)
    ]
    return TurnProposalV2.create(
        policy_id=policy.descriptor_id,
        observation_id=observation.observation_id,
        intents=intents,
    )


def _assert_redacted_shape(value: Any, path: str = "$capture") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in FORBIDDEN_DURABLE_KEYS:
                raise ContractError(f"capture contains prohibited field at {path}.{key}")
            _assert_redacted_shape(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_redacted_shape(child, f"{path}[{index}]")


def _model_attributed(spec: ProviderCaptureSpecV2, returned_model: str) -> bool:
    return returned_model.casefold() == spec.model_id.casefold()


async def capture_attempt_v2(
    *,
    spec: ProviderCaptureSpecV2,
    client: CaptureClient,
    fixture: Mapping[str, Any],
    attempt_number: int,
    fixture_manifest_sha256: str,
    source: Mapping[str, str],
) -> dict[str, Any]:
    source_doc = source_identity_v2(source.get("commit", ""), source.get("tree", ""))
    observation, legal = await observed_fixture_v2(fixture)
    candidates = candidate_catalog_v2(observation, legal)
    prompt = capture_prompt_v2(observation, candidates)
    tools = [_submit_tool(len(candidates))]
    request_doc = {
        "model": spec.model_id,
        "max_tokens": MAX_TOKENS,
        "system": "Use only the supplied observation; submit one structured proposal.",
        "messages": [{"role": "user", "content": prompt}],
        "tools": tools,
    }
    before_posts = client.posts_sent
    returned_model: str | None = None
    response_sha256: str | None = None
    input_tokens = 0
    output_tokens = 0
    proposal: TurnProposalV2 | None = None
    failure_code: str | None = None
    attributed = False
    try:
        reply = await client.create(
            system=request_doc["system"],
            messages=request_doc["messages"],
            tools=tools,
        )
        returned_model = reply.model
        input_tokens = reply.input_tokens
        output_tokens = reply.output_tokens
        response_sha256 = sha256_hex(
            canonical(
                {
                    "content": reply.content,
                    "stop_reason": reply.stop_reason,
                    "model": reply.model,
                    "input_tokens": reply.input_tokens,
                    "output_tokens": reply.output_tokens,
                }
            )
        )
        attributed = _model_attributed(spec, reply.model)
        if not attributed:
            raise ContractError("provider response model identity mismatch")
        proposal = normalize_reply_v2(
            reply,
            observation,
            legal,
            provider_policy_v2(spec),
        )
    except ModelUnavailable:
        failure_code = "model_unavailable"
    except (ContractError, TypeError, ValueError):
        failure_code = "invalid_provider_proposal"
    post_delta = client.posts_sent - before_posts
    if post_delta != 1:
        failure_code = "provider_post_accounting_invalid"
        proposal = None
    proposal_doc = proposal.to_doc() if proposal is not None else None
    attempt = {
        "schema": CAPTURE_SCHEMA,
        "experiment_id": EXPERIMENT_ID,
        "fixture_manifest_sha256": fixture_manifest_sha256,
        "source": source_doc,
        "attempt": attempt_number,
        "fixture_id": fixture["fixture_id"],
        "provider": spec.public_doc(),
        "request_sha256": sha256_hex(canonical(request_doc)),
        "prompt_sha256": sha256_hex(prompt),
        "response_sha256": response_sha256,
        "returned_model": returned_model,
        "model_attributed": attributed,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "post_delta": post_delta,
        "success": proposal is not None and failure_code is None,
        "failure_code": failure_code,
        "proposal_sha256": (
            sha256_hex(canonical(proposal_doc)) if proposal_doc is not None else None
        ),
        "proposal": proposal_doc,
    }
    _assert_redacted_shape(attempt)
    canonical(attempt)
    return attempt


def _attempt_dir(root: Path, spec: ProviderCaptureSpecV2) -> Path:
    return root / spec.provider_id / "attempts"


def load_attempts_v2(
    root: Path,
    spec: ProviderCaptureSpecV2,
    manifest_sha256: str,
    source: Mapping[str, str],
) -> list[dict[str, Any]]:
    source_doc = source_identity_v2(source.get("commit", ""), source.get("tree", ""))
    directory = _attempt_dir(root, spec)
    if not directory.exists():
        return []
    rows: list[dict[str, Any]] = []
    for expected, path in enumerate(sorted(directory.glob("*.json")), start=1):
        if path.name != f"{expected:03d}.json" or path.is_symlink():
            raise ContractError("provider attempt sequence is not contiguous")
        row = json.loads(path.read_text(encoding="utf-8"))
        _assert_redacted_shape(row)
        if set(row) != {
            "schema",
            "experiment_id",
            "fixture_manifest_sha256",
            "source",
            "attempt",
            "fixture_id",
            "provider",
            "request_sha256",
            "prompt_sha256",
            "response_sha256",
            "returned_model",
            "model_attributed",
            "input_tokens",
            "output_tokens",
            "post_delta",
            "success",
            "failure_code",
            "proposal_sha256",
            "proposal",
        }:
            raise ContractError("provider attempt has unknown or missing fields")
        if row.get("attempt") != expected or row.get("provider") != spec.public_doc():
            raise ContractError("provider attempt identity mismatch")
        if row.get("experiment_id") != EXPERIMENT_ID:
            raise ContractError("provider attempt names the wrong experiment")
        if row.get("fixture_manifest_sha256") != manifest_sha256:
            raise ContractError("provider attempt fixture manifest mismatch")
        if row.get("source") != source_doc:
            raise ContractError("provider attempt source identity mismatch")
        if row.get("post_delta") not in {0, 1}:
            raise ContractError("provider attempt POST accounting is invalid")
        for field in ("input_tokens", "output_tokens"):
            if type(row.get(field)) is not int or row[field] < 0:
                raise ContractError("provider attempt usage is invalid")
        rows.append(row)
    if sum(row["post_delta"] for row in rows) > POST_CEILING:
        raise ContractError("provider attempts exceed the POST ceiling")
    if len(manifest_sha256) != 64:
        raise ContractError("provider capture manifest identity is malformed")
    return rows


def write_attempt_v2(
    root: Path,
    spec: ProviderCaptureSpecV2,
    attempt: Mapping[str, Any],
) -> None:
    path = _attempt_dir(root, spec) / f"{attempt['attempt']:03d}.json"
    write_immutable_json(path, attempt)


def _summary_envelope(body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": CAPTURE_SCHEMA,
        "capture_sha256": sha256_hex(canonical(dict(body))),
        "body": dict(body),
    }


def _validated_summary(doc: Mapping[str, Any], label: str) -> dict[str, Any]:
    if set(doc) != {"schema", "capture_sha256", "body"} or doc.get("schema") != 2:
        raise ContractError(f"{label} envelope is invalid")
    body = doc.get("body")
    if not isinstance(body, dict) or sha256_hex(canonical(body)) != doc["capture_sha256"]:
        raise ContractError(f"{label} digest mismatch")
    _assert_redacted_shape(doc)
    return body


async def capture_pilot_provider_v2(
    *,
    manifest_doc: Mapping[str, Any],
    root: Path,
    spec: ProviderCaptureSpecV2,
    client: CaptureClient,
    source: Mapping[str, str],
) -> dict[str, Any]:
    source_doc = source_identity_v2(source.get("commit", ""), source.get("tree", ""))
    body = validate_fixture_manifest_v2(manifest_doc)
    fixtures = body["fixtures"][:PILOT_FIXTURES]
    attempts = load_attempts_v2(
        root,
        spec,
        manifest_doc["manifest_sha256"],
        source_doc,
    )
    if len(attempts) > PILOT_FIXTURES:
        raise ContractError("pilot directory already contains post-pilot attempts")
    client.posts_sent = sum(row["post_delta"] for row in attempts)
    for index in range(len(attempts), PILOT_FIXTURES):
        attempt = await capture_attempt_v2(
            spec=spec,
            client=client,
            fixture=fixtures[index],
            attempt_number=index + 1,
            fixture_manifest_sha256=manifest_doc["manifest_sha256"],
            source=source_doc,
        )
        write_attempt_v2(root, spec, attempt)
        attempts.append(attempt)
    passed = (
        len(attempts) == PILOT_FIXTURES
        and sum(row["post_delta"] for row in attempts) == PILOT_FIXTURES
        and all(row["success"] and row["model_attributed"] for row in attempts)
    )
    summary = _summary_envelope(
        {
            "schema": CAPTURE_SCHEMA,
            "stage": "pilot",
            "experiment_id": EXPERIMENT_ID,
            "fixture_manifest_sha256": manifest_doc["manifest_sha256"],
            "source": source_doc,
            "provider": spec.public_doc(),
            "attempts": len(attempts),
            "posts": sum(row["post_delta"] for row in attempts),
            "successes": sum(row["success"] for row in attempts),
            "input_tokens": sum(row["input_tokens"] for row in attempts),
            "output_tokens": sum(row["output_tokens"] for row in attempts),
            "passed": passed,
            "status": "PASS" if passed else "BLOCKED",
        }
    )
    write_immutable_json(root / spec.provider_id / "pilot.json", summary)
    return summary


def build_global_pilot_gate_v2(
    manifest_sha256: str,
    summaries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if len(summaries) != len(PROVIDERS):
        raise ContractError("global pilot requires every provider stratum")
    bodies = [_validated_summary(summary, "provider pilot") for summary in summaries]
    if {body.get("provider", {}).get("provider_id") for body in bodies} != {
        spec.provider_id for spec in PROVIDERS
    }:
        raise ContractError("global pilot provider identities are incomplete")
    if any(
        body.get("fixture_manifest_sha256") != manifest_sha256
        or body.get("stage") != "pilot"
        for body in bodies
    ):
        raise ContractError("global pilot summaries do not bind the fixture manifest")
    sources = {canonical(body.get("source")) for body in bodies}
    passed = (
        all(body["passed"] for body in bodies)
        and sum(body["attempts"] for body in bodies) == 20
        and sum(body["posts"] for body in bodies) == 20
        and len(sources) == 1
    )
    return _summary_envelope(
        {
            "schema": CAPTURE_SCHEMA,
            "stage": "global_pilot_gate",
            "experiment_id": EXPERIMENT_ID,
            "fixture_manifest_sha256": manifest_sha256,
            "source": bodies[0]["source"],
            "provider_capture_sha256s": sorted(
                summary["capture_sha256"] for summary in summaries
            ),
            "attempts": sum(body["attempts"] for body in bodies),
            "posts": sum(body["posts"] for body in bodies),
            "successes": sum(body["successes"] for body in bodies),
            "passed": passed,
            "status": "PASS" if passed else "BLOCKED",
        }
    )


async def capture_full_provider_v2(
    *,
    manifest_doc: Mapping[str, Any],
    root: Path,
    spec: ProviderCaptureSpecV2,
    client: CaptureClient,
    source: Mapping[str, str],
) -> dict[str, Any]:
    source_doc = source_identity_v2(source.get("commit", ""), source.get("tree", ""))
    body = validate_fixture_manifest_v2(manifest_doc)
    pilot_gate = json.loads((root / "pilot-gate.json").read_text(encoding="utf-8"))
    pilot_body = _validated_summary(pilot_gate, "global pilot")
    if (
        pilot_body.get("fixture_manifest_sha256") != manifest_doc["manifest_sha256"]
        or pilot_body.get("passed") is not True
        or pilot_body.get("source") != source_doc
    ):
        raise ContractError("full capture requires the passing 20-attempt pilot gate")
    attempts = load_attempts_v2(
        root,
        spec,
        manifest_doc["manifest_sha256"],
        source_doc,
    )
    if len(attempts) < PILOT_FIXTURES or not all(
        row["success"] for row in attempts[:PILOT_FIXTURES]
    ):
        raise ContractError("full capture requires ten successful provider pilot rows")
    captured = {
        row["fixture_id"]: row
        for row in attempts
        if row["success"]
    }
    client.posts_sent = sum(row["post_delta"] for row in attempts)
    fixture_by_id = {row["fixture_id"]: row for row in body["fixtures"]}
    for fixture in body["fixtures"]:
        fixture_id = fixture["fixture_id"]
        while fixture_id not in captured:
            if len(attempts) >= POST_CEILING or client.posts_sent >= POST_CEILING:
                break
            attempt = await capture_attempt_v2(
                spec=spec,
                client=client,
                fixture=fixture_by_id[fixture_id],
                attempt_number=len(attempts) + 1,
                fixture_manifest_sha256=manifest_doc["manifest_sha256"],
                source=source_doc,
            )
            write_attempt_v2(root, spec, attempt)
            attempts.append(attempt)
            if attempt["success"]:
                captured[fixture_id] = attempt
            elif attempt["post_delta"] != 1:
                break
        if fixture_id not in captured:
            break
    complete = len(captured) == len(body["fixtures"])
    proposals = [
        {
            "fixture_id": fixture["fixture_id"],
            "proposal_sha256": captured[fixture["fixture_id"]]["proposal_sha256"],
            "proposal": captured[fixture["fixture_id"]]["proposal"],
        }
        for fixture in body["fixtures"]
        if fixture["fixture_id"] in captured
    ]
    corpus_body = {
        "schema": CAPTURE_SCHEMA,
        "stage": "full_capture",
        "experiment_id": EXPERIMENT_ID,
        "fixture_manifest_sha256": manifest_doc["manifest_sha256"],
        "source": source_doc,
        "provider": spec.public_doc(),
        "policy": provider_policy_v2(spec).to_doc(),
        "attempts": len(attempts),
        "posts": sum(row["post_delta"] for row in attempts),
        "successes": len(proposals),
        "input_tokens": sum(row["input_tokens"] for row in attempts),
        "output_tokens": sum(row["output_tokens"] for row in attempts),
        "complete": complete,
        "status": "PASS" if complete else "BLOCKED",
        "proposals": proposals,
    }
    corpus = _summary_envelope(corpus_body)
    write_immutable_json(root / spec.provider_id / "proposals.json", corpus)
    return corpus


def proposal_docs_from_corpus_v2(
    corpus: Mapping[str, Any],
) -> tuple[PolicyDescriptorV2, dict[str, Mapping[str, Any]], str]:
    body = _validated_summary(corpus, "provider proposal corpus")
    if body.get("complete") is not True:
        raise ContractError("provider proposal corpus is not complete")
    policy = PolicyDescriptorV2.from_doc(body["policy"])
    proposals: dict[str, Mapping[str, Any]] = {}
    for row in body["proposals"]:
        proposal = TurnProposalV2.from_doc(row["proposal"])
        if sha256_hex(canonical(proposal.to_doc())) != row["proposal_sha256"]:
            raise ContractError("provider proposal row digest mismatch")
        if row["fixture_id"] in proposals:
            raise ContractError("provider proposal fixture ids are not unique")
        proposals[row["fixture_id"]] = proposal.to_doc()
    if len(proposals) != 100:
        raise ContractError("provider proposal corpus must contain 100 fixtures")
    return policy, proposals, corpus["capture_sha256"]


def new_provider_client(spec: ProviderCaptureSpecV2) -> MiniMaxMessagesClient:
    return MiniMaxMessagesClient(spec.llm_spec(), auth_style=spec.auth_style)
