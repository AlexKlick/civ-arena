"""Canonical request identities and provider-counter receipts; no estimated hard limit."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol


def encoded(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def payload_hash(value: dict) -> str:
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def input_payload(model: str, *, system: str, messages: list[dict], tools: list[dict],
                  tool_choice: dict | None = None) -> dict:
    body = {'model': model, 'system': system, 'messages': messages, 'tools': tools}
    if tool_choice is not None:
        body['tool_choice'] = tool_choice
    return copy.deepcopy(body)


@dataclass(frozen=True)
class TokenCount:
    input_tokens: int
    model: str
    input_payload_sha256: str
    source: str = 'provider_count_tokens'


@dataclass(frozen=True)
class GenerationAdmission:
    """Immutable serialized dispatch body bound to an exact counter receipt.

    This is an in-process admission, not a replay authorization. Parsing yields
    a fresh copy, so callers cannot mutate the body admitted for transport.
    """
    request_json: str
    receipt: TokenCount
    provider_context_tokens: int

    def body(self) -> dict:
        body = json.loads(self.request_json)
        if not isinstance(body, dict) or encoded(body) != self.request_json:
            raise ValueError('noncanonical admitted request')
        required = {'model', 'system', 'messages', 'tools', 'max_tokens'}
        if not required <= body.keys() or body.keys() - (required | {'tool_choice'}):
            raise ValueError('unsupported admitted request fields')
        reserve = body['max_tokens']
        receipt = self.receipt
        if (not isinstance(receipt, TokenCount) or type(receipt.input_tokens) is not int
                or receipt.input_tokens <= 0 or type(reserve) is not int or reserve <= 0
                or type(self.provider_context_tokens) is not int
                or receipt.input_tokens + reserve > self.provider_context_tokens
                or receipt.source not in ('provider_count_tokens', 'injected_token_counter')
                or receipt.model != body['model']
                or receipt.input_payload_sha256 != payload_hash(
                    {key: value for key, value in body.items() if key != 'max_tokens'})):
            raise ValueError('admitted request does not match token count/window')
        return body


class TokenCounter(Protocol):
    async def count_tokens(self, *, system: str, messages: list[dict], tools: list[dict],
                           tool_choice: dict | None = None) -> TokenCount: ...


def task_for(reasons: list[str], opening_actions_pending: bool) -> str:
    if not opening_actions_pending:
        return 'economy'
    if any(reason in reasons for reason in ('new_visible_contact', 'owned_unit_damaged',
                                            'owned_unit_lost', 'tactical_control_requested')):
        return 'contact'
    return 'strategy'


def measurements(body: dict, output_reserve: int) -> dict:
    full = {**body, 'max_tokens': output_reserve}
    return {'input_payload_sha256': payload_hash(body),
            'request_payload_sha256': payload_hash(full),
            'canonical_input_bytes': len(encoded(body).encode('utf-8')),
            'canonical_request_bytes': len(encoded(full).encode('utf-8')),
            'input_bytes_by_field': {key: len(encoded(value).encode('utf-8'))
                                     for key, value in body.items()},
            'output_reserve_tokens': output_reserve,
            'local_estimate': {'input_tokens': len(encoded(body).encode('utf-8')),
                              'method': 'serialized_utf8_bytes_diagnostic_only',
                              'provider_exact': False, 'hard_admission_authority': False}}
