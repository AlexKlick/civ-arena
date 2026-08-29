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

### 2026-08-28 — shakedown complete

`configs/llm-vs-turtler.yaml` (MiniMax-M3 as ROME/pid 0 vs the scripted
turtler as KOREA/pid 1), preceded by a 2-turn smoke on the same seed:

| Run | Result |
|---|---|
| `llm-smoke` (2 turns) | finished, 0 violations, 29 LLM tool calls, 0 errors; diary preview: *"Turn 2: Founded ANTIUM at (-5,0) on plains. u3 warrior escorting+fortifying there…"*; REPLAY OK (110 comparable events) |
| `llm-vs-turtler-001` (40 turns) | finished, **0 violations**, 838 LLM tool calls — 836 referee-visible results (160 rejections, fog-of-war noise) + 2 model-side malformed-argument errors (now reported separately as `model_errors`; they are not tool calls and emit no events, preserving telemetry/log recount parity and the model-free replay); tokens 410,477 in / 108,671 out; final score ROME 2 cities / 24 units / 8 techs vs KOREA 2 / 19 / 8 — the model played the turtler to a draw |
| replay of the 40-turn run | **REPLAY OK: 2,580 comparable events identical**, final hash `49914109…`, zero network — the model-free replay seam holds on a real LLM match |

The model wrote 38 diary entries across 40 turns and used them as genuine
bookkeeping — its final note tracks per-unit combat damage from the turn's
fights. The first shakedown verdict: the harness works end-to-end, and the
model neither stalled, nor leaked, nor tripped the watchdog.

