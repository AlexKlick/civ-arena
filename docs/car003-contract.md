# CAR-003 contract — decision-attempt lifecycle, sealed bundles, capture capability

**Status: FROZEN 2026-09-09.** Base: `origin/master` `45f36c6`. Packet:
CAR-QUALIFIED-EXPERIENCE-003 (NEXT-04B causal decision inputs + failure-inclusive
provider accounting; NEXT-03B qualification-gated atomic bundles; NEXT-08 per-stream
capability/coverage declaration). Every name below is byte-exact. A lane that needs a
name this document does not define adds it to its OWN records under its own prefix and
files a one-line amendment here; it never reuses a frozen name with a different meaning.
All `file:line` anchors are to the base commit `45f36c6` (the seam commit in §8 shifts
`strategic_controller.py` and `live_driver.py` by a few lines); `runtime.py` anchors
name their package explicitly because two modules share the basename.

Companions: `docs/cap03-decision-telemetry.md` (exporter design),
`docs/dataset-evidence-card.md` (allowed uses; §6 qualification procedure),
`docs/cap-integration-20260907.md` (CAP-R1 repairs).

## 0. Scope, premise corrections, ownership, merge order

### 0.1 Premise corrections (the code wins over the packet)

1. **`scripts/qualify_recording.py` and `scripts/capture_coverage.py` are NOT on
   master.** They exist only on `feat/capture-qualification-20260909` (merge commit
   `f90a6fb`, based on `45f36c6`; adds scripts/tests/configs/one doc, touches no
   `src/`). Every `qualify_recording.py:N` / `capture_coverage.py:N` anchor below refers
   to that lane at `f90a6fb`. **That lane merges before Lane E**; Lane E's qualifier
   changes are made on top of it.
2. **The exact model context is already recorded.** `strategy_request`
   (`strategic_controller.py:592-600`) is emitted BEFORE dispatch with `user_context`
   verbatim plus `context_sha256`/`context_chars`, pinned by
   `tests/test_strategic_controller.py:455-472`. What is missing is only the identity of
   the system prompt (module constants `SYSTEM`/`GROWTH_SYSTEM`, `:45-142`, sent as
   `SYSTEM + GROWTH_SYSTEM` at `:583`), the tool schema actually sent (`:549-558`,
   `:585`), and `decision_id` on the request audit (`_emit`, `:185-190`, stamped only
   audit/controller_version/match_id/agent_id/player_id/turn). The seam commit (§8)
   closes the `decision_id` part; Lane D closes the rest (§2.3).
3. **The viewer key sets live in `src/civ_arena/minimap.py:30-40`**, not
   `game/civ6/minimap.py`.
4. **`run_id` is recorded nowhere inside a run today.** `summary.json` carries
   `match_id` and `game_instance_id` (`live_driver.py:256-257`); the exporter's sample
   field `run_id` is `summary["match_id"]` (`export_dataset.py:545`, `:616`) — a
   misnomer. §1 fixes the vocabulary; §4 fixes the field.
5. **Ambiguous cost rows are not silently dropped; unassigned ones are.** A reused
   `decision_id` lands on the last eligible occurrence and is flagged
   (`export_dataset.py:328-362`, `:449-450`). A row whose id/agent matches no occurrence
   is dropped with no counter (`:355-362`). Both are replaced by the disposition
   accounting in §2.5.
6. **`provider_request` HEARTBEATs carry `turn=0`** (`live_driver.py:65-66`). They are a
   post counter, never a per-turn join key.
7. **The client has no run/match/agent/turn** (`client.py:104-126`); a cost row's only
   join keys were `decision_id` + `agent_id` (`wire_log.py:44-53`). The seam commit adds
   `turn`/`match_id`/`run_id` at the sink composer, which is the first place that knows
   them (`live_driver.py:70-115`).

### 0.2 Ownership (one owner per file; no lane edits another lane's file)

| Lane | Owns | Emits / consumes |
|---|---|---|
| D | `agents/llm/strategic_controller.py`, `agents/llm/client.py`, `agents/llm/wire_log.py`, their tests | emits §2 records |
| E | `research/export_dataset.py`, `tests/test_export_dataset.py`, **and** `scripts/qualify_recording.py` + `tests/test_qualify_recording.py` (on the qual lane) | consumes §2/§7, produces §3-§6 |
| C | `game/civ6/{world_capture,spectate_capture,validate_run,live_driver}.py`, `game/civ6/fake_tuner_server.py` (FakeMod mirror), `mods/PuppeteerMod/PuppeteerMod.lua`, `scripts/capture_coverage.py`, new `game/civ6/capture_schema.py`, `minimap.py` key-set imports only | emits §7, `summary.json`/`run_identity` `run_id` (§1) |

Merge order: `feat/capture-qualification-20260909` → seam (this branch) → D and C in
either order (independent files) → E last (consumes both; must also handle their
ABSENCE, because legacy runs and /0 exports never go away).

### 0.3 Non-goals (lanes must not build these)

- No controller resume/restart. `take_turn` stays fresh-only (`:233-237`). Boundaries
  and attempt records describe reproduction inputs; they are not checkpoints.
