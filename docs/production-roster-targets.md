# Production roster targets

Local evidence date: 2026-09-06.

This opt-in strategic controller change prevents a recurring preference such as
`SCOUT` from rebuilding that unit forever. It is prepared in
`/home/alexk/civ-arena-production-targets-20260906`, branch
`fix/production-roster-targets-20260906`, from base
`684efdffef21a34172b90adb70eb6faf3b744648`. The preserved four-seat branch and the
parent's preserved live 100-round game were not modified by this work.

## Policy contract

`production_preferences` ranks each empty city's next build and persists until the
model changes it. It is not a consumable build-order list. The deterministic
policy counts observed owned units plus units in every owned city's observed
queue. Accepted queue requests that have not appeared in that city's refreshed
observation count as conservative reservations only within the current economy
pass; the audit reports them separately from observed inventory. Cities are
processed in sorted ID order, and each gets at most one production action per pass.
Existing queues are never replaced. Rejected requests do not reserve inventory.

Defaults are two scouts across the empire, one builder per owned city, and one
of each other unit type. A preferred unit at its target is skipped. The policy
tries another eligible preference, an available building, then an eligible unit
with a deterministic opening priority. Owned builders are counted, without
inventing remaining build charges. These conservative inventory defaults are
heuristics, not a measured strategy optimum.

The model may supply optional `unit_targets`, an object of at most 16 exact unit
item IDs with integer desired counts from 0 through 32. Zero disables new
production of that unit; raising a target permits more. Counts include queued
units. Unavailable IDs are audited and cannot be translated into guessed aliases:
`WAR_CART` cannot select `SUMERIAN_WAR_CART`. Only exact items in the current
city's observed catalog can be issued. The catalog must identify each item's
observed `unit` or `building` kind; malformed/missing queues, owned identities,
unit types, duplicate catalog IDs, or unsupported kinds stop execution.

For example:

```json
{
  "version": 1,
  "scouting": {"unit_types": ["SCOUT", "WARRIOR"]},
  "production_preferences": ["SUMERIAN_WAR_CART", "SCOUT", "MONUMENT"],
  "unit_targets": {"SUMERIAN_WAR_CART": 2, "SCOUT": 2, "BUILDER": 1}
}
```

Only an explicit projected `is_barbarian: true` on a foreign visible unit within
five axial hexes of an owned city enables defensive priority. A foreign seat,
player-number guess, missing classification, or truthy non-boolean value does
not establish a barbarian. The opening defender allowlist is `ARCHER`,
`SUMERIAN_WAR_CART`, `SPEARMAN`, `WARRIOR`, and `SLINGER`, filtered through the
actual city catalog. The default defensive shortfall is two defenders per owned
city in total, counting owned, queued and reserved defenders. Explicit model
targets still apply. This does not infer diplomacy or declare war. The distance
check does not infer map wrapping or unseen contacts.

`scouting.unit_types` assigns persistent roles. Defaults include `SCOUT`; an
explicit omission holds scouts and suppresses new scout production. Tactical
orders still take precedence. Existing unassigned scouts receive an audit graph
warning on initial and executed decisions. SYSTEM and schema explain the role
semantics so the model can revise its next directive. The controller does not
silently override an explicit exclusion.

If observed production candidates exist but all are ineligible, the controller
requests at most one strategy refresh for the entire economy pass, with reason
`production_targets_satisfied`. It reuses current curated observations and the
existing `_decide` path. Initial decisions, format repair and this late refresh
share `max_tool_rounds`; every transport POST retains the existing match cap.
After that refresh, selection is retried once. Failure to find an eligible item
is an audited controlled abort. An observed empty catalog also aborts, without
asking the model to invent an item. No retry or discovery loop is added.

A late directive persists and updates the cadence clock, but its tactical
orders are explicitly discarded because scouting has already executed. Its
movement metadata advertises no untouched frozen units. Routine quiet turns
continue without model requests when choices remain eligible. Context rendering,
provider configuration and caps, tuner protocol, installed mod, replay,
movement-drift allowance, and desktop control were not changed in this branch.
Old directives are accepted; empty/omitted targets preserve their historical
normalized shape and scouting seed hash. Nonempty targets are explicit new
strategy input and consequently participate in the existing directive hash.

## Verified findings

| Status | Evidence lane | Evidence | Claim boundary |
|---|---|---|---|
| PASS | Repository | `runs/production-targets-local/focused-final.log` | 241 passed, zero failed/skipped/deselected in 41.01s across the eight listed focused files. No provider or native game traffic. |
| PASS | Repository | `runs/production-targets-local/ruff-final.log` | Ruff checked the seven changed Python files, with no findings. |
| PASS | Repository | `tests/test_production_policy.py`, `tests/test_strategic_controller.py` | Owned/queued/reserved caps, two-city ordering, exact IDs, bounded overrides, confirmed-threat behavior, role exclusions, late refresh/repair budgets, and honest closure failures. |
| PASS | Repository fake integration | `tests/test_strategic_wiring.py` within the focused log | Existing fake hotseat dispatch closes two ordered turns and honors a warrior preference with an explicit target of two. This is not live-engine proof. |

