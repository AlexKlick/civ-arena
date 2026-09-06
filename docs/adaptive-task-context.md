# Adaptive task context candidate

This opt-in strategic-controller candidate separates briefing size from returned
JSON/tool-result limits. Mandatory relevant observations can expand beyond a
soft target. The complete finalized model input is counted before generation;
only that counter result plus the configured output reserve is compared with a
separately declared provider token window. Local byte estimates never authorize
or reject a request.

The running main checkout and its 100-round configuration were not changed.
The isolated base is `76f4dce0a344f749d6ddbea42b4f92dcba9d649e`; source and viewer
work on the other isolated catalog branch remain separate.

## Configuration and compatibility

The `llm` block may explicitly include:

```yaml
max_result_chars: 8000
max_tokens: 4096
max_requests_per_match: 2000
adaptive_context:
  provider_context_tokens: 1000000
  strategy_target_chars: null
  economy_target_chars: null
  contact_target_chars: null
```

Omitting `adaptive_context` retains legacy briefing behavior. With it enabled,
`max_result_chars` still bounds returned directive arguments and legacy serialized
tool results; it no longer caps the strategic request briefing. No numeric soft
target is supplied by default. An explicit positive per-task character target
guides optional terrain selection; it never removes required observations or
causes a soft-target overflow abort. Null includes all currently bounded relevant
mandatory and optional context. This candidate requires the strategic controller;
a non-strategic runtime refuses it explicitly rather than silently ignoring it.