- No new graph/database/reader service. The only readers are the exporter, the
  qualifier and the dashboard, all file-based.
- No promise to observe every engine command. Spectator intervals stay OBSERVATION
  intervals (`actor_attribution: owner_only`); the mod ledger/digest exclusions in §7.4
  are DECLARED, not fixed.
- No new `EVENT_KINDS`; every new record is a HEARTBEAT audit or a file field.
- No change to the trace ring size (64), to `on_post`/spend parity, to the wire
  transcript format, or to `_refuse_rerun`.
- No re-export or rewrite of existing `/0` corpora; no `/0` emitter is retained.
- No legacy-path (`LLMAgentRuntime.take_turn` without controller,
  `agents/llm/runtime.py:137-144`) boundary. Its rows stay `unassigned` and the run
  stays `pre_boundary_run`.

## 1. Identity vocabulary

| Name | Type | Minted at | Carried on |
|---|---|---|---|
| `match_id` | str | config `MatchSpec.match_id` | events envelope (`live_driver.py:206`), `summary.json` (`:256`), controller audits (`strategic_controller.py:188`), cost row (seam) |
| `run_id` | str | the run directory basename, `runs/<run_id>/` — `opts.run_id or spec.match_id` (`live_driver.py:1867`, `:1884`) | cost row (seam: composer passes `run_dir.name`); **Lane C adds it to `summary.json` and the `run_identity` audit** (`:950`, `:1204`) as `run_id`; /1 samples (§4) |
| `game_instance_id` | str | `f"{match_id}-i{pid}"` (`live_driver.py:394`) | envelope (`:207`), summary (`:257`); **not** on cost rows |
| `agent_id` / `player_id` | str / int | `AgentSpec` | everywhere |
| `turn` | int ≥ 1 | `LLMAgentRuntime.begin_turn` (`agents/llm/runtime.py:97-99`); `_turn == 0` means "no turn begun" (`:129`) | envelope; controller audits (`strategic_controller.py:189`); cost row (seam, via `turn_of`) |
| `decision_id` | uuid4 hex | `strategic_controller.py:245` (turn start), `:728` (economy replacement), `agents/llm/runtime.py:144` (legacy path) | `client.decision_id` (`client.py:125`) → attempt record (`:280`) → cost row; `decision_boundary`; `strategy_request`/`strategy_response_shape`/`strategy_decision` (seam) |
| `directive_id` | uuid4 hex | `:295`, `:741` | boundary; quiet turns cite the standing one |
| `logical_request_id` | uuid4 hex | `client.py:243`, one per `_post_doc` (one per format attempt or token count) | attempt record (`:278`) → cost row |
| `request_set_key` | `f"{payload_hash}:{request_kind}"` (`client.py:245`) | attempt record → cost row |
| `attempt` (transport) | int ≥ 0 | `client.py:294` loop index: retry index WITHIN one logical request | attempt record → cost row |
| `attempt` (format) | int ≥ 1 | `strategic_controller.py:565`: one per `_post_doc` of kind `generation` | `strategy_request.attempt`, `strategy_response_shape.attempt` |

The two `attempt` fields keep their existing names (both pinned by tests); prose calls
them **transport attempt** (row) and **format attempt** (audit). One format attempt is
exactly one `logical_request_id` and 1..`max_retries+1` transport attempts.

`run_id` is a captured fact, never a reader-side guess: the exporter takes it from
`summary.json["run_id"]`, else from the cost rows when every row agrees, else `null`
with flag `run_id_unrecorded`. It never derives it from the directory it was pointed at.

## 2. Decision-attempt lifecycle (NEXT-04B) — Lane D emits, Lane E consumes

### 2.1 Records, in seq order, for ONE decision unit

All controller records are HEARTBEAT audits through `_emit` with the envelope
`{audit, controller_version: 1, match_id, agent_id, player_id, turn}` plus, for the
stamped kinds, `decision_id`. In production they pass through `strategy_audit_event`
(`agents/runtime.py:124-130`): every non-envelope field — including `decision_id` — is
nested inside `strategy_payload_json`, exactly as `decision_boundary`'s fields are
today. Readers flatten with `_boundary_view` (`export_dataset.py:119-134`).

| # | Record | Where | When | Identity |
|---|---|---|---|---|
| 0 | (mint) | `:245`/`:728` | `decision_id` minted and set on `client.decision_id` BEFORE any provider call | — |
| 1 | `strategy_request` | events.jsonl | per format attempt, BEFORE dispatch (`:592`) | `decision_id` (seam), `attempt` (format) |
| 2 | attempt record → cost row | `llm_costs.jsonl` (+ opt-in wire) | per COUNTED transport attempt, success or failure (`client.py:324/335/340/344/354`) | `decision_id`, `logical_request_id`, `request_set_key`, `attempt` (transport), `turn`, `match_id`, `run_id` (seam) |
| 3 | `strategy_response_shape` | events.jsonl | per format attempt AFTER a parsed reply (`:648`) | `decision_id` (seam), `attempt` (format) |
| 4 | `strategy_decision` | events.jsonl | once, when a directive is accepted (`:656`) | `decision_id` (seam) |
| 5 | `decision_boundary` | events.jsonl | **exactly once per minted `decision_id`, after the decision resolves — accepted OR NOT** | `decision_id`, `directive_id` (null unless accepted) |
| 6 | `strategy_failed` | events.jsonl | on any turn failure (`:403`, `:406`) | `decision_id` (Lane D adds the kind to `_DECISION_STAMPED_AUDITS`) |

