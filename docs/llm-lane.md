# LLM lane — wire facts and live validation record

The LLM lane (M10) puts a real model-driven agent behind the `AgentRuntime`
seam. This file records the proven wire behavior and the live validation
results. It is the LLM analogue of `docs/live-validation.md`.

## Proven wire facts (MiniMax, Anthropic-Messages-compatible path)

- Base URL: `https://api.minimax.io/anthropic/v1` (**with** `/v1`);
  messages endpoint `POST {base}/messages`.
- Auth: `x-api-key: <key>` + `anthropic-version: 2023-06-01` (proven from
  Python). `Authorization: Bearer` also accepted (the models probe uses it).
  Key comes from the env var named in config (`api_key_env`) — an inline
  `api_key` in YAML is a ConfigError by construction. The working key on this
  host is the `sk-cp` coding-plan key; `sk-api`-prefix keys return
  `1008 insufficient balance`.
- Never use `/v1/chat/completions`: returns 1008 on the coding-plan key.
- `max_tokens` is required; `temperature` is not sent (M3 rejects it).
- Response `content` is a block list; only `type == "text"` blocks carry
  answer text; thinking blocks must be skipped when joining text (M3 often
  emits none, but the parse must not assume that).
- `usage` arrives with Anthropic field names (`input_tokens`,
  `output_tokens`); the client also tolerates `prompt_tokens` /
  `completion_tokens` aliases.
- Assistant content can be echoed back VERBATIM (thinking blocks included)
  alongside `tool_result` blocks — proven in step 3b below.

## Wire spike result — 2026-08-28

`uv run python scripts/llm_ping.py` against MiniMax-M3 with the `sk-cp` key:

```
key env: ANTHROPIC_AUTH_TOKEN_MINIMAX2 (prefix sk-cp..., 125 chars)
STEP 1: OK — GET /models -> 200; sample ids: ['MiniMax-M3', 'MiniMax-M2.7', ...]
STEP 2: OK — stop_reason=end_turn block_types=['text'] text='PONG'
        usage={'input_tokens': 169, 'output_tokens': 3, 'service_tier': 'standard'}
STEP 3a: OK — stop_reason=tool_use tool_use=[{'name': 'echo', 'input': {'x': 1}}]
STEP 3b: OK — stop_reason=end_turn text='Done. The `echo` tool was called with
        `x=1` and returned `"ok"`.'
LLM PING: ALL STEPS OK
```

This was the gating unknown: no Python code on this host had spoken
`tool_use` to MiniMax before (only the Claude CLI had, via the same endpoint
and key). Both directions plus the verbatim echo are now proven. The
thinking-block-strip fallback in the design was never needed.

## Local glm-5.3 later

Same client, different config: `base_url: https://api.z.ai/api/anthropic/v1`,
`model_id: glm-5.3`, and the ZAI key env. No code change.

## Live match validation

(to be recorded after the first `llm-vs-turtler` run)
