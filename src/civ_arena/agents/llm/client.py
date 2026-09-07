"""The Messages-compatible HTTP client — the ONLY network code in the repo.

Speaks the Anthropic Messages wire shape (proven against MiniMax's
``/anthropic/v1`` plan path; see docs/llm-lane.md for the recorded wire
spike). Two properties are pinned by tests:

- the api key is read from the named env var AT REQUEST TIME and never
  appears in any exception, log line, or repr;
- EVERY posted request counts toward the match budget, retries included —
  a budget that ignores retries lies about spend.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from typing import Any, Protocol

import httpx

from civ_arena.agents.llm.request_budget import (
    GenerationAdmission,
    TokenCount,
    encoded,
    input_payload,
    payload_hash,
)
from civ_arena.config import LLMSpec

ANTHROPIC_VERSION = "2023-06-01"
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


def _utcnow_iso() -> str:
    from datetime import datetime
    return datetime.now(UTC).isoformat()


class ModelUnavailable(RuntimeError):
    """The model endpoint cannot be used (auth, network, budget, shape)."""


def _normalize_blocks(content: Any) -> list[dict[str, Any]] | None:
    """Provider-shape tolerance (live-learned glm-g1, Z.AI): the anthropic-
    compat spec says content is a list of typed blocks, but providers ship
    plain strings (whole-content or mixed into the list). Strings wrap into
    text blocks; anything else fails LOUD — a silently dropped block is
    worse than a refused response."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        out: list[dict[str, Any]] = []
        for b in content:
            if isinstance(b, str):
                out.append({"type": "text", "text": b})
            elif isinstance(b, dict) and isinstance(b.get("type"), str):
                out.append(b)
            else:
                return None
        return out
    return None


@dataclass(frozen=True)
class ModelReply:
    content: list[dict[str, Any]]  # verbatim block list (thinking included)
    stop_reason: str | None
    model: str
    input_tokens: int
    output_tokens: int


def text_of(reply: ModelReply) -> str:
    """Answer text = concatenated ``type == "text"`` blocks ONLY. Thinking
    blocks (M2 always, M3 sometimes) are structurally skipped."""
    return "".join(b.get("text", "") for b in reply.content
                   if b.get("type") == "text")


def tool_uses(reply: ModelReply) -> list[dict[str, Any]]:
    """``type == "tool_use"`` blocks, in order."""
    return [b for b in reply.content if b.get("type") == "tool_use"]


class ModelClient(Protocol):
    async def create(self, *, system: str, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]],
                     tool_choice: dict[str, Any] | None = None) -> ModelReply: ...

    async def aclose(self) -> None: ...