Quiet (autopilot) turns: #0 and #5 only, `provider_requests: 0`, `source: "autopilot"`,
`outcome: "accepted"`, `directive_id` = the standing directive.

`count_tokens` requests (adaptive context, `:588`) produce #2 rows with
`request_kind: "count_tokens"` under the same `decision_id`; they are never format
attempts and never produce #1/#3.

### 2.2 The boundary becomes failure-inclusive

Today `decision_boundary` is emitted only after `_decide` returns (`:304-308`,
`:748-754`); a provider failure emits `strategy_failed` and no boundary, orphaning the
rows that already carry the id. Lane D:

- keeps the boundary AFTER the decision (the window semantics in §3 depend on it —
  inputs precede the boundary);
- adds `outcome` ∈ `{"accepted", "rejected", "unavailable", "aborted"}` and
  `format_attempts` (int; 0 on autopilot turns) to every boundary;
- emits the boundary with a non-accepted outcome, `directive_id: null`, and the honest
  `provider_requests` (`posts_sent - posts_before`) **before** `strategy_failed`
  whenever the failure occurs between mint and the boundary — on the turn-start path
  AND the economy-replacement path;
- emits **no second boundary** when the failure occurs after the boundary (scouting,
  capital guard, closure): `strategy_failed` alone carries the ambient `decision_id`.

Outcome causes: `rejected` — the provider replied but no directive was accepted
(`invalid_shape`/`invalid_args`/capital guard/`tactical_authority_disabled`, format
budget exhausted; `:666-678`); `unavailable` — no usable reply (`ModelUnavailable`,
`TimeoutError`, budget exhausted before or after a post: `:563`, `:568`, `:590`);
`aborted` — anything else raised between mint and boundary. Existing readers that only
know `/0` boundaries see an extra key and keep working.

### 2.3 `strategy_request` additions (Lane D)

| Field | Value |
|---|---|
| `system_prompt_sha256`, `system_prompt_chars` | over the exact `system` string sent (`:583`: `SYSTEM + (GROWTH_SYSTEM if growth_autopilot else '')`) |
| `tools_sha256`, `tools_chars` | over `_encode(kwargs['tools'])` — the schema list actually sent (`:585`), including the economy `maxItems: 0` edit (`:558`) |
| `tool_choice` | the dict as sent (`:586`) — small and bounded |
| `request_payload_sha256` | non-adaptive: `payload_hash({**input_payload(runtime.llm.model_id, **kwargs), 'max_tokens': runtime.llm.max_tokens})` — the body the client hashes (`client.py:185-187`, `:244`); adaptive: `== admitted_request_payload_sha256` (`:597`) |

`request_payload_sha256` equals the cost row's `payload_hash` for that format attempt's
transport attempts. Lane D pins this with a test that drives the REAL
`MiniMaxMessagesClient` over `httpx.MockTransport` and asserts equality; the
controller's copy of the body construction is otherwise unverified.

Verbatim `user_context` stays (already pinned). The system prompt and tool schema are
recorded by identity, not copied: they are module constants reproducible from
`controller_version` + the run's `launch_identity.commit`.

### 2.4 Attempt records and cost rows — what is and is not invented

- One row per counted POST, including transport errors (`status_code: null`),
  retryable HTTP, terminal HTTP and malformed-200 (`client.py:324-356`). **Attempt ok**
  := `status_code == 200 and error is null` (only `:340` fires that shape).
- `input_tokens`/`output_tokens`: absent or uncoercible usage stays `null`; an explicit
  `0` stays `0` (`_usage_value`, `:247-256`). No sink, exporter or qualifier may
  reconstruct a zero. Totals are KNOWN SUBTOTALS with `attempts_with_unknown_usage`
  beside them (existing R-04 rule, `export_dataset.py:468-479`).
- A failed or no-action attempt is recorded **without inventing a command**: no
  directive, no `requested_action_refs` entry, no `directive_id`, no zero cost is ever
  synthesized for it. The exporter carries such attempts only as rows and counts.
- Cost row fields (seam): `ts, agent_id, player_id, turn, match_id, run_id,
  request_kind, attempt, status_code, latency_ms, model, payload_hash, input_tokens,
  output_tokens, decision_id, logical_request_id, request_set_key`. Rows written before
  the seam lack the three identity keys; **absent ≡ null ≡ unknown**.

### 2.5 Disposition vocabulary and the conservation identity (Lane E)

Every parsed `llm_costs.jsonl` row receives exactly one **disposition**:

| Disposition | Rule |
|---|---|
| `attributed` | exactly one candidate occurrence; attempt ok; occurrence `outcome == "accepted"` |
| `no_action` | exactly one candidate; attempt ok; occurrence outcome ≠ accepted (rejected/unavailable/aborted), or legacy boundary with unknown outcome whose decision produced no `directive_id` |
| `failed` | exactly one candidate; attempt NOT ok (any non-200 or `error` set) — regardless of the decision's outcome (a 503 then 200 in an accepted decision = one `failed` + one `attributed`) |
| `ambiguous` | more than one candidate |
| `unassigned` | zero candidates: orphan `decision_id` (legacy failure path, legacy runtime path), `decision_id` null, or `match_id`/`run_id` present on the row and unequal to the run's |

Candidates = boundary occurrences (per §2.1 #5, per `(turn, agent)` segment, fanned out
for agentless boundaries exactly as today, `:340-351`) whose `decision_id` equals the
row's, whose agent the row may join (`:358-360`), and — **when the row carries `turn`**
— whose turn equals the row's. With the seam fields present every well-formed row has
at most one candidate; `ambiguous` survives only for legacy rows.

A boundary that predates `outcome` (every boundary on master today) was emitted only
on the accepted path, so for disposition purposes an absent `outcome` with a non-null
`directive_id` counts as `accepted`, and the sample carries `outcome_unrecorded`; an
absent `outcome` with a null `directive_id` is malformed and its rows are
`unassigned` — never guessed.

Conservation identity (a test MUST assert it on every export):

```
manifest.attempt_dispositions = {attributed, no_action, failed, ambiguous, unassigned, total}
total == rows parsed == attributed + no_action + failed + ambiguous + unassigned
Σ_samples request_costs.attempts_by_disposition[k] == manifest.attempt_dispositions[k]   for k in {attributed, no_action, failed}
sample.request_costs.attempts == Σ_k sample.request_costs.attempts_by_disposition[k]
```

`ambiguous` and `unassigned` rows are carried by **no sample** (manifest only); every
occurrence that was a candidate of an ambiguous row carries the existing flag
`decision_id_reused_costs_ambiguous`; a run with `unassigned > 0` carries the run flag
`costs_unassigned_rows` on every controlled sample. `expected_attempts` /
`costs_attempts_missing` / `usage_complete` keep their /0 semantics (`:451-515`).

## 3. Reference fields and windows (Lane E)

Windows use the three cursors that already exist per boundary occurrence
(`export_dataset.py:390-403`): `w_in` = previous boundary seq of the same
`(turn, agent)` (null for the first), `w_exec` = own boundary seq, `w_end` = next
boundary seq (null for the last). The single predicate `_window(e, _s, _e)`
(`:410-421`) is **exclusive on both bounds**; a null bound is unbounded within the
`(turn, agent)` segment; a non-int seq falls out of every bounded window.

| Field | Events | Window | Status |
|---|---|---|---|
| `decision_input_refs` | `TOOL_RESULT`, tool ∈ `_OBSERVE_TOOLS` | `(w_in, w_exec)` | new in /1 — the observations the decision could have seen |
| `execution_observation_refs` | `TOOL_RESULT`, tool ∈ `_OBSERVE_TOOLS` | `(w_exec, w_end)` | new in /1 — observed while executing, never a decision input |
| `requested_action_refs` | `TOOL_CALL`, tool ∈ `_ACTION_TOOLS` | `(w_exec, w_end)` | retained (`:427-430`) |
| `execution_receipt_refs` | `TOOL_RESULT`, `status == "accepted"` with `mutations`/`receipts` | `(w_exec, w_end)` | retained (`:431-435`) |
| `decision_boundary_ref` | the occurrence's own boundary | — | new in /1; `null` for `pre_boundary_run` |
| `outcome_refs` | `[decision_boundary_ref, MATCH_END ref if present, source_manifest_ref]` | — | new in /1 — the evidence for `outcome_horizon`/`censoring`/`decision_outcome` |
| `observation_refs` | — | — | **/0 only**; in /1 it is split into the first two rows, never aliased |

Boundary-less segments (`pre_boundary_run`): `w_in = w_exec = w_end = null`, so
`decision_input_refs` = the whole segment's observations and
`execution_observation_refs` = `[]` (nothing can be shown to postdate a decision).

### 3.1 Ref shapes

```
{"artifact": "events.jsonl",    "seq": <int>}                        # unchanged (_ref, :137-138)
{"artifact": "summary.json",    "sha256": "<hex of the bytes parsed>"}
{"artifact": "llm_costs.jsonl", "line": <int ≥ 1>}                   # 1-based PHYSICAL line in raw.decode().splitlines(); blank lines count
{"artifact": "qualification.json", "sha256": "<hex>"}                # bundle member (§5), not a run artifact
```

`request_costs.attempt_refs` lists one `llm_costs.jsonl` ref per row the sample carries
(attributed/no_action/failed only). `source_manifest_ref` gains `sha256`.

### 3.2 `qualify_recording.check_sample_refs` (Lane E, on the qual lane)

`_collect_refs` (`qualify_recording.py:184-196`) collects only dicts with both `seq`
and `artifact`; `check_sample_refs` (`:234-258`) fails any `artifact != "events.jsonl"`.
Required change, without weakening the events check:

- collect every dict carrying `artifact`;
- dispatch on `artifact`: `events.jsonl` → `seq` must be an int present in the consumed
  bytes (unchanged rule); `summary.json` → `sha256` must equal the sha256 of the run's
  `summary.json` bytes; `llm_costs.jsonl` → `line` must be an int in
  `[1, physical line count]` and that line non-blank; `qualification.json` → `sha256`
  must equal the verdict bytes the seal step wrote; any other artifact → failure;
- a `/0` sample's bare `{"artifact": "summary.json"}` (no `sha256`) passes only when
  `schema_version` ends in `/0`.

## 4. Schema versions (Lane E)

`SCHEMA_CONTROLLED = "cap03.controlled_decision/1"`,
`SCHEMA_SPECTATOR = "cap03.spectator_interval/1"` (`export_dataset.py:38-39`). The
exporter emits /1 only; a legacy run exports as /1 with nulls + flags, never as /0.

Every in-repo consumer that parses samples (qualifier, dashboard, tests) dispatches on
`schema_version` and accepts exactly the four strings
`cap03.controlled_decision/{0,1}`, `cap03.spectator_interval/{0,1}`, refusing others.

**What a /0 reader must still be able to do:** read every /0 export already on disk
(e.g. `runs/cap03-evidence-20260907/`) with the field names, types and semantics of
`docs/dataset-evidence-card.md` unchanged — /0 samples are never rewritten. A /1-aware
reader that meets a /0 sample must treat: `observation_refs` as the union of both /1
windows (it cannot split them without `events.jsonl`); `run_id` as `match_id`;
absent `decision_outcome`, `attempts_by_disposition`, `promotable` as UNKNOWN (never
false/zero); `eligible_tasks` as an UNQUALIFIED claim.

### 4.1 `cap03.controlled_decision/1` field list

Retained, same semantics: `sample_class, schema_version, game_instance_id, segment_id,
agent_id, player_id, split_group, decision_id, directive_id, provider_requests,
decisions_in_turn, decision_source, logical_request_ids, requested_action_refs,
execution_receipt_refs, procedural_tool_calls, outcome_horizon, censoring,
visibility_class, quality_flags`.
Changed: `run_id` (§1 captured fact or null + `run_id_unrecorded`); `source_manifest_ref`
(+`sha256`); `request_costs` (+`attempts_by_disposition` {attributed, no_action,
failed}, +`attempt_refs`).
New: `match_id` (= `summary.match_id`, always), `decision_input_refs`,
`execution_observation_refs`, `decision_boundary_ref`, `outcome_refs`,
`decision_outcome` (boundary `outcome`; `null` + flag `outcome_unrecorded` for legacy
boundaries), `eligible_tasks`, `ineligible_with_reasons`, `promotable`,
`qualification_ref` (§6).
Removed: `observation_refs`.

### 4.2 `cap03.spectator_interval/1` field list

Retained: everything in /0 including `source_cursor_start`/`source_cursor_end` (raw
ring-entry strings, §7.5 — deprecated, never removed) and the three run-wide gap
counters. Changed: `run_id` (§1), `eligible_tasks`/`ineligible_with_reasons` are
DERIVED (§6.3), not literals. New: `match_id`, `trace_cursor_start`/`trace_cursor_end`
(§7.5 objects or null), `promotable`, `qualification_ref`.

## 5. Sealed bundle (NEXT-03B, Lane E)

### 5.1 Layout

```
<bundle_dir>/
  samples.jsonl        one sample per line, sorted keys, compact separators (deterministic, as today)
  qualification.json   the verdict document, byte-copied from the qualifier
  manifest.json        §5.2
  SEALED               completion marker: "<sha256 of manifest.json bytes>\n"
```

The flat diagnostic form `write(samples, manifest, out_path)` (`:214-245`, samples +
`<out>.manifest.json` sibling) is retained for unqualified exports and switches to
`canonical.atomic_write_text` (`canonical.py:65-89`, mkstemp + `os.replace`, already
used by `scripts/fit_weights.py:350`). A diagnostic export has no `SEALED` and no
`qualification.json`, and its manifest says so (`publication: "diagnostic"`).

### 5.2 Manifest additions

| Field | Value |
|---|---|
| `samples_sha256`, `samples_count` | sha256 and line count of `samples.jsonl` exactly as written — binds the manifest to the OUTPUT (today it hashes inputs only, `:189-210`; `qualify_recording.py:294` recomputes after the fact) |
| `qualification_sha256` | sha256 of the copied `qualification.json`; `null` for diagnostic |
| `publication` | `"qualified"` or `"diagnostic"` |
| `qualification` | `{verdict, components}` copied from the verdict (§6.1); `null` for diagnostic |
| `attempt_dispositions` | §2.5 |
| `exporter` | `{"module": "civ_arena.research.export_dataset", "contract": "CAR-003", "schema_versions": [...], "commit": <git HEAD or null>, "dirty": <bool or null>}` — the same two git probes `implementation_identity` uses (`live_driver.py:730-736`); `null` when git is unavailable, never a fabricated commit |
| existing `source.*` digests, `run_dir`, `exported_at`, `sample_counts`, `schema_versions` | unchanged |

### 5.3 Ordering rule and what the seal guarantees

`write_bundle(samples, manifest, qualification_bytes, bundle_dir)`:

