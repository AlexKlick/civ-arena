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
