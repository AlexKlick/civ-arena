# CAR-M1 V2 validation and evidence report

Date: 2026-09-02  
Campaign: `CAR-M1-GRAPH-TURN-CORE-001`  
Scope ruling: **`AMEND_M1`**  
Research verdict: **`BLOCKED`**

CAR-M1's repository implementation is complete through `b60ac0426c5462fa88ab24796e98dcb5c1a3b4ad`
(tree `527a590f0069f852db497ce899a16412ce6158e0`). The validation-report
commit and the final repository gates intentionally come after that source
commit; their exact commit/tree and counts belong in the final handoff so the
gate evidence is not invalidated by a post-gate documentation commit.

`BLOCKED` is a cross-lane research verdict, not a repository-test result. The
deterministic corpus supports the required A/B/C/D ordering. The two provider
strata and four actual Civ VI runs do not have the preregistered evidence needed
to adjudicate H1. No merge or push is authorized by this report.

## Verified findings

| ID | Status | Evidence lane | Evidence | Claim boundary |
|---|---|---|---|---|
| V-01 | PASS | Repository | `schemas/v2/`, `src/civ_arena/v2/contracts.py`, CAR-102 `13c3c54` | Draft 2020-12 schemas and frozen strict Python types implement the breaking V2 state/action contract. This is not provider or live proof. |
| V-02 | PASS | Repository | `tests/test_v2_contracts.py`, `tests/test_v2_environment.py`, `tests/test_v2_graph.py` | Player-scoped observations reject privileged fields and equivalent visible worlds remain noninterfering. Private referee state is not a policy input or persisted receipt field. |
| V-03 | PASS | Repository | `src/civ_arena/v2/graph.py`, CAR-105 `c8a19c1`, `tests/test_v2_graph.py` | Complete observable legal sets compile to stable-ID graphs with all eight edge kinds, dominance, deterministic cycle witnesses, canonical order, and exact fake-state reduction coverage. |
| V-04 | PASS | Repository | `src/civ_arena/v2/executor.py`, CAR-107 `4193051` through `128cbd2`, `tests/test_v2_executor.py` | Proposals are untrusted; authorization binds observation, graph, and legal action; accepted mutations force a complete re-observe/re-enumerate/recompile; unsafe retries and forced end turns are refused. The executor is the sole V2 mutation caller. |
| V-05 | PASS | Repository/artifact | `src/civ_arena/v2/ledger.py`, `src/civ_arena/v2/replay.py`, `src/civ_arena/v1_compat.py`, CAR-103/106 `a5ea3c2`/`b443673` | V2 writes a single locked hash chain with content-addressed objects and terminal receipts. Exact fake replay verifies schemas, objects, chain, environment, and post-state identities. V1 remains read-only and cannot resume through V2. |
| V-06 | PASS | Repository | `src/civ_arena/v2/arena.py`, `src/civ_arena/v2/policy.py`, schema-2 checked-in configs, CAR-107 commits | Scripted, planner, LLM, replay, diary/strategy/recall, policy-state, and live dispatch paths enter proposal -> graph -> transaction. Normal configs require `dag_tx`; experimental unsafe treatments are not reachable from the production match module. |
| V-07 | PASS | Deterministic artifact | `experiments/car-m1-v2/fixture-manifest.json`, `experiments/car-m1-v2/deterministic-results.json`, `/tmp/car-108-deterministic-validate-1.log` | All 100 immutable fixtures (80 generated, 20 curated) ran under A/B/C/D with identical proposal bytes. The artifact is canonical, integer-only, source-bound, and passed its validator. This does not supply model or live evidence. |
| V-08 | PASS | Provider custody | `evidence/car-m1-v2/provider/`, CAR-109 `71d771e` through `cf9e16c`, `/tmp/car-109-provider-postcommit-focused-1.log`, `/tmp/car-109-provider-hardening-focused-3.log` | Attempt self-hashes, source/manifest/model attribution, usage accounting, redaction, ceilings, and strict revalidation are implemented. The pilot outcome itself is BLOCKED below. |
| V-09 | PASS | Fake live rehearsal | Four `configs/car-m1-live-*.yaml`, `tests/test_car_m1_live_gate.py`, `/tmp/car-110-live-rehearsal-1.log`, `/tmp/car-110-live-rehearsal-validate-1.log` | The production `dag_tx` entrypoint completed four fake-FireTuner rehearsals, each 4/4 phases with zero violations. This is explicitly not an actual Civ VI run. |
| V-10 | PASS | Repository guard probes | `/tmp/car-m1-critical-guards.log` | Ten targeted hostile-input/tamper guards passed: schema closure/version refusal, hidden-field injection, conflict-over-commute, hash payload/deletion/reorder tampering, stale/forged authority, and executor-only dispatch. This is input/artifact mutation coverage, not a source-mutation score. |

