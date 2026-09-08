# S3 economic forecast lane (graph-optimizer O1)

**Branch:** `feat/s3-economic-forecast-20260907` (worktree `~/civ-arena-s3-forecast-20260907`),
based on `feat/cap-integration-20260907` @ `5711fc1` (CAP-01/02/03 integrated; CAP-R1 was
pending at branch time and is expected to land on top — this lane is additive by design).

**Program:** CAR-GRAPH-OPTIMIZER-001 lane O1 (see
`~/pop-deck-uploads/2026-09/civ-arena-graph-optimizer-handoff-2026-09-07-6a221d.md`),
implementing the first slice of the S-milestone ladder in
`docs/strategic-autopilot.md` (S3 bounded economic forecast + the S4
contingent-alternative slice: one investment-vs-progress comparison plus a
threat interrupt). Deterministic economics only — no learned component
anywhere in this lane (the M19/M20 harm/harm/null record and the M16b
structural-null condition in `docs/civgraph-program.md` §7 are the standing
reasons why).

## What landed

| Commit | Content |
|---|---|
| `921a8cf` | `agents/economic_forecast.py` — pure forecast contract |
| `922f3f6` | `agents/build_option.py` — closed-loop option monitor |
| `1eac3f5` | config/runtime/controller wiring (advisory, default-off) |
| `6c67e7b` | engine-scenario + mutation-hardening tests |
| `2f150d9` | Codex review round 1 — all five findings fixed |
| `7b23b72` | Codex review round 2 — all four findings fixed |

### Codex R1 (all five findings verified real, all fixed)

1. **Rate changes never invalidated** → `observe(catalogs=...)` rechecks the
   pending item's turns estimate when a fresh catalog row exists and censors
   with `rate_estimate_changed`; the assumption text now states exactly when
   the recheck can fire (queued cities are not catalog-refreshed by default).
2. **Outcomes reached the model a turn late** → pending resolution moved from
   `_economy` to `take_turn` BEFORE `_decide`; the model deciding on turn N
   sees turn-N completions and censor state (wiring regression pins it).
3. **Advisory comparisons blind to threats** → advisory computes the
   confirmed-threat observable and passes it into every plan comparison.
4. **Ordering did not mirror the policy** → unit tier uses the policy's
   `BUILDER/DEFENDERS/SCOUT/SETTLER` fallback order; the barbarian override
   fires only under the policy's own `defenders < defense_goal` shortfall
   (passed from the policy result); unknown shortfall keeps growth order with
   the preempt condition carried explicitly instead of silently flipping the
   recommendation.
5. **Early completions inflated interval coverage** → verdicts check both
   bounds; completing before the lower bound is `'early'` (a miss), not
   in-window.

### Codex R2 (four findings on the R1 fixes, all verified real, all fixed in `7b23b72`)

1. **`GetTurnsLeft` is a countdown, not a static estimate** — comparing raw
   turns censored every healthy build. The recheck now compares implied
   completion dates (`turn + turns` vs the recorded completion); a healthy
   countdown never censors, real slippage does (`rate_estimate_changed`
   carries recorded/implied completion turns).
2. **The recheck was unreachable through the controller** (a fresh curator
   never catalogs queued cities). `take_turn` now reads each pending city's
   catalog row via one guarded `curator.read` before observation.
3. **Defense shortfall recomputed from eligible candidates under-counted
   satisfied defenders** — now taken from the policy result's own
   `defenders_owned_queued_reserved` + `defense_goal` (`defense_context`).
4. **Threat branch with no eligible defender returned alphabetical order**
   instead of the policy's fall-through — now mirrors `choose_production`
   exactly: no eligible defender ⇒ normal cascade.

### Forecast contract (`economic_forecast.py`)

Implements the S3/S4 data contract verbatim: **observed facts**, **assumptions**
(id + source + invalidation), **forecast ranges** (metric + method), and
**selection weights** are distinct field classes; a record never mixes them.

- **Completion basis = the engine's own catalog estimate.** Live catalog rows
  carry `cost` + `turns` (`lua_translator.available_production_read`; the
  engine's `GetTurnsLeft` already internalizes accumulated progress, rates and
  modifiers). Horizons 1/3/5 per S3.
- **Unsupported-explicit rule.** Any quantity no accessor observes — progress
  buckets, per-turn rates, food/growth, housing/amenities, live maintenance,
  modifier stacks, or a missing `turns` — becomes an `unsupported` entry.
  Never a guessed zero (`test_missing_accessors_are_unsupported_never_fabricated`,
  `test_zero_or_noninteger_turns_is_not_a_completion_basis`).
