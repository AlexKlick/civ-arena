"""Lane D — the provider-portable glm-flash research client.

Speaks the OpenAI chat-completions wire shape with Bearer auth (the zai
coding lane). It deliberately shares no code with
``civ_arena.agents.llm.client.py``, which speaks the Anthropic Messages
shape — but it mirrors that client's DISCIPLINES:

- the api key is read from the named env var AT REQUEST TIME and never
  appears in any log line, spend row, exception, or repr;
- EVERY posted request is counted, retries included — a counter that
  ignores retries lies about spend;
- retry on {429, 500, 502, 503, 504} and on transport errors, with
  full-jitter exponential backoff.

Everything else is fail-soft (the ``planner.proposer`` pattern moved to
the HTTP boundary): a missing key, an exhausted budget, a bad status, an
unreadable body — each returns a typed :class:`FlashResult` instead of
raising. A broken research lane costs one iteration, never a match.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"
API_KEY_ENV = "ZAI_CODING_API_KEY"
MODEL = "glm-5.3-flash"

# the zai lane degrades above 3 under sustained parallelism: hard ceiling
MAX_INFLIGHT = 3
# glm flash models THINK before answering: reasoning_content tokens count
# against max_tokens (measured on glm-4.5-flash: an 8-token probe died at
# finish_reason "length" with empty content; glm-5.3-flash burned a full
# 8192 cap on the roster proposal and truncated mid-answer). Every call
# needs thinking headroom regardless of the pinned model.
MAX_TOKENS = 16384
PROBE_TOKENS = 512
TEMPERATURE = 0.2
BACKOFF_S = 0.5

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class FlashSpec:
    base_url: str = BASE_URL
    api_key_env: str = API_KEY_ENV
    model: str = MODEL
    concurrency: int = 2  # zai lane degrades above 3 under sustained parallelism
    max_retries: int = 3
    # thinking models on large prompts routinely exceed 120s per attempt
    # (measured: 6 consecutive 120s timeouts on the roster-proposal call)
    timeout_s: float = 480.0
    max_calls: int = 12  # hard cap per iteration


def spec_from_env() -> FlashSpec:
    """FlashSpec with operator overrides; unset or blank vars keep defaults."""
    values: dict[str, str] = {}
    for name, var in (("base_url", "RESEARCH_FLASH_BASE_URL"),
                      ("api_key_env", "RESEARCH_FLASH_API_KEY_ENV"),
                      ("model", "RESEARCH_FLASH_MODEL")):
        value = os.environ.get(var, "").strip()
        if value:
            values[name] = value
    return FlashSpec(**values)


@dataclass(frozen=True)
class FlashResult:
    """One logical model call — never an exception.

    ``data`` is {} unless ``status == "ok"``. ``raw`` carries the
    key-redacted model text and is set ONLY on parse_error: enough to
    diagnose a lane, never enough to leak a credential.
    """

    status: str  # ok | parse_error | no_key | budget_exhausted | http_error
    data: dict[str, Any] = field(default_factory=dict)
    raw: str = ""
    call_id: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    attempts: int = 0


def first_json_object(text: str) -> str | None:
    """The first balanced {...} substring of ``text``, string-literal aware
    (a brace inside a JSON string never moves the depth). None when there
    is no balanced object."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def parse_json_object(text: str) -> dict[str, Any] | None:
    """``json.loads`` first; on failure the first balanced {...} substring.
    A non-object payload (list, scalar) is not an answer."""
    for candidate in (text, first_json_object(text)):
        if not candidate:
            continue
        try:
            doc = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict):
            return doc
    return None


def _response_doc(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:  # invalid / empty body on a 200
        return None


def _content(doc: Any) -> str:
    """Answer text from the OpenAI shape; a block-list content (provider
    tolerance, cf. agents/llm/client.py) is flattened to its text parts."""
    if not isinstance(doc, dict):
        return ""
    choices = doc.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content
                       if isinstance(block, dict))
    return ""