class MiniMaxMessagesClient:
    """One shared AsyncClient, lazily created; close via ``aclose``.

    ``on_post`` (set by the coordinator) fires on EVERY counted attempt —
    the durable spend ledger depends on it (see coordinator spend.jsonl)."""

    def __init__(self, spec: LLMSpec, auth_style: str = "x-api-key",
                 on_post: Callable[[], None] | None = None,
                 on_request_post: Callable[[str], None] | None = None,
                 on_attempt: Callable[[dict[str, Any]], None] | None = None
                 ) -> None:
        self.spec = spec
        self.auth_style = auth_style
        self.on_post = on_post
        self.on_request_post = on_request_post
        # spectator-capture lane: fires after EVERY counted attempt
        # (success, retryable failure, terminal failure) with the full
        # request/response record — powers llm_costs.jsonl + the opt-in
        # wire transcript. on_post semantics are untouched (budget/spend
        # parity depends on them).
        self.on_attempt = on_attempt
        self.posts_by_kind = {'generation': 0, 'count_tokens': 0}
        self.posts_sent = 0  # every POST, retries included (budget authority)
        self._http: httpx.AsyncClient | None = None

    # ---------------------------------------------------------------- wire
    def _api_key(self) -> str:
        key = os.environ.get(self.spec.api_key_env, "")
        if not key:
            raise ModelUnavailable(
                f"env var {self.spec.api_key_env} is not set — cannot "
                "authenticate the model endpoint"
            )
        return key

    def _budget_check(self) -> None:
        if self.posts_sent >= self.spec.max_requests_per_match:
            raise ModelUnavailable(
                f"request budget exhausted ({self.posts_sent} posts >= "
                f"{self.spec.max_requests_per_match})"
            )

    def _parse(self, doc: Any) -> ModelReply:
        try:
            return self._parse_inner(doc)
        except ModelUnavailable:
            raise
        except (ValueError, TypeError, KeyError, AttributeError,
                ArithmeticError) as exc:
            # any shape surprise (bad JSON top level, non-mapping blocks,
            # non-numeric usage, 1e309-style infinities) degrades to
            # ModelUnavailable — the runtime turns it into MatchAborted and
            # a summary is still written
            raise ModelUnavailable(f"malformed response: {exc}") from exc

    def _parse_inner(self, doc: Any) -> ModelReply:
        if not isinstance(doc, dict):
            raise ModelUnavailable(
                f"malformed response: top level is {type(doc).__name__}, "
                "not an object"
            )
        content = _normalize_blocks(doc.get("content"))
        if content is None:
            raise ModelUnavailable(
                "malformed response: content is not a list of typed blocks"
            )
        usage = doc.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        return ModelReply(
            content=content,
            stop_reason=doc.get("stop_reason"),
            model=str(doc.get("model", self.spec.model_id)),
            input_tokens=int(usage.get("input_tokens",
                                       usage.get("prompt_tokens", 0))),
            output_tokens=int(usage.get("output_tokens",
                                        usage.get("completion_tokens", 0))),
        )

    async def create(self, *, system: str, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]],
                     tool_choice: dict[str, Any] | None = None) -> ModelReply:
        body = input_payload(self.spec.model_id, system=system, messages=messages,
                             tools=tools, tool_choice=tool_choice)
        body['max_tokens'] = self.spec.max_tokens
        return self._parse(await self._post_doc(body, 'generation', '/messages'))

    async def create_admitted(self, *, admission: GenerationAdmission) -> ModelReply:
        try:
            body = admission.body()
        except (ValueError, TypeError, AttributeError) as exc:
            raise ModelUnavailable('invalid generation admission') from exc
        admitted_model = body['model']
        doc = await self._post_doc(body, 'generation', '/messages', admission=admission)
        # A concurrent configuration edit cannot relabel a missing response model.
        if isinstance(doc, dict) and 'model' not in doc:
            doc = {**doc, 'model': admitted_model}
        return self._parse(doc)

    async def count_tokens(self, *, system: str, messages: list[dict[str, Any]],
                           tools: list[dict[str, Any]],
                           tool_choice: dict[str, Any] | None = None) -> TokenCount:
        body = input_payload(self.spec.model_id, system=system, messages=messages,
                             tools=tools, tool_choice=tool_choice)
        try:
            async with asyncio.timeout(self.spec.request_timeout_s):
                doc = await self._post_doc(body, 'count_tokens', '/messages/count_tokens')
        except TimeoutError as exc:
            raise ModelUnavailable('token counter deadline expired') from exc
        tokens = doc.get('input_tokens') if isinstance(doc, dict) else None
        if type(tokens) is not int or tokens < 1:
            raise ModelUnavailable('token counter returned invalid input_tokens')
        if 'model' in doc and doc['model'] != body['model']:
            raise ModelUnavailable('token counter model mismatch')
        return TokenCount(tokens, body['model'], payload_hash(body))

    async def _post_doc(self, body: dict, request_kind: str, path: str, *,
                        admission: GenerationAdmission | None = None) -> Any:
        # Freeze admitted bytes before any await or callback. The legacy path
        # retains its existing JSON serialization and has no token admission.
        dispatch_json = encoded(body) if request_kind == 'count_tokens' else None
        if admission is not None:
            try:
                admitted_body = admission.body()
                if body != admitted_body or request_kind != 'generation' or path != '/messages':
                    raise ValueError('dispatch differs from admission')
            except (ValueError, TypeError, AttributeError) as exc:
                raise ModelUnavailable('invalid generation admission') from exc
            dispatch_json = admission.request_json
        url = self.spec.base_url.rstrip('/') + path
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.spec.request_timeout_s))

        import time as _time

        def _fire(attempt: int, status_code: int | None, latency_ms: int,
                  sent_key: str, response_doc: Any = None,
                  error: str | None = None) -> None:
            if self.on_attempt is None:
                return
            input_tokens = output_tokens = None
            if isinstance(response_doc, dict):
                usage = response_doc.get("usage")
                if isinstance(usage, dict):
                    try:
                        input_tokens = int(usage.get(
                            "input_tokens", usage.get("prompt_tokens", 0)) or 0)
                        output_tokens = int(usage.get(
                            "output_tokens", usage.get("completion_tokens", 0)) or 0)
                    except (TypeError, ValueError):
                        input_tokens = output_tokens = None
            self.on_attempt({
                "ts": _utcnow_iso(), "request_kind": request_kind,
                "attempt": attempt, "status_code": status_code,
                "latency_ms": latency_ms, "model": body.get("model"),
                "payload_hash": payload_hash(body), "request": body,
                "response": response_doc, "error": error,
                "input_tokens": input_tokens, "output_tokens": output_tokens,
            })

        last_error = "no attempt made"
        for attempt in range(self.spec.max_retries + 1):
            if admission is not None and (self.spec.model_id != admitted_body['model']
                    or self.spec.max_tokens != admitted_body['max_tokens']):
                raise ModelUnavailable('admitted generation configuration mismatch')
            self._budget_check()
            # capture the key BEFORE posting: redaction must target the
            # credential actually sent, even if the env var rotates mid-flight
            sent_key = self._api_key()
            # count the attempt immediately before the POST itself (after key
            # resolution): a missing key burns nothing, a transport failure
            # mid-flight still consumed a slot (the server may have received it)
            self.posts_sent += 1
            self.posts_by_kind[request_kind] += 1
            if self.on_request_post is not None:
                self.on_request_post(request_kind)
            if self.on_post is not None:
                self.on_post()  # durable spend ledger (per attempt)
            t0 = _time.monotonic()
            try:
                resp = await self._http.post(
                    url, **({'content': dispatch_json} if dispatch_json is not None
                            else {'json': body}),
                    headers={"anthropic-version": ANTHROPIC_VERSION,
                             **({'content-type': 'application/json'}
                                if dispatch_json is not None else {}),
                             **({"Authorization": f"Bearer {sent_key}"}
                                if self.auth_style == "bearer"
                                else {"x-api-key": sent_key})})
            except httpx.TransportError as exc:
                last_error = self._redact(f"transport error: {exc}", sent_key)
                _fire(attempt, None, int((_time.monotonic() - t0) * 1000),
                      sent_key, error=last_error)
                if attempt < self.spec.max_retries:
                    await asyncio.sleep(0.5 * 2 ** attempt)
                    continue
                raise ModelUnavailable(last_error) from exc
            latency_ms = int((_time.monotonic() - t0) * 1000)
            if resp.status_code == 200:
                try:
                    doc = resp.json()
                except ValueError as exc:  # invalid JSON in a 200 body
                    _fire(attempt, 200, latency_ms, sent_key,
                          error=f"malformed response: body is not JSON ({exc})")
                    raise ModelUnavailable(
                        f"malformed response: body is not JSON ({exc})"
                    ) from exc
                _fire(attempt, 200, latency_ms, sent_key, response_doc=doc)
                return doc
            if resp.status_code in _RETRYABLE_STATUS \
                    and attempt < self.spec.max_retries:
                _fire(attempt, resp.status_code, latency_ms, sent_key,
                      error=f"HTTP {resp.status_code} (retryable)")
                await asyncio.sleep(0.5 * 2 ** attempt)
                continue
            # 4xx and unretryable 5xx: fail immediately, carry a REDACTED body
            # (a reflecting proxy can echo the key back; it must never reach
            # the durable log via MatchAborted). Redact BEFORE slicing: a key
            # straddling the cut would leak its prefix.
            terminal = (f"HTTP {resp.status_code} from {self.spec.model_id}: "
                        f"{self._snippet(self._redact(resp.text, sent_key))}")
            _fire(attempt, resp.status_code, latency_ms, sent_key,
                  error=terminal)
            raise ModelUnavailable(terminal)
        raise ModelUnavailable(last_error)

    @staticmethod
    def _redact(text: str, *keys: str) -> str:
        for key in keys:
            if key:
                text = text.replace(key, "<redacted>")
        return text

    @staticmethod
    def _snippet(redacted_text: str, limit: int = 300) -> str:
        """Truncate REDACTED text without leaving a half-written marker at
        the cut. Only a tail that actually matches a '<redacted…' prefix
        (>= 4 chars — a bare '<' is ordinary text) is closed; anything
        else is left exactly as sliced."""
        marker = "<redacted>"
        snippet = redacted_text[:limit]
        if len(redacted_text) > limit:
            for k in range(len(marker) - 1, 3, -1):
                if snippet.endswith(marker[:k]):
                    snippet = snippet[:-k] + marker
                    break
        return snippet

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
