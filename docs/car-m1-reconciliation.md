# CAR-M1 graph-turn-core reconciliation

Date: 2026-09-02  
Packet: `CAR-M1-GRAPH-TURN-CORE-001`  
Scope ruling: **`AMEND_M1`**

The repository already contains useful M15–M20 planner, replay, visibility,
FireTuner, hotseat, experiment, and evidence machinery. CAR-M1 replaces the
turn-control contract with V2; it does not restart the repository or treat the
existing plan-local DAG as the required legal-action graph.

## Custody and repository binding

| Surface | Bound state |
|---|---|
| Source checkout at campaign handoff | `master` at `f60282ff2eda9cf991ba509a33b1c6772b798685`, tree `808ba83a1a2fbe2caa426f96999e75cc2192888c` |
| CAR worktree | `/home/alexk/worktrees/civ-arena-car-m1-v2-20260902`, branch `car-m1-v2` |
| Clean-floor commit | `eb43f337866c49d75722944b280ea884f4145d25`, tree `a1ee1dfb49874c74e795e591bb2343eb4b159af6` |
| Latest implementation commit before final reporting | `b60ac0426c5462fa88ab24796e98dcb5c1a3b4ad`, tree `527a590f0069f852db497ce899a16412ce6158e0` |
| GitHub repository | private `AlexKlick/civ-arena`; default branch `master` |
| GitHub default-branch head at reconciliation | `292ea9209481fb67896179722097f24a745aa804` |
| Pull requests at reconciliation | none |
| Integration authority | none; local CAR commits are not merge or push authority |

The remote default branch is intentionally behind the local source checkout.
Repository, remote, integration, provider, and live proof remain separate.

## Supplied packet custody

The packet files remain external evidence and are not copied into the
repository. Their exact bytes were read from the supplied paths.

| File | Bytes | SHA-256 | Disposition |
|---|---:|---|---|
| `/home/alexk/pop-deck-uploads/2026-09/civ-arena-next-milestone-handoff-2026-09-02-ee1f90.md` | 34,781 | `42e0cb7dcf5a0842f0484bd3611256ed585ebb926fc2893d73e4460516d96b0e` | Reconciled; stale repository facts were not copied verbatim |
| `/home/alexk/pop-deck-uploads/2026-09/civ-arena-next-milestone-lane-graph-2026-09-02-bd964a.json` | 5,415 | `d7578b5e0be65864223ae8fd19bb0a77338b89bfa52b9207c062de2ae57d7b88` | Reconciled as the provisional lane graph |

The packet said to keep M20 paused. M20 was completed by the prior owner before
CAR-M1 obtained a stable worktree (`f60282f`). Those commits and artifacts are
preserved as historical substrate. CAR-M1 adds no M20 or M21 feature work and
makes no claim that the prior learning lanes satisfy this packet's turn-core
gates.

## Clean-floor evidence

CAR-101 repaired the two retained pytest failures and the two Ruff findings
present at the actual handoff. The tested working diff was SHA-256
`e44c943dd3f018b48fd25cb0dede499a862582c27aec6c74f7f017e772535216`
before and after the serial gate. The original scratch-log names are reused by
the final exact-head gate, so the durable baseline result is this reconciliation
record plus clean-floor commit `eb43f33`, not the later contents of those `/tmp`
paths.

| Gate | Retained log | Result |
|---|---|---|
| `.venv/bin/python -m ruff check .` | CAR-101 captured run | PASS: zero findings |
| `.venv/bin/python -m pytest -q` | CAR-101 captured run | PASS: 521 passed, 0 failed, 1 skipped in 615.51s |
| `uv build` | CAR-101 captured run | PASS: sdist and wheel built |

This is repository/build proof only. It is not provider, browser, FireTuner,
live Civ VI, remote integration, or independent-review proof.

## M1 gate matrix

| M1 gate | Implemented substrate | Verification boundary | Owner | Disposition |
|---|---|---|---|---|
| Versioned state/action contract | Nine Draft 2020-12 documents under `schemas/v2/`; frozen strict `from_doc`/`to_doc` types and semantic identities in `v2/contracts.py` (`13c3c54`) | Repository tests and schema construction are verified; no claim about external consumers | CAR-102 | COMPLETE |
| Hidden-state isolation | Observable execution facet and private referee monitor; closed observation vocabulary; safe errors/receipts; noninterference pins (`13c3c54`, `caa2954`) | Actual-live observation behavior remains part of the blocked live lane | CAR-102/CAR-104 | SOURCE COMPLETE |
| Action dependency graph | Full observable legal-action graph with eight typed edge kinds, stable identities, dominance, deterministic witnesses/order, overflow refusal, exact reduction (`c8a19c1`) | Verified in simulator/property tests; no provider/live extrapolation | CAR-105 | COMPLETE |
| Transactional revalidation | Graph-bound executor remaps each intent and fully recomputes after every accepted mutation; terminal end-turn and system housekeeping are authorized proposals (`4193051` through `128cbd2`) | Actual Civ VI execution is blocked at host launch | CAR-105/CAR-107 | SOURCE COMPLETE |
| Receipt and replay | Locked hash-chained V2 event root, content-addressed objects, typed terminal receipts, child resume, exact fake replay, frozen V1 reader (`a5ea3c2`, `b443673`, `128cbd2`) | Exact live replay is not claimed | CAR-103/CAR-106 | FAKE/REPO COMPLETE |
| Live adapter | FireTuner split boundary, V2 hotseat driver, executor-authorized housekeeping, environment identity, four immutable CAR-110 configs; 4/4 fake rehearsals (`0cf3a8b`, `b60ac04`) | Actual four-run Civ VI gate BLOCKED: current Steam handoff starts no game process/window/listener | CAR-104/CAR-107/CAR-110 | BLOCKED (HOST/LIVE) |
| Baselines and experiment | Immutable 80-generated/20-curated manifest and 400-row A/B/C/D result; D=4 vs A=508, B=224, C=225 (`4a86085`, `8e50f7b`, `4da3a2f`) | Provider pilot captured only 14/20 valid proposals; full model strata prohibited | CAR-108/CAR-109 | DETERMINISTIC PASS; PROVIDER BLOCKED |

The normalized evidence and research verdict are in
[`docs/car-m1-v2-validation-report.md`](car-m1-v2-validation-report.md).
Repository completion, deterministic artifacts, provider calls, host/live
execution, remote state, independent review, and owner approval are distinct
lanes. The overall CAR-M1/H1 verdict is `BLOCKED` until the mandatory provider
and actual-live evidence exists, regardless of the deterministic result or
final repository gate.

## GitHub coordination

CAR-101 through CAR-110 are private issue records
[#1](https://github.com/AlexKlick/civ-arena/issues/1) through
[#10](https://github.com/AlexKlick/civ-arena/issues/10). They are not hosted
test gates, pull requests, merge approvals, or live evidence.

## Frozen campaign boundaries

- V2 is a breaking turn-core cutover; V1 evidence remains byte-preserved and
  readable but cannot resume through V2.
- Only the transactional executor may call mutating engine methods.
- Policy proposals, learned artifacts, case memory, diary, strategy, and recall
  are untrusted inputs or read-only services, never legality authority.
- Scored runs cannot resume or import branch observations.
- No new MCTS, MCGS, option-policy, learned-model, league, Neo4j hot-path, or
  unrestricted tool work enters CAR-M1.
- A clean local gate is not an independent Codex `GO`, a remote integration,
  provider proof, or live Civ VI proof.