1. refuse if `bundle_dir` exists and contains `SEALED` (no in-place re-seal; publish to
   a fresh directory) and apply the existing source-clobber refusal (`:225-238`) to every
   member;
2. `samples.jsonl` via `atomic_write_text(..., fsync=True)`;
3. `qualification.json` via `atomic_write_text(..., fsync=True)`;
4. `manifest.json` via `atomic_write_text(..., fsync=True)` — computed from the bytes
   written in 2-3, not from in-memory objects;
5. `SEALED` via `atomic_write_text(..., fsync=True)` — LAST; best-effort fsync of the
   directory afterwards.

**Four `os.replace` calls are four atomic operations, not one transaction.** The seal
guarantees only this: *if* `SEALED` exists and its content equals the sha256 of
`manifest.json`, *and* `manifest.samples_sha256`/`qualification_sha256` equal the
sha256 of the sibling bytes, *then* the reader holds exactly the files the sealer
hashed. It does not guarantee that a partially published directory cannot exist — it
guarantees that one is **detectable**: any missing member, missing marker or digest
mismatch is "interrupted publication" and the reader refuses. `qualify_recording`'s
final stage and any consumer verify all three digests before reading a sample.

## 6. Qualification-result interface (Lane E)

### 6.1 What `qualification.json` exposes

Schema 1 (`qualify_recording.py:311-318`): `verdict PASS|FAIL`, `failures[]`, and the
stage sections `audit`/`viewer`/`export`/`coverage`. Schema **2** is additive:

```
"components": {
  "<name>": {"status": "pass" | "fail" | "unchecked", "reasons": [str], "evidence": {…}}
}
```

Component names (frozen): `capture_audit` (validate_run), `identity` (commit/dirty/
fake/mod_sha256), `spectator_world` (row contract, `spectator_world_failed` count),
`census` (snapshot presence and `atomic`/`consistent` census), `ambient_windows`
(mod version ≥ 0.4.0 AND `supports_ambient_windows`; evidence card §4), `cost_ledger`
(no cost-affecting `ledger_write_failed`; §2.5 identity holds; `unassigned == 0` for
rows that carry the seam fields), `decision_boundaries` (every minted id has exactly
one boundary; `outcome` present), `viewer`, `export_refs` (ref integrity + digest
binding), `capture_capability` (§7 audit present and consistent with the streams
observed). A component the qualifier could not evaluate is `unchecked`, never `pass`.
`verdict == PASS` iff no component is `fail`.

### 6.2 Sequencing: export → qualify → seal

`stage_export` (`:271-305`) runs the exporter before any verdict exists, so:

1. `stage_export` runs `export(run_dir)` → a **diagnostic** export (as today);
2. `stage_summarize` produces the verdict with `components`;
3. new `stage_seal`: `export(run_dir, qualification=verdict)` → samples with eligibility
   filled from the verdict, then `write_bundle(...)` into `<run>.qual/bundle/`, then
   `check_sample_refs` again over the sealed samples (they differ from step 1 only in
   the eligibility fields and `qualification_ref`).

`export()` gains the keyword `qualification: dict | None = None`; `None` means
diagnostic.

### 6.3 Eligibility is per component/field AND per task

Today `eligible_tasks`/`ineligible_with_reasons` are duplicated hardcoded literals
(`export_dataset.py:644-650`, `:681-687`), asserted unconditionally. They MUST come from
one module-level table in `export_dataset.py`:

```
TASK_REQUIREMENTS = {
  "controlled_decision": {
    "procedural_behaviour_cloning": {"components": ["capture_audit", "decision_boundaries", "export_refs"],
                                     "disqualifying_flags": ["pre_boundary_run"]},
    "decision_cost_analysis":       {"components": ["cost_ledger"],
                                     "disqualifying_flags": ["pre_ledger_run", "costs_partial", "costs_attempts_missing",
                                                             "decision_id_reused_costs_ambiguous", "costs_unassigned_rows"]},
    "outcome_label":                {"components": ["capture_audit", "identity"], "disqualifying_flags": []},
  },
  "spectator_interval": {
    "state_trend":              {"components": ["census"], "disqualifying_flags": ["orphan_end_without_start"]},
    "outcome_label":            {"components": ["capture_audit", "identity"], "disqualifying_flags": []},
    "owner_attributed_ambient": {"components": ["ambient_windows"], "disqualifying_flags": []},
    "exact_action_imitation":   {"never": "interval diffs are net state changes, not commands"},
    "reasoning_attribution":    {"never": "no intent channel recorded for this run"},
  },
}
```

A task is eligible iff the export is qualified, every required component is `pass`,
and the sample carries none of its disqualifying flags. `ineligible_with_reasons`
carries the first failing reason per task (`unqualified_export`,
`component:<name>:<status>`, `flag:<name>`, or the `never` text). `promotable` :=
`publication == "qualified"` and at least one task eligible. In a diagnostic export
`eligible_tasks == []` for every sample — the /0 behaviour of asserting
`state_trend`/`outcome_label` on every interval is retired. This is what lets a readable
census survive an unusable ambient stream: `state_trend` stays eligible while
`owner_attributed_ambient` fails on `ambient_windows`. The table is the only place task
names, component names and flag names meet; tests pin that both sample classes read it.