def _reasoning_content(doc: Any) -> str:
    """Thinking text from the glm wire shape. A parse SOURCE of last
    resort: thinking models sometimes finish the JSON answer inside the
    reasoning field while the content field stays empty (truncation or
    early-answer behavior). Never preferred over a real content field."""
    if not isinstance(doc, dict):
        return ""
    choices = doc.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    reasoning = message.get("reasoning_content") if isinstance(message, dict) else None
    return reasoning if isinstance(reasoning, str) else ""


def _finish_reason(doc: Any) -> str:
    choices = doc.get("choices") if isinstance(doc, dict) else None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return str(choices[0].get("finish_reason", ""))
    return ""


def _coerce_tokens(usage: dict[str, Any], *keys: str) -> int:
    """Present key -> coerced int (explicit 0 stays 0); a key that is
    absent OR uncoercible reads 0 (cf. agents/llm/client.py)."""
    for key in keys:
        if key in usage:
            try:
                return int(usage[key])
            except (TypeError, ValueError):
                return 0
    return 0


def _token_pair(usage: Any) -> tuple[int, int]:
    """(input, output) tokens; the prompt_tokens/completion_tokens aliases
    are tolerated, an absent or non-mapping usage reads (0, 0)."""
    if not isinstance(usage, dict):
        return 0, 0
    return (_coerce_tokens(usage, "input_tokens", "prompt_tokens"),
            _coerce_tokens(usage, "output_tokens", "completion_tokens"))


def _redact(text: str, key: str) -> str:
    """A reflecting provider can echo the key back inside model text; the
    key must never survive into anything we keep."""
    return text.replace(key, "<redacted>") if key else text


def _jitter(attempt: int) -> float:
    """Full-jitter exponential backoff: uniform in [0, BACKOFF_S * 2**attempt]."""
    return random.uniform(0.0, BACKOFF_S * 2 ** attempt)