- **No hindsight edits.** `classify_outcome` issues separate later-observation
  records (`completed`/`in_estimated_window|late|completion_unsupported`,
  `overdue_pending` once, `invalidated_queue_changed`); the issued forecast is
  byte-stable afterwards (pinned in module and wiring tests).
- **Bounded comparison** (`compare_alternatives`): ≤4 candidates mirroring the
  production-policy cascade (not the full legal catalog — `candidate_coverage`
  says so), one composed counterfactual (investment-then-next at constant
  rates), threat contingency as a separate branch, deterministic selection
  weights with algorithm id. `statics` is an explicit optional input (supplied
  = static effects, absent = `static_yield_and_maintenance_effects` unsupported).

### Option monitor (`build_option.py`)

`develop_city_under_threat_watch/v1`: register-on-accepted, observe at economy
start (BEFORE any economy action — an interrupt is visible before the next
primitive effect), resolve on later queue observations. A confirmed
`is_barbarian` contact within `THREAT_RADIUS` of an owned city (the same
observable `production_policy` uses) **censors** non-defender forecasts; the
censor flag survives into the completed outcome. Advisory only — the
production policy's defense override remains the only behavioral effect of a
threat.

### Wiring (default-off, advisory-only)

- Config knob `llm.economic_forecast` (LLMSpec, default `False`) requires
  `strategic_autopilot` + `adaptive_context`, enforced at BOTH `parse_config`
  and `build_runtime` (the `research_building_briefing` precedent).
- `_decide` gains `metadata['economic_forecast']` (pending, recent outcomes,
  ≤2 catalog-only city plans, labeled `advisory_only_executor_unchanged`) —
  the `metadata['growth']` additive pattern; `context_curator.py` untouched.
- `_economy` resolves pending options before any action, emits a pre-action
  `strategy_forecast` audit (forecast + comparison over the policy's own
  candidates), and registers the forecast only on `accepted`.
- **Off = unchanged:** knob-off produces zero `strategy_forecast*` audits, no
  metadata key, and byte-identical executor call sequences (pinned by
  `test_knob_off_default_is_inert`, `test_knob_on_leaves_executor_calls_identical`,
  and the untouched existing seam suites — 366 passed at wiring time).

## Evidence

- `runs/s3-forecast-evidence-20260907/`: focused logs, full pytest + ruff
  gate, `mutations/` (5 targeted semantic mutations, all killed).
- Independent-engine scenario (`tests/test_forecast_engine_scenario.py`): the
  real sim engine produces the queued WARRIOR; the monitor's `completed`
  classification agrees with the actual unit spawn. The sim catalog publishes
  no `turns` estimate, so its forecasts are honestly completion-unsupported —
  which is itself pinned.
- **Mutation runs caught one real coverage hole** (the exact-boundary pending
  case at the estimated completion turn) — fixed and re-killed.

## Traps hit

- **NEVER run the sed-mutation harness with uncommitted work in the tree.**
  `git checkout -- <file>` after each mutant reverts to HEAD — one uncommitted
  review-fix round was wiped mid-harness and had to be rewritten from the
  session transcript. Commit first, mutate second.
- **Same-length sed mutations + `__pycache__` = stale-bytecode false
  failures.** The m5 revert left a cached `.pyc` matching the restored file's
  size/mtime; `inspect.getsource` showed correct source while stale bytecode
  executed. The mutation harness must clear `__pycache__` (or copy to temp
  dirs with `PYTHONDONTWRITEBYTECODE=1` — exactly what the reference
  package's `mutation_checks.py` does). A mutant exiting 4 (collection error)
  is NOT a kill — require exit 1 with a failure line in the log.
- **`pytest | tail` in a subshell masks exit codes** — the evidence commit
  initially landed with a red focused run (stale-bytecode artifact). Capture
  to a file and read the summary; never trust a masked pipe.
- **The sim advances a round only after the LAST seat's `end_phase`** —
  single-seat drivers stall on `turn mismatch`.

## Deliberately out of scope (recorded follow-probes)

1. **Static-effect enrichment:** deriving per-item yields/maintenance from
   `research_building_unlocks` into `compare_alternatives(statics=...)` when
   the briefing is also on.
2. **Rate-derived estimator for adapters without `turns`** (the sim): a
   validated-subset model (ceil(cost / production-per-turn) from the engine's
   declared math), differentially compared against engine completion — the
   O2 model-class ladder's step 2.
3. **Live Lua accessors** for production progress/rates (currently declared
   placeholders in `response_parser.py`) — needs a live game session.
4. Research-side completion forecasts (cost basis only today).
5. O5 evaluation contract freezing (arms A/B/C of the treatment ladder) —
   must be frozen before this lane's advisory is compared for effect.