## 7. Capability/coverage manifest (NEXT-08, Lane C)

### 7.1 Producer-owned single source of truth

New module `src/civ_arena/game/civ6/capture_schema.py` (constants and pure functions
only; no adapter imports) defines:

- `WORLD_ROSTER_KEYS`, `WORLD_PLAYER_KEYS`, `WORLD_CITY_KEYS`, `WORLD_TILE_KEYS`
  (frozensets) — replacing the literals in `world_capture.package`
  (`world_capture.py:526-529`, `:541-544`), `minimap.py:30-40` (`WORLD_*_ROW`) and
  `capture_coverage.py:56-65` (`*_ADMITTED`). Those three sites IMPORT them; a test
  pins identity (`minimap.WORLD_PLAYER_ROW is capture_schema.WORLD_PLAYER_KEYS`, etc.)
  and that `capture_coverage` derives its tuples from them.
- `BOUNDS`: `MAX_ROSTER=64`, `MAX_WORLD_CITIES=256`, `MAX_OWNED_TILES=4096`,
  `MAX_DISAGREE_COORDS=64` (`world_capture.py:38-42`), `MAX_SNAPSHOT_BYTES=262144`
  (`spectate_capture.py:36`), `MAX_WORLD_BYTES=262144` (`minimap.py:21`),
  `TRACE_RING_CAPACITY=64` (`PuppeteerMod.lua:82`). The existing constants become
  re-exports; the viewer's `WORLD_*_MAX` (`minimap.py:22-28`) import them.
- `STREAMS`: the per-stream declaration (§7.2).
- `EXCLUSIONS` (§7.4).
- `capability_manifest(mod_doc, spec) -> dict` (§7.3).

### 7.2 Per-stream, per-field declaration

`STREAMS[name]` = `{carrier, context, cadence, bounds, fields, exclusions}` where
`carrier` names the events.jsonl `kind`/`audit` (e.g. `HEARTBEAT/spectator_world`,
`SPECTATOR_SNAPSHOT`, `HUMAN_TURN_END.human_ambient`), `context` ∈
`{gamecore, ingame, driver, mod}` (matching `world["contexts"]`, `world_capture.py:585-588`),
and `fields[name]` = `{type, nullable: bool, bounds: {...} | null, context,
observed_vs_derived: "observed" | "derived", status: "emitted" | "admitted_unread"}`
(`level` on roster rows is `admitted_unread`; `palette_confirmed` is `emitted`, value
pinned false, `world_capture.py:603`). Stream names (frozen): `hook_trace`,
`census_snapshot`, `ambient_windows`, `spectator_world`, `spectate_world`,
`mod_digest`, `command_ledger`, `fog_audit`.

### 7.3 The capability audit

Once per run, immediately after `mod_capabilities` (`live_driver.py:988`) on the
hotseat path and after the spectate capability check (`:1198-1199`) on the spectate
path: `audit("capture_capability", schema=1, mod={"version", "supports": {...handshake
bools...}}, source_clock={...§7.5...}, streams={name: {enabled: bool, **STREAMS[name]}},
exclusions=EXCLUSIONS)`. `enabled` comes from the spec (`spectator_capture`,
`snapshot_scope`, `observed_players`) and the handshake — a declared stream that the
run did not enable is present with `enabled: false`, never omitted. The qualifier's
`capture_capability` component checks that every enabled stream was actually observed
at least once and that every observed key set ⊆ the declared field set.
`capture_coverage.py` reads the declared fields from `capture_schema`, not from its own
tuples.

### 7.4 Exclusions become machine-readable

`docs/productive-native-actions.md:85-89` states that districts, production queues and
tile ownership are outside the mod ledger AND digest. `EXCLUSIONS` carries exactly that:

```
EXCLUSIONS = {
  "mod_digest":     ["districts", "production_queues", "tile_ownership"],
  "command_ledger": ["districts", "production_queues", "tile_ownership"],
  "spectate_world": ["palette", "extended_cities"],          # read transport only (spectate_capture.py:199-206)
  "hook_trace":     ["entries_evicted_by_ring_wrap"],        # ring capacity 64; §7.5 makes the loss countable
}
```

The doc keeps its prose; a test pins that the prose lists and the constant agree.

### 7.5 Source clock, minted in the mod

Only the mod knows an entry was evicted. `PuppeteerMod.lua:78-83` gains three globals
that survive re-injection the way `PUPPETEER_TRACE` does (`:78`):

```lua
PUPPETEER_TRACE_EPOCH   = (PUPPETEER_TRACE_EPOCH or 0) + 1      -- +1 per (re)injection into this VM; 1 on a fresh VM
PUPPETEER_TRACE_NEXT    = PUPPETEER_TRACE_NEXT or 1              -- ordinal of the NEXT entry; monotonic, never reset within a VM
PUPPETEER_TRACE_EVICTED = PUPPETEER_TRACE_EVICTED or 0           -- +1 per table.remove(PUPPETEER_TRACE, 1)
```