Reproduce from this worktree using the existing repository virtual environment:

```bash
PYTHONPATH=src /home/alexk/documents/civ-arena/.venv/bin/python -m pytest -q tests/test_strategy_directive.py tests/test_production_policy.py tests/test_strategic_controller.py tests/test_strategic_wiring.py tests/test_scouting.py tests/test_scouting_standing_intent.py tests/test_scouting_nonprogress.py tests/test_context_curator.py > runs/production-targets-local/focused-final.log 2>&1
PYTHONPATH=src /home/alexk/documents/civ-arena/.venv/bin/python -m ruff check src/civ_arena/agents/strategy_directive.py src/civ_arena/agents/production_policy.py src/civ_arena/agents/llm/strategic_controller.py tests/test_production_policy.py tests/test_strategy_directive.py tests/test_strategic_controller.py tests/test_strategic_wiring.py > runs/production-targets-local/ruff-final.log 2>&1
```

Historical local iterations remain captured. `focused-initial.log` recorded
9 failed/163 passed: seven new error-message assertions, one new test naming a
nonexistent per-turn config field, and the existing fake warrior preference test
whose observed warrior already met the new default target. These were corrected;
the fake preference test now explicitly requests two warriors.
`focused-refined.log` then recorded 177 passed. `focused-settled.log` recorded
1 failed/239 passed: adding an empty field had shifted a scouting hash and exposed
a synthetic negative-map-row destination in an adapter fixture. Preserving the
legacy normalized shape fixed that compatibility regression. Earlier Ruff logs
record only new line-length findings, corrected before the final checks. No
failure is attributed to the reviewed baseline or hidden by deselection.

## Follow-up probes

| Probe | Why it remains separate | Next evidence |
|---|---|---|
| Exact-head independent review and integration | Parent owns the integration branch and context/hostile projection work. | Review frozen commit/tree, merge, then run affected integration and release checks. |
| MiniMax target/role selection | The tests use deterministic fake model replies. | Parent's next authorized fresh live probe should inspect submitted targets, per-city policy rows, and scout assignment corrections. |
| Longer operational behavior | No native game ran from this branch. | Observe a fresh 100-round run with complete ordered turns, existing caps and allowance recorded. |

## Blocked checks

| Check | Blocker | Next probe |
|---|---|---|
| Native barbarian classification and defensive production | Native projection belongs to the parent's separate mod/context lane. This branch consumes only the explicit field and does not install it. | Integrate the parent's validated projection, then inspect live observed contacts and the production audit. |
| Full release and live acceptance | Deliberately deferred to parent integration; this task does not own the preserved game, tuner, provider or desktop. | Parent runs the merged release gate and the agreed live sequence. |

## Evidence gaps

The graph warnings have source and deterministic execution tests but no new
browser-visible capture. The current production audit is carried by existing
`strategy_economy` events; no new dashboard panel is claimed. This work does not
prove calibrated strategy quality, battle tactics, native map wrapping, or a
completed 100-round game. Repository checks do not replace the two-fresh-30-round
operational acceptance contract; the user-requested 100-round stage remains a
separate live objective.

## P3 repair-prompt follow-up, 2026-09-06

Review of `7705a601b6d7b8f989e66329c828c13971b72cd0` identified one
nonblocking prompt defect: a late format repair claimed that no game action had
executed, after scouting or earlier economy actions could already have run. The
repair now states that no action from the rejected response was executed, which
is accurate for both malformed replies and rejected directive arguments in both
phases. Late movement authority continues to expose no untouched frozen units.

The earlier 241-test result and `source-custody.json` describe the original
implementation commit. This wording-only source follow-up has a separate captured
regression result: `runs/production-targets-local/p3-focused.log` records
35 passed, 31 deselected, zero failed/skipped in 0.22s;
`runs/production-targets-local/p3-ruff.log` records Ruff passing on the two changed
Python files. `runs/production-targets-local/source-custody-p3.json` binds the
current source/test hashes. New tests inspect a real late repair request after an
accepted facade research action, including malformed text and foreign-ID argument
rejection, and confirm honest instructions, unchanged movement authority, and
successful closure within the shared request budget. No broad suite or live
checks were rerun for this narrow source delta.

```bash
PYTHONPATH=src /home/alexk/documents/civ-arena/.venv/bin/python -m pytest -q tests/test_strategic_controller.py -k 'repair or budget or cap or closure or completeness or late_production_refresh' > runs/production-targets-local/p3-focused.log 2>&1
PYTHONPATH=src /home/alexk/documents/civ-arena/.venv/bin/python -m ruff check src/civ_arena/agents/llm/strategic_controller.py tests/test_strategic_controller.py > runs/production-targets-local/p3-ruff.log 2>&1
```