### Deterministic A/B/C/D result

The result is bound to source commit `8e50f7b299556c1ade35bb4ee062c2fcd66b7c3a`
and tree `152a5336c5a53b1d4749cb965d189652397c090f`. Its semantic result
SHA-256 is `f7bd33d231df4fa1d540d672ac221a6ff373dac28e875f8ce0d0268727d0bb8d`;
the fixture-manifest SHA-256 is
`4eddf07f3b9bfeaf39ca7373487869a78480b8b47913b9cd75a3e9c13b54b810`.

| Treatment | Composite failures | Adapter rejected | Stale authorization reached adapter | Forced closure | Unresolved mandatory | Unhandled divergence |
|---|---:|---:|---:|---:|---:|---:|
| A — sequential | 508 | 308 | 0 | 100 | 100 | 0 |
| B — initial legal list | 224 | 4 | 104 | 100 | 8 | 8 |
| C — initial DAG | 225 | 4 | 105 | 100 | 8 | 8 |
| D — DAG+TX | 4 | 4 | 0 | 0 | 0 | 0 |

The deterministic verdict is
`D_STRICTLY_BELOW_A_B_AND_NO_WORSE_THAN_C`. D's four counted adapter
rejections are the declared `curated-004` through `curated-007` fortify-reject
fixtures; all four turns still completed, and D had zero failures in the other
four composite classes. Detection and successful revalidation are not counted
as failures.

### Provider pilot boundary

The paid pilot was run once from source commit
`7370e17dcf9d339b86e6419077738de6e874a323`, tree
`d93f0e77006c44e2d4dab25df98beb9f14e8b46f`. It exhausted the
preregistered 20 POST attempts but captured only 14 valid proposals, so no full
capture or model-corpus evaluation was allowed.

| Stratum | Attempts / POSTs | Valid attributable proposals | Usage | Status |
|---|---:|---:|---:|---|
| MiniMax-M3 | 10 / 10 | 8 | 137,972 input / 3,723 output tokens | BLOCKED |
| GLM-5.3 | 10 / 10 | 6 | 135,396 input / 9,063 output tokens | BLOCKED |
| Global pilot gate | 20 / 20 | 14 | — | BLOCKED |

The global capture SHA-256 is
`666dc287ce03200da0e8be097d92d0a67b9d56fad0435c2baa67cabf1d437677`.
Only normalized proposals, digests, usage, model identity, safe status, and
attribution were retained. Credential values, authorization headers, cookies,
and raw HTTP bodies are absent from the committed evidence.

### Live gate boundary

CAR-110 defines the required four 20-round, two-seat, `dag_tx` configs and pins
both Civ VI engine seeds to `271828`. The canonical-first repeat config is
identical to gate 1 except for `match_id`. The fake-wire rehearsal verified the
V2 dispatch shape only.

On 2026-09-02, a fresh host probe found resident Steam and the Civ VI install,
but both the URI handoff and `steam -applaunch 289070` returned without
producing a Civ VI process, X window, or listener on 4318/4319. Evidence is in
`/tmp/car-110-live-boot-1.log`, `/tmp/car-110-live-applaunch-1.log`, and the
contemporaneous process/listener checks. No live episode was opened and no
actual-live receipt exists.

## Follow-up probes