Entries keep their exact `"<turn>|<EVENT>|<pid>"` form (`TurnWatch.parse` requires
three fields, `spectate_capture.py:110-119`). `Puppeteer.Trace()` (`:911-913`) prepends
ONE header line:

```
TRACE|<epoch>|<first_ordinal>|<evicted_total>|<count>
```

with `first_ordinal = PUPPETEER_TRACE_NEXT - #PUPPETEER_TRACE`; entry *i* (0-based) has
ordinal `first_ordinal + i`. Mod version becomes `0.4.1` with
`Puppeteer.supports_trace_clock = true`; the handshake gate (`firetuner.py:459-463`)
and `FakeMod` move with it. `TurnWatch` consumes the header when present (exact loss =
`first_ordinal - (last_seen_ordinal + 1)` within one epoch; an epoch change = a new
`generation`) and falls back to the suffix-overlap heuristic (`:77-108`) when absent,
reporting `clock: "exact" | "heuristic"` and `lost_entries: int | null` on every
`trace_gap` audit (`live_driver.py:1323-1326`). `source_clock` in §7.3 records
`{epoch, ordinal_at_attach, evicted_at_attach, ring_capacity: 64, clock: ...}`.

### 7.6 Disambiguating `source_cursor`

Today the name means two things: a raw ring-entry string on
`HUMAN_TURN_START`/`SPECTATOR_SNAPSHOT`/`HUMAN_TURN_END` (`live_driver.py:1298`,
`:1309`, `:1414`; consumed at `export_dataset.py:628-630`, `:666-667`) and an
events.jsonl seq range in coverage rows (`capture_coverage.py:224-226`, `:345`).
Frozen names:

| Name | Meaning | Shape | Where |
|---|---|---|---|
| `source_cursor` | **frozen to its current meaning**: the raw ring entry string; deprecated, retained for /0 readers; never used for anything new | `str` | the three events above; `/1` `source_cursor_start/_end` |
| `trace_cursor` | the mod trace-clock position of that entry | `{"epoch": int, "ordinal": int, "entry": str}` — present only when the clock is exact; `null` otherwise | ADDED to the same three events; `/1` `trace_cursor_start/_end`; `window_start_cursor`/`window_end_cursor` (`:1416-1417`) gain `window_start_trace`/`window_end_trace` siblings |
| `event_seq_span` | an events.jsonl seq range | `{"seq_min", "seq_max", "events"}` | RENAMES the coverage row's `source_cursor` (the coverage lane is unmerged; rename its doc and tests with it) |

`spectate_capture._allowed_world_reads()` (`:326-336`) admits world Lua by exact string
equality; any read string Lane C adds or changes (including the trace header producer if
it is ever read through the spectate transport) is added there or the spectate phase
refuses it.

## 8. The seam commit (already applied on this branch)

Two mechanical identity changes so the three lanes never contend on one file:

**(a) `wire_log.CostLedger.note(agent_id, player_id, record, *, turn=None, match_id=None,
run_id=None)`** — every cost row now carries `turn` (int ≥ 1 or null), `match_id`
(str or null), `run_id` (str or null). Values are recorded only when supplied and
well-typed; nothing is defaulted. `_wire_client_sinks(client, agent, audit, run_dir, *,
match_id=None, run_id=None, turn_of=None)` threads them; `turn_of` is evaluated at
attempt time and a raising `turn_of` yields `null` (the row still lands — identity is
best-effort, cost is not). Both production call sites pass `match_id=spec.match_id`,
`run_id=run_dir.name` and the runtime's `_turn` (`live_driver.py` single-seat and
hotseat dispatch; the hotseat lambda binds its loop variable by default argument).

**(b) `StrategicController._emit`** stamps `decision_id` onto the kinds in
`_DECISION_STAMPED_AUDITS = {strategy_request, strategy_response_shape,
strategy_decision}` from `self._decision_id`, set at the turn-start mint and at the
economy-replacement mint, so both paths' request/shape/decision audits name the
decision the rows joined. An explicit `decision_id` kwarg (as on `decision_boundary`)
always wins. Lane D extends the set (`strategy_failed`) rather than editing `_emit`.

## 9. Test obligations (minimum, per lane)

- **D**: boundary emitted exactly once per minted id on accepted, rejected, unavailable
  and aborted paths, on both the turn-start and economy-replacement paths, always before
  `strategy_failed`; `request_payload_sha256 == row.payload_hash` through the real client;
  `strategy_failed` carries `decision_id`; retry chains keep one `logical_request_id`.
- **E**: the §2.5 identity on a fixture with all five dispositions; windows (§3) on a
  two-boundary turn including the empty `execution_observation_refs` case; `/0` inputs
  still readable and `/1` never emits `observation_refs`; `write_bundle` interrupted
  after each step is detected as unsealed; `TASK_REQUIREMENTS` drives both classes and a
  diagnostic export has no eligible task; the census-survives-ambient case.
- **C**: key-set identity across the three sites; every declared stream observed ⊆
  declared fields on the fake hotseat + spectate rehearsals; exact loss count from the
  trace header across a forced wrap and across a re-injection (epoch change); heuristic
  fallback unchanged against a 0.4.0-shaped trace; `EXCLUSIONS` agrees with the prose.
