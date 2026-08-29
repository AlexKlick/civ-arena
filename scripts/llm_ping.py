"""LLM lane wire spike: prove the MiniMax Messages endpoint + tool_use from Python.

No Python code on this host has spoken `tool_use` to MiniMax before (only the
Claude CLI has, via the same endpoint and key). This script gates the LLM lane:
stop on the first anomaly, exit non-zero, and record the result in
docs/llm-lane.md before building on top of it.

Three steps:
  1. GET {base with /anthropic/v1 -> /v1}/models with Bearer auth  -> expect 200
  2. minimal Messages call, no tools                              -> stop_reason + text + usage
  3. tool round-trip: one `echo` tool; second round echoes the
     assistant content VERBATIM (thinking blocks included) plus a
     tool_result                                              -> final text block

    uv run python scripts/llm_ping.py [--base-url ...] [--model ...]
        [--api-key-env ...] [--auth x-api-key|bearer]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import httpx

DEFAULT_BASE_URL = "https://api.minimax.io/anthropic/v1"
DEFAULT_MODEL = "MiniMax-M3"
DEFAULT_KEY_ENV = "ANTHROPIC_AUTH_TOKEN_MINIMAX2"
ANTHROPIC_VERSION = "2023-06-01"


def fail(step: str, detail: str) -> None:
    print(f"STEP {step}: FAIL — {detail}")
    sys.exit(1)


def auth_headers(key: str, style: str) -> dict[str, str]:
    if style == "bearer":
        return {"Authorization": f"Bearer {key}"}
    return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--api-key-env", default=DEFAULT_KEY_ENV)
    ap.add_argument("--auth", choices=["x-api-key", "bearer"], default="x-api-key")
    opts = ap.parse_args()

    key = os.environ.get(opts.api_key_env, "")
    if not key:
        fail("0", f"env var {opts.api_key_env} is not set")
    print(f"key env: {opts.api_key_env} (prefix {key[:5]}..., {len(key)} chars)")

    timeout = httpx.Timeout(120.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        # ---- step 1: models probe (Bearer per evidence_engine llm.py) --------
        models_url = opts.base_url.replace("/anthropic/v1", "/v1").rstrip("/") + "/models"
        resp = await http.get(models_url, headers={"Authorization": f"Bearer {key}"})
        if resp.status_code != 200:
            fail("1", f"GET {models_url} -> {resp.status_code}: {resp.text[:300]}")
        ids = [m.get("id") for m in resp.json().get("data", [])][:8]
        print(f"STEP 1: OK — GET /models -> 200; sample ids: {ids}")

        # ---- step 2: minimal Messages call, no tools --------------------------
        messages_url = opts.base_url.rstrip("/") + "/messages"
        body = {
            "model": opts.model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "Reply with exactly: PONG"}],
        }
        resp = await http.post(messages_url, json=body, headers=auth_headers(key, opts.auth))
        if resp.status_code != 200:
            fail("2", f"POST /messages -> {resp.status_code}: {resp.text[:300]}")
        doc = resp.json()
        blocks = doc.get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        types = [b.get("type") for b in blocks]
        usage = doc.get("usage", {})
        print(f"STEP 2: OK — stop_reason={doc.get('stop_reason')} "
              f"block_types={types} text={text.strip()[:80]!r} usage={usage}")
        if not text.strip():
            fail("2", f"no type=='text' block in content: {doc}")

        # ---- step 3: tool round-trip with verbatim assistant echo -------------
        tools = [{
            "name": "echo",
            "description": "Echo a value back. Call this exactly once with x set to 1.",
            "input_schema": {
                "type": "object",
                "properties": {"x": {"type": "integer", "description": "value to echo"}},
                "required": ["x"],
            },
        }]
        body = {
            "model": opts.model,
            "max_tokens": 2048,
            "system": "You are a wire-test agent. Use tools when asked.",
            "tools": tools,
            "messages": [{"role": "user", "content": "Call the echo tool with x=1."}],
        }
        resp = await http.post(messages_url, json=body, headers=auth_headers(key, opts.auth))
        if resp.status_code != 200:
            fail("3a", f"POST /messages with tools -> {resp.status_code}: {resp.text[:300]}")
        doc = resp.json()
        uses = [b for b in doc.get("content", []) if b.get("type") == "tool_use"]
        print(f"STEP 3a: OK — stop_reason={doc.get('stop_reason')} "
              f"tool_use={[{'name': u.get('name'), 'input': u.get('input')} for u in uses]}")
        if not uses:
            fail("3a", f"no tool_use block in content: {doc.get('content')}")
        tool_use_id = uses[0].get("id")

        body2 = {
            "model": opts.model,
            "max_tokens": 2048,
            "system": "You are a wire-test agent. Use tools when asked.",
            "tools": tools,
            "messages": [
                {"role": "user", "content": "Call the echo tool with x=1."},
                # verbatim echo — thinking blocks included, exactly what we will
                # do in the arena runtime's conversation loop
                {"role": "assistant", "content": doc["content"]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"},
                ]},
            ],
        }
        resp = await http.post(messages_url, json=body2, headers=auth_headers(key, opts.auth))
        if resp.status_code != 200:
            fail("3b", f"verbatim-echo round -> {resp.status_code}: {resp.text[:300]}")
        doc2 = resp.json()
        text2 = "".join(b.get("text", "") for b in doc2.get("content", [])
                        if b.get("type") == "text")
        print(f"STEP 3b: OK — stop_reason={doc2.get('stop_reason')} "
              f"text={text2.strip()[:80]!r}")
        if not text2.strip() and not any(b.get("type") == "tool_use"
                                         for b in doc2.get("content", [])):
            fail("3b", f"empty final content after tool_result: {doc2.get('content')}")

    print("LLM PING: ALL STEPS OK")


if __name__ == "__main__":
    asyncio.run(main())