class FlashClient:
    """Async chat-completions client with bounded concurrency, retries, and
    a durable per-attempt spend trail.

    ``transport`` is injectable (httpx.MockTransport-compatible) so tests
    never touch the network. ``spend_path`` gets one JSONL row per ATTEMPT:
    {call_id, purpose, model, status, attempt, input_tokens,
    output_tokens} — never the api key, never message content. Rows are
    fsynced per line and parent dirs are created lazily. ``no_key`` and
    ``budget_exhausted`` make no attempt and write no row; a missing key
    burns no budget either.
    """

    def __init__(self, spec: FlashSpec | None = None, *,
                 spend_path: Path | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.spec = spec if spec is not None else FlashSpec()
        self.spend_path = spend_path
        self.calls_sent = 0  # admitted logical calls, retries NOT included
        self.attempts_sent = 0  # every POST, retries included
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._gate: asyncio.Semaphore | None = None

    async def call_json(self, prompt: str, *, purpose: str,
                        system: str | None = None) -> FlashResult:
        """One JSON-object completion for ``purpose``. Fail-soft: never raises."""
        return await self._call(prompt, purpose=purpose, system=system,
                                max_tokens=MAX_TOKENS)

    async def probe(self) -> bool:
        """Minimal connectivity check: True iff the reply parsed as a JSON
        object. The prompt asks for JSON because the request pins
        ``response_format: json_object`` — a prose reply would parse-fail
        and read as a dead lane. max_tokens clears the model's thinking
        phase first (reasoning counts against the cap). It consumes a call
        slot and writes spend rows like any call."""
        result = await self._call('Reply with the JSON object {"pong": true}',
                                  purpose="probe", system=None,
                                  max_tokens=PROBE_TOKENS)
        return result.status == "ok"

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------ internals
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            options: dict[str, Any] = {"timeout": httpx.Timeout(self.spec.timeout_s)}
            if self._transport is not None:
                options["transport"] = self._transport
            self._http = httpx.AsyncClient(**options)
        return self._http

    def _concurrency_gate(self) -> asyncio.Semaphore:
        # created lazily inside a running loop — never bound to a foreign loop
        if self._gate is None:
            self._gate = asyncio.Semaphore(min(self.spec.concurrency, MAX_INFLIGHT))
        return self._gate

    def _spend(self, call_id: str, purpose: str, status: str, attempt: int,
               input_tokens: int, output_tokens: int) -> None:
        """Append one durable spend row. Sync, no awaits: a row cannot be
        interleaved mid-line, and the single-threaded loop makes each
        append atomic."""
        if self.spend_path is None:
            return
        row = {"call_id": call_id, "purpose": purpose, "model": self.spec.model,
               "status": status, "attempt": attempt,
               "input_tokens": input_tokens, "output_tokens": output_tokens}
        self.spend_path.parent.mkdir(parents=True, exist_ok=True)
        with self.spend_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    async def _call(self, prompt: str, *, purpose: str, system: str | None,
                    max_tokens: int) -> FlashResult:
        call_id = uuid.uuid4().hex
        if self.calls_sent >= self.spec.max_calls:
            return FlashResult(status="budget_exhausted", call_id=call_id,
                               model=self.spec.model)
        # the key is resolved AT REQUEST TIME (env may rotate mid-run) and
        # is only ever placed in the request header, never logged or stored
        key = os.environ.get(self.spec.api_key_env, "")
        if not key:
            # a missing key burns nothing (mirrors agents/llm/client.py)
            return FlashResult(status="no_key", call_id=call_id,
                               model=self.spec.model)
        self.calls_sent += 1
        messages: list[dict[str, str]] = []
        if system is not None:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = {"model": self.spec.model, "messages": messages,
                "response_format": {"type": "json_object"},
                "max_tokens": max_tokens, "temperature": TEMPERATURE}

        attempts = 0
        resp: httpx.Response | None = None
        settled = False  # a response arrived that we will not retry
        for attempt in range(1, self.spec.max_retries + 2):
            attempts = attempt
            self.attempts_sent += 1
            transport_failed = False
            async with self._concurrency_gate():
                try:
                    resp = await self._client().post(
                        self.spec.base_url, json=body,
                        headers={"Authorization": f"Bearer {key}"})
                except httpx.TransportError:
                    transport_failed = True
            if transport_failed or resp.status_code in _RETRYABLE_STATUS:
                # the attempt is spent whether or not the server saw it
                self._spend(call_id, purpose, "http_error", attempt, 0, 0)
                if attempt > self.spec.max_retries:
                    break
                await asyncio.sleep(_jitter(attempt - 1))
                continue
            settled = True
            break

        if not settled or resp is None:
            return FlashResult(status="http_error", call_id=call_id,
                               model=self.spec.model, attempts=attempts)
        if resp.status_code != 200:
            self._spend(call_id, purpose, "http_error", attempts, 0, 0)
            return FlashResult(status="http_error", call_id=call_id,
                               model=self.spec.model, attempts=attempts)

        doc = _response_doc(resp)
        usage = doc.get("usage") if isinstance(doc, dict) else None
        input_tokens, output_tokens = _token_pair(usage)
        model = self.spec.model
        if isinstance(doc, dict) and doc.get("model"):
            model = str(doc["model"])
        content = _content(doc)
        # parse tier 2: a thinking model that exhausted its cap can leave
        # the answer only inside reasoning_content (measured on 5.3-flash)
        parse_source = content or _reasoning_content(doc)
        data = parse_json_object(parse_source)
        if data is None:
            self._spend(call_id, purpose, "parse_error", attempts,
                        input_tokens, output_tokens)
            raw = parse_source or (
                f"(empty content and reasoning; finish_reason="
                f"{_finish_reason(doc)!r}; output_tokens={output_tokens})")
            return FlashResult(status="parse_error", raw=_redact(raw, key),
                               call_id=call_id, model=model,
                               input_tokens=input_tokens,
                               output_tokens=output_tokens, attempts=attempts)
        self._spend(call_id, purpose, "ok", attempts, input_tokens, output_tokens)
        return FlashResult(status="ok", data=data, call_id=call_id, model=model,
                           input_tokens=input_tokens,
                           output_tokens=output_tokens, attempts=attempts)
