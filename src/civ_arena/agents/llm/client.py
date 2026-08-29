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
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from civ_arena.config import LLMSpec

ANTHROPIC_VERSION = "2023-06-01"
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


class ModelUnavailable(RuntimeError):
    """The model endpoint cannot be used (auth, network, budget, shape)."""


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
                     tools: list[dict[str, Any]]) -> ModelReply: ...

    async def aclose(self) -> None: ...


class MiniMaxMessagesClient:
    """One shared AsyncClient, lazily created; close via ``aclose``."""

    def __init__(self, spec: LLMSpec, auth_style: str = "x-api-key") -> None:
        self.spec = spec
        self.auth_style = auth_style
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
        content = doc.get("content")
        if not isinstance(content, list) or not all(
                isinstance(b, dict) and isinstance(b.get("type"), str)
                for b in content):
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
                     tools: list[dict[str, Any]]) -> ModelReply:
        url = self.spec.base_url.rstrip("/") + "/messages"
        body = {
            "model": self.spec.model_id,
            "max_tokens": self.spec.max_tokens,
            "system": system,
            "messages": messages,
            "tools": tools,
        }
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.spec.request_timeout_s))

        last_error = "no attempt made"
        for attempt in range(self.spec.max_retries + 1):
            self._budget_check()
            # capture the key BEFORE posting: redaction must target the
            # credential actually sent, even if the env var rotates mid-flight
            sent_key = self._api_key()
            # count the attempt immediately before the POST itself (after key
            # resolution): a missing key burns nothing, a transport failure
            # mid-flight still consumed a slot (the server may have received it)
            self.posts_sent += 1
            try:
                resp = await self._http.post(
                    url, json=body,
                    headers={"anthropic-version": ANTHROPIC_VERSION,
                             **({"Authorization": f"Bearer {sent_key}"}
                                if self.auth_style == "bearer"
                                else {"x-api-key": sent_key})})
            except httpx.TransportError as exc:
                last_error = f"transport error: {exc}"
                if attempt < self.spec.max_retries:
                    await asyncio.sleep(0.5 * 2 ** attempt)
                    continue
                raise ModelUnavailable(last_error) from exc
            if resp.status_code == 200:
                try:
                    return self._parse(resp.json())
                except ValueError as exc:  # invalid JSON in a 200 body
                    raise ModelUnavailable(
                        f"malformed response: body is not JSON ({exc})"
                    ) from exc
            if resp.status_code in _RETRYABLE_STATUS \
                    and attempt < self.spec.max_retries:
                await asyncio.sleep(0.5 * 2 ** attempt)
                continue
            # 4xx and unretryable 5xx: fail immediately, carry a REDACTED body
            # (a reflecting proxy can echo the key back; it must never reach
            # the durable log via MatchAborted). Redact BEFORE slicing: a key
            # straddling the cut would leak its prefix.
            raise ModelUnavailable(
                f"HTTP {resp.status_code} from {self.spec.model_id}: "
                f"{self._snippet(self._redact(resp.text, sent_key))}"
            )
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
        the cut: if the limit lands inside '<redacted…', close the marker
        so the snippet still SHOWS where a secret was removed."""
        snippet = redacted_text[:limit]
        if len(redacted_text) > limit and "<" in snippet:
            cut = snippet.rindex("<")
            if not snippet[cut:].endswith(">"):
                snippet = snippet[:cut] + "<redacted>"
        return snippet

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
