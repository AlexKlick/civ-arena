# Dataset evidence card — what an exported run may and may not be used for

Companion to `docs/cap03-decision-telemetry.md` (the exporter's design) and
`docs/cap-integration-20260907.md` (the CAP-R1 capture repairs). This document is
the **publication surface** required before any exported corpus is used for
training, distillation, or evaluation: coverage, gaps, actor uncertainty, and the
allowed-use list. It is written from the exporter source
(`src/civ_arena/research/export_dataset.py`), not from a run.

**Status: template + code-grounded contract. NO recording has been qualified yet.**
Section 6 is the qualification procedure; its result table is empty on purpose.
Nothing here asserts that a dataset exists or is fit for use.

## 1. Sample classes are strictly separated

The exporter emits exactly two classes and never blends them. Class purity is
enforced at export time: `_FORBIDDEN_KEYS`
(`request`, `response`, `prompt`, `system`, `messages`, `payload`, `context`,
`wire`, `actor_input`) may not appear in any sample, and a violation raises
rather than exporting. **No prompt, context, or provider payload text is ever
copied into a sample** — observations, actions and receipts are carried as
`seq` references into `events.jsonl`.

| | `controlled_decision` | `spectator_interval` |
|---|---|---|
| Schema | `cap03.controlled_decision/0` | `cap03.spectator_interval/0` |
| Source runs | driven agent runs (our own controller) | spectate/human runs |
| Grain | one sample per decision boundary | one sample per recorded round |
| Identity | `decision_id`, `directive_id`, `logical_request_ids` | `segment_id` = `seq:<start>` |
| Actor claim | the driven agent, by construction | **none** — `actor_attribution: owner_only` |
| Payload | refs only (`observation_refs`, `requested_action_refs`, `execution_receipt_refs`) | refs only (`snapshot_refs`, `ambient_diff_refs`) |

## 2. Decision grain (controlled_decision)

One sample **per boundary**, not per turn — a turn whose economy-refresh path
accepts a replacement directive exports two samples. Windows carry two starts:

- **observations** span the wide window `(previous boundary, next boundary)`,
  because production emits the boundary *after* refresh and decide, so a
  decision's inputs precede its own boundary;
- **requested actions and execution receipts** span the narrow window
  `(own boundary, next boundary)`, so an execution belongs to the decision that
  commanded it and is never inherited by a replacement that superseded it.

`procedural_tool_calls` counts the narrow-window actions only. The last boundary
of a `(turn, agent)` keeps the stable `segment_id`; superseded ones carry an
`@<decision_id>~<seq>` suffix so a reused id cannot mint duplicate ids.

Hotseat turns are keyed `(turn, agent_id)`: two agents in one turn export as two
samples, each with only its own observations, actions, receipts and boundary.

## 3. Gaps are flags, never repairs

Every known deficiency is carried on the sample and none of them is silently
filled. `quality_flags` values and their meaning:

**Run-level (controlled_decision)**

| Flag | Meaning |
|---|---|
| `pre_boundary_run` | run predates decision boundaries — `decision_id`/`directive_id` are null, not invented |
| `pre_ledger_run` | run predates the cost ledger — `request_costs` is null, not zero |
| `ledger_write_gaps` | at least one `ledger_write_failed` audit: recorded costs are known-incomplete |
| `run_aborted` | the run did not finish; `censoring: "run_aborted"` and outcome horizons are truncated |

**Sample-level (controlled_decision)**

| Flag | Meaning |
|---|---|
| `boundary_superseded_within_turn` | **turn-level context, not a claim about this sample**: the turn carried more than one boundary. It is set on *every* sample of that turn, including the surviving last boundary, so a consumer reading one sample knows the turn's decision grain was split. |
| `decision_id_reused_costs_ambiguous` | the id appears on more than one boundary **anywhere in the run** — across turns and agents, not only within one `(turn, agent)`. Every occurrence is flagged. |
| `costs_partial` | at least one *recorded* attempt's usage or latency was not captured — the totals are a **known subtotal**, not a total |
| `costs_attempts_missing` | fewer attempts were recorded than the boundary expected |

Cost joining: each ledger row is assigned to **exactly one** sample. A row is
eligible for the occurrences whose agent it may join (a row with no `agent_id`
may join any), and it goes to the **last eligible occurrence** for its decision
id. Agent-qualified rows for one reused id can therefore land on different
occurrences — the rule is last-eligible-per-row, not one universal final
occurrence for the id.

`usage_complete` asserts that the known subtotal **covers the whole decision**:
every recorded attempt has usage and latency, at least one attempt was recorded,
the count is not short of what the boundary expected, and no ledger write is
known to have been lost anywhere in the run (a lost write is unattributable, so
no sample may claim coverage). It is not a statement about the well-formedness
of the rows present. Independent of it: an empty known-usage list yields `None`,
never `0`, so "no known usage" stays distinguishable from "usage known to be
zero"; a recorded `0` is preserved as data.

**Sample-level (spectator_interval)**

| Flag | Meaning |
|---|---|
| `turn_label_mismatch` | the round lost its DEACT to the 64-entry ring wrap and was closed by the next turn's END; both labels are carried, never reconciled |
| `orphan_end_without_start` | an END with no open start |
| `gap_inferred_round` | the round's start was inferred from a capture gap, not observed |
| `interval_open_at_export` | the final round was interrupted; the end is `null`, never faked |

Three **run-wide** counters ride on every interval: `capture_gaps`,
`engine_jump_gaps`, `gap_inferred_rounds`. `truncated` is **interval-local and
nullable** — it reports whether *this* interval's own snapshot was truncated, and
is `null` when the interval has no snapshot (including the open final interval).
Successive intervals in one run legitimately carry different values, so reading
one interval never establishes run-wide truncation.

## 4. Actor uncertainty — the hard limit on spectate data

A `spectator_interval` is an **observation interval**: the net state difference
between two observed points with owner attribution only. It is not a command
sequence and carries no actor claim. `observed_actor_ids` is always empty;
`entity_owner_ids` lists owners derived from ambient entity ids.

The samples declare their own ineligibility in `ineligible_with_reasons`:

- `exact_action_imitation` — "interval diffs are net state changes, not commands"
- `reasoning_attribution` — "no intent channel recorded for this run"

and their eligibility in `eligible_tasks`: `state_trend`, `outcome_label`.

CAP-R1 is what makes the ambient windows trustworthy at all: before mod 0.4.0 a
single shared ambient slot corrupted cross-player windows, so **any interval
exported from a pre-0.4.0 recording carries owner attribution that may be
wrong**. Census snapshots were unaffected. Recordings are therefore qualified per
mod version, and the mod gate refuses anything below 0.4.0.

## 5. Allowed and prohibited uses

**Allowed**

- `controlled_decision`: procedural/tool-call behaviour cloning of *our own*
  controller, decision-cost analysis, outcome-horizon labelling — provided the
  sample's flags are read and `costs_partial` / `costs_attempts_missing` samples
  are excluded from any cost claim.
- `spectator_interval`: state-trend modelling and outcome labels.
- Both: evaluation and analysis of capture fidelity itself.

**Prohibited**

- Imitating a human spectate subject's actions, or attributing intent/reasoning
  to them — declared ineligible on every interval.
- Treating `request_costs` totals as complete when `usage_complete` is false, or
  as covering a decision whose sample carries `costs_partial`,
  `costs_attempts_missing` or `ledger_write_gaps`.
- Treating a `pre_ledger_run` or `pre_boundary_run` sample as if identity or
  costs merely happened to be zero.
- Mixing samples across `split_group` when constructing train/eval splits:
  sibling runs and restarts of one match share `split_group` (= `match_id`)
  precisely so they cannot land on both sides of a split;
  `game_instance_id` distinguishes executions within it.
- Any use of a recording captured by a mod below 0.4.0 for owner-attributed
  work (see §4).

## 6. Qualification procedure for a sealed recording — NOT YET RUN

A recording becomes usable only after this sequence, on a **disposable** native
scenario, with the exporter unchanged:

1. Capture the recording with mod 0.4.0 and `spectator_capture: true`.
2. Capture audit: the run's own capture-fidelity checks pass; no
   `spectator_world_failed`.
3. Viewer: the run loads in the match room with no new warnings.
4. Exporter: run `python -m civ_arena.research.export_dataset <run> -o …`
   **without modifying the exporter for the run**; record sample counts per
   class, ref-integrity failures, and the full flag histogram.
5. Publish the filled table below plus the manifest digests.

| Field | Value |
|---|---|
| Run id | *(pending)* |
| Mod version | *(pending)* |
| Samples by class | *(pending)* |
| Ref-integrity failures | *(pending)* |
| Flag histogram | *(pending)* |
| Allowed uses for THIS recording | *(pending — the §5 list minus whatever its flags remove)* |

The exporter refuses to write over its own sources (`events.jsonl`,
`summary.json`, `llm_costs.jsonl`, the run dir itself) through resolved paths,
symlinks and hardlinks, and binds each manifest digest to the exact bytes it
parsed — so a qualification run cannot damage or drift from the evidence it
describes.

## 7. Evidence separation

Repo proof (tests, gates) and live proof (a real recording through the four
steps above) are kept separate and never merged into one claim. Until §6 has a
filled row, statements in this document are **contract claims about the code**,
verified by the test suite, and nothing more.
