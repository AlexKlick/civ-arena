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
before and after the serial gate.

| Gate | Retained log | Result |
|---|---|---|
| `.venv/bin/python -m ruff check .` | `/tmp/car-m1-ruff.log` | PASS: zero findings |
| `.venv/bin/python -m pytest -q` | `/tmp/car-m1-pytest.log` | PASS: 521 passed, 0 failed, 1 skipped in 615.51s |
| `uv build` | `/tmp/car-m1-build.log` | PASS: sdist and wheel built |

This is repository/build proof only. It is not provider, browser, FireTuner,
live Civ VI, remote integration, or independent-review proof.

## M1 gate matrix

| M1 gate | Current status and exact evidence | Residual V2 gap | Owner | Disposition |
|---|---|---|---|---|
| Versioned state/action contract | `src/civ_arena/game/adapter.py` has frozen-ish adapter dataclasses; `src/civ_arena/arena/events.py` writes schema 1 | No Draft 2020-12 schemas, strict V2 construction, semantic identities, or explicit knowledge states | CAR-102 | Replace at the public turn boundary; retain V1 reader |
| Hidden-state isolation | Bound sessions, `arena/visibility.py`, simulator ground-truth oracle, and hostile-agent tests exist | Adapter observation is omniscient before projection; V2 policy values/errors/receipts are not structurally observable-only | CAR-102/CAR-104 | Amend and split policy/referee facets |
| Action dependency graph | `planner/action_dag.py` canonicalizes one selected plan and has local dependency rules | Not the complete observable legal-action graph; lacks normative edge identities, complete edge kinds, graph identity, and deterministic cycle witnesses | CAR-105 | Replace as execution authority |
| Transactional revalidation | `planner/executor.py` prevalidates against a rolling belief and reconciles selected tools | It does not bind authorization to observation/graph identity or fully re-observe, re-enumerate, and recompile after every accepted mutation | CAR-105/CAR-107 | Replace mutation path |
| Receipt and replay | `arena/events.py` is sequenced append-only V1; `replay.py` re-executes fake runs | No event hash chain, object custody, typed terminal receipts, environment binding, or V2 tamper verification | CAR-103/CAR-106 | Freeze V1 read-only; new runs V2 only |
| Live adapter | FireTuner adapter, fake tuner, hotseat driver, and retained M18 evidence exist | Policy/referee facets are not split; housekeeping can mutate outside a graph-authorized system proposal; no V2 capability descriptor | CAR-104/CAR-107/CAR-110 | Port without claiming fake smoke as live proof |
| Baselines and experiment | M19/M20 experiment tooling is historical evidence | No identical-proposal A/B/C/D turn-core corpus or declared control-failure composite | CAR-108/CAR-109 | Build isolated V2 harness; keep provider evidence separate |

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