| ID | Probe | Why it is not verified yet | Next evidence needed |
|---|---|---|---|
| P-01 | Provider pilot retry | The one authorized pilot yielded 14/20 valid proposals; the protocol requires 20/20 before continuing. | With explicit spend authorization, run `.venv/bin/python -u scripts/car_m1_provider_capture.py --pilot`, then require the strict global gate to pass before any full capture. |
| P-02 | Actual Civ VI four-run gate | The host launcher did not create a game process/window/listener, so staging, intro, and dispatch were unreachable. | Restore an authenticated, visible Steam session on `DISPLAY=:1`; launch app 289070; verify `StagingRoom` and 4318/4319; then execute each `configs/car-m1-live-*.yaml` through `dispatch-hotseat` for 20 rounds and validate its V2 ledger. |
| P-03 | Repeat semantic comparison | Gate 1 and gate 4 have matching config semantics, but no actual-live receipts exist. | Compare the two completed live receipt streams with declared nondeterministic masks; claim exact live replay only if the semantic receipts match. |
| P-04 | Independent exact-head review | This implementation and validation were performed in the CAR worktree; no separate reviewer verdict is recorded at the final handoff commit/tree. | Run a read-only adversarial review at the exact final commit/tree and record a literal GO/NO-GO without changing that tree. |
| P-05 | Owner-controlled integration | The CAR branch has no merge/push authority. | After source GO and the missing provider/live evidence, obtain explicit owner approval before integrating into `master`. |

## Blocked checks

| Check | Blocker | Next command/probe |
|---|---|---|
| MiniMax-M3 full 100-proposal stratum | Pilot captured 8/10 valid proposals, below the 10/10 gate. | Fix only a demonstrated capture/parser defect if one exists; otherwise obtain authorization for one fresh pilot. |
| GLM-5.3 full 100-proposal stratum | Pilot captured 6/10 valid proposals, below the 10/10 gate. | Same gated pilot sequence; do not spend the 120-POST full ceiling before a clean pilot. |
| Four actual-live 20-round matches | Civ VI did not launch from the current resident Steam session. | Operator restores the authenticated graphical Steam session, then rerun the host preflight and the four immutable configs. |
| Overall H1 verdict | Both model strata and all four actual-live runs are mandatory verdict inputs. | Complete P-01 through P-03; until then the result remains `BLOCKED`, not `SUPPORTS_H1`. |
| Integration to `master` | Explicit owner approval has not been granted. | Exact-SHA/tree review first; merge/push only after owner approval. |

## Evidence gaps

| Claim | Missing or stale evidence | Normalized wording |
|---|---|---|
| Source-mutation score | No mutation framework or source-mutant score was run. | Ten critical hostile-input/artifact-mutation guards passed; no source-mutation percentage is claimed. |
| Model-stratum ordering | There are no complete 100-proposal captures for either requested model. | Provider custody is implemented; model comparison is blocked. |
| Live success or exact live replay | No actual Civ VI V2 episode or terminal receipt exists for the four CAR-110 configs. | Fake-driver rehearsals pass; actual-live proof is blocked at host launch. |
| Remote/default-branch integration | The reconciliation snapshot is not an integration receipt and the CAR branch was not pushed by this campaign. | Remote and approval lanes remain unproven and unauthorized. |
| Independent review GO | No separate exact-head reviewer verdict exists. | Repository self-validation can establish a source candidate only, not independent merge authority. |

## Final exact-head repository gates

After this report and the reconciliation update are committed, the handoff runs
these commands once against the resulting clean tree and reports the exact
commit, tree, exit status, and counts from the complete captured logs:

```bash
.venv/bin/python -m ruff check . > /tmp/car-m1-ruff.log 2>&1
.venv/bin/python -m pytest -q > /tmp/car-m1-pytest.log 2>&1
uv build > /tmp/car-m1-build.log 2>&1
```

No post-gate commit is permitted. Even if all three repository gates pass, the
campaign-level research verdict stays `BLOCKED` until the provider and actual
live rows above are complete. CAR-M1 adds no M20/M21 work; further milestone
work remains paused.