The declared window is an operator configuration, not an inferred tokenizer
limit. The example uses the 1,000,000-token MiniMax-M3 context window documented
in [MiniMax's Messages-compatible API reference](https://platform.minimax.io/docs/api-reference/text-anthropic-api),
which the parent checked on 2026-09-06. A small successful count does not prove
that requests near that maximum will work.

## Task scope and mandatory context

An opening/cadence/research decision uses `strategy`; new contacts, damage,
owned-unit loss or an explicit tactical request use `contact`. A late request
after opening actions uses `economy`, even if its trigger mentions a contact.

All tasks retain every projected owned actor, projected visible contact, owned
city queue, current research/production choice source and current option rows.
All known terrain adjacent to owned actors remains mandatory. Contact review
also makes already observed contact neighborhoods mandatory. No unseen tiles
are queried or inferred. Optional radius-two terrain is ordered around the task
focus; economy prioritizes city neighborhoods. Unrelated distant map terrain
remains explicitly omitted. The earlier global 48-row optional limit does not
apply to adaptive rendering.

This first slice conservatively retains full projected actor rows even in
an economy briefing. Economy's request schema sets tactical overrides to an
empty array, and response validation rejects nonempty overrides using the same
bounded repair path. It cannot conceal unit details while retaining tactical
authority. Persistent production/research/scouting preferences keep their prior
contract. Strategy/contact tactical requests retain terrain around every owned
unit they may control. Future finer actor selection needs an equally explicit
restriction of model authority.

Lossless terrain/entity packing is selected only as a representation choice;
explicit null/absent fields and full planner state retain their prior semantics.
Required context can expand far beyond the configured target. Audit payloads
record task/focus, configured and allocated targets, mandatory/final sizes,
retained actor/tile counts, omitted terrain, and the reason for expansion.
No token counter is called for individual rows or routine game actions.

## Complete-request token admission

The shared request builder forms `model`, `system`, all `messages`, all `tools`
and optional `tool_choice`. The count POST sends those fields to
`/messages/count_tokens`; generation sends the same input plus `max_tokens`.
Canonical input and full generation-body SHA-256 identities, per-field UTF-8
sizes, output reserve and declared window are recorded. The receipt must bind
the same input identity and model, contain an exact positive integer token
count, and match the generation model/output configuration. The serialized-byte
estimate is labeled diagnostic-only, non-provider-exact and without hard
admission authority.

Each finalized decision or format-repair packet gets one logical count request.
Transport retries are bounded by the existing retry setting, with an overall
counter deadline using the existing request timeout; cancellation propagates.
An unsupported, timed-out, malformed or mismatched counter makes adaptive mode
explicitly unavailable. There is no local-estimate fallback or invented exact
count. Oversized provider-counted input plus output reserve stops before
sending generation; no critical observations are silently discarded to pass it.

`TokenCounter`/`TokenCount` provide an injected test seam. Host receipts are not
cryptographic provider signatures. Native receipts are labeled
`provider_count_tokens`; injected fixture receipts remain distinguishable and
do not constitute provider proof.

Every count and generation POST, including retries, consumes the existing
`max_requests_per_match` cap and existing durable post hook. Per-POST audit rows
record `request_kind=count_tokens` or `generation` and cumulative type counts.
Original post hooks are preserved/restored. Logical strategic request/repair
limits count generation decisions only. Quiet autopilot turns issue neither
count nor generation requests. Generation response usage is kept separate from
count-endpoint observations rather than adding both as model token usage.

## Grounding and verified repository evidence

Retained partial-run measurements through event sequence 4828 include 10
strategy responses with reported input tokens 2,552–4,222 and 38 contact responses
with 2,131–4,440. No economy-only response was present. These samples were already
subject to the old 8,000-character cap and cannot establish optimal task budgets.
No target defaults were invented from that censored evidence.

The final focused gate passed **233 tests, zero failed/skipped**, in 65.09 seconds;
Ruff passed. It includes 25 adaptive cases plus configuration, Messages wire,
legacy runtime, strategic control, context freshness, production and simulator/
live replay regressions. New checks cover retained turn-28 state and 64 dispersed
units across all tasks, exact body/model/output binding, soft expansion,
byte-estimate non-authority, empty economy tactics, count/generation caps,
recounted repairs, returned JSON bounds, malformed and stale receipts, timeout,
cancellation, and quiet-turn zero HTTP. Initial lint-only findings were corrected
and final checks recaptured. This is a focused gate, not the full release suite.

Evidence is retained under `runs/adaptive-context-evidence-20260906/`, including
`checks.json`, full pytest/Ruff logs, measured strategy audit rows and
`measurement-summary.json`. The measurement record binds the source event prefix
hash and last complete sequence; it is not a claim about the final live outcome.

## Separate provider evidence and follow-up

The parent independently made one real count POST, zero generation calls and no
game actions on a retained complete input: HTTP 200, 4,285 input tokens, 14,098
serialized input bytes, 7,058 user-context characters, and 0.44777 seconds elapsed.
Its input SHA-256 was
`bc57946c8120800c3f801039a1668a202545586307c4399f390b40830e6294ce`.
The response was `{"input_tokens":4285}`; the body included tool choice and omitted
`max_tokens`, reserving 4,096 output tokens separately. The copied parent record
is `parent-token-count-provider-probe.json`. That proves the endpoint's tested
payload shape, not live validation of this candidate or maximum-window capacity.
This agent made no provider, tuner, engine or desktop calls.

Independent exact-head review and the parent's integration/release gate remain
pending. The source candidate is opt-in; existing live configuration remains
unchanged. Task-target tuning, larger-request provider behavior, cross-model
context policies and operational reliability after integration are separate
follow-up evidence. No strategic-strength, forecast or optimal-context claim is
made by these repository tests.

## Admission-to-generation binding correction

Independent review of `5fff203c` found that replacing the client's model or
output-token configuration after the count response could change the subsequent
generation request. The counter had admitted one complete input and output
reserve, while `create()` rebuilt a different request from the then-current
client specification. This was a P2 repository defect, not provider evidence.

Adaptive requests now produce a frozen `GenerationAdmission` containing the
canonical serialized generation body, original typed count receipt, and declared
provider window. The complete body includes model, system, messages, tools,
optional tool choice, and reserved `max_tokens`. It is validated against the
receipt and window. `create_admitted()` is required for adaptive clients; a
counter alone does not establish a safe dispatch path. This capability remains
an injected in-process interface, not a persisted replay authorization.

The count endpoint also sends canonical JSON so nested tool/schema field order
matches the admitted generation input, rather than relying on alternate JSON
serializers producing equivalent tokenization. At each transport attempt, the
actual client validates the current model/output
configuration and sends the immutable admitted JSON text with the JSON content
type. It never rebuilds the generation payload from mutable request dictionaries
or current configuration. Configuration drift after counting or in the controller
request-audit callback refuses generation. A later synchronous POST hook cannot
change the already frozen payload; a retry likewise retains those exact bytes.
A missing response model is labeled with the captured admitted model. The
controller's generation-request audit includes admitted full-request and
input-payload hashes to join it directly to its count admission.

The correction preserves the legacy `create()` path, count endpoint format,
shared POST caps, quiet-turn zero-request behavior, directive output limits,
soft briefing policy, and model/tool format. Tests use the actual client with
MockTransport: post-count and request-audit changes of model/output reserve,
POST-hook drift, mutation of system/messages/tools/tool-choice/body during retry,
and invalid input/count/reserve/window admissions. No live provider calls were
made for this correction. Prior evidence remains preserved; corrected checks and
exact commit binding are retained in `runs/adaptive-binding-evidence-20260906/`.
