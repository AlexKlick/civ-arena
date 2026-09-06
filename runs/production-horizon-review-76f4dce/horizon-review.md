# Production horizon review and snapshot proposal

Evidence date: 2026-09-06. Read-only source target:
`/home/alexk/civ-arena-reliability-20260904`, clean HEAD
`76f4dce0a344f749d6ddbea42b4f92dcba9d649e`, tree
`7de749d64ce92e4db54a7c5b6a8058cecd67e978`. This is the implementation
used to start `minimax100-20260906T193543Z`. The run's game, tuner, provider,
desktop, configuration and source were not modified or queried by these probes.
All new artifacts and the subsequently authorized queue correction are isolated
in `/home/alexk/civ-arena-production-targets-20260906`.

## Verified findings

1. **P2, conditional queue defect; isolated correction prepared.**
   `src/civ_arena/game/civ6/lua_translator.py:214` emits an active item absent
   from the unit/building catalogs as `UNKNOWN_PRODUCTION_<hash>`.
   Signed target hashes are accepted by the existing production verification
   contract (`src/civ_arena/game/civ6/firetuner.py:151`). An active
   `UNKNOWN_PRODUCTION_-123` city is skipped safely when it is the only city,
   but an additional idle city invokes whole-empire queue counting at
   `src/civ_arena/agents/production_policy.py:82`; `_queue` then passes the
   opaque string through the real-item regex (`production_policy.py:49`),
   raising ValueError before the other city's valid build. A positive opaque
   token does not trigger that failure. This is reproduced through the native
   city parser, real curator, real strategic controller, and offline facade in
   `test_unknown_active_hash_can_abort_different_idle_city`.
   An unsupported inherited/captured queue could trigger the condition; this
   fresh controller cannot create such district/project queues itself. There
   is no evidence that the current live attempt is affected.

   The authorized isolated correction recognizes canonical nonzero signed
   opaque hashes with magnitude below 2^53, keeps those queues occupied,
   excludes them from unit counts and records their exact tokens separately.
   It does not resolve or replace the unsupported build. Malformed lookalikes
   remain rejected. Its independent review and any future integration are
   separate from this read-only report; the running implementation stays frozen.

2. **Confirmed native capability boundary: districts and projects are absent.**
   `lua_translator.py:315` and `:334` enumerate only `GameInfo.Units` and
   `GameInfo.Buildings`. `response_parser.py:304` rejects other item kinds.
   `_resolve_item_lua` (`lua_translator.py:815`) also resolves only those two
   catalogs, and `session/tools.py:100` has no placement coordinate in the
   production action. Thus a district/project-only feasible native choice has
   no supported catalog/action representation. Model target increases cannot
   add such a choice or construct its district prerequisite. If the represented
   catalog becomes empty, the controller aborts at
   `agents/llm/strategic_controller.py:398` without a target-refresh request.
   This is pre-existing surface coverage, not proof that a 100-round run must
   encounter an empty catalog. No claim that currently absent MONUMENT or
   GRANARY entries indicate an API failure: already-built items can be absent.

3. **Valid model target increases do work; the ceiling remains real.**
   `test_current_model_path_can_raise_owned_plus_queued_target` starts with two
   owned scouts and a third queued elsewhere, supplies a model response raising
   the target to four, and observes one accepted production action followed by
   closure. The actual late request contains the roster, queue and current
   production catalog. At 32 owned scouts with SCOUT the only offered item,
   a requested target of 33 is rejected, including the one bounded format
   repair; no production action or turn closure occurs. This is the documented
   hard ceiling, not an implementation regression. It proves a conditional
   terminal limit, not that this inventory will occur before turn 100.
   A new directive is a replacement snapshot: omitted targets revert to defaults
   rather than merging previous overrides. The previous directive is supplied
   as context, so continued raised targets must be retained by the model.

4. **Defense and active-queue behavior remain deliberately limited.**
   The defense census at `production_policy.py:16` and `:106` recognizes only
   ARCHER, SUMERIAN_WAR_CART, SPEARMAN, WARRIOR and SLINGER. A source fixture with
   three owned CROSSBOWMAN rows still reports zero counted defenders and can
   choose ARCHER against a confirmed nearby barbarian. A CROSSBOWMAN-only catalog
   with one owned unit retains the default target of one; it receives no extra
   automatic defensive target. These are tested opening-allowlist limits, not
   claims about battle quality or a native unit currently available in the run.
   Active queues receive no replacement action or catalog query even under a
   persistent preference. Consequently raising targets cannot preempt an
   already-running build for emergency defense; that capability is absent.

| Check | Result | Evidence lane and boundary |
|---|---|---|
| Main-source horizon probes | 10 passed, zero failed/skipped/deselected in 0.07s | `probes.log`; source assertions and offline facade execution. Passing reproduction probes confirm the reported behavior, including the defect. |
| Isolated queue correction | 187 passed, zero failed/skipped/deselected in 0.30s | `opaque-queue-focused-final.log`; production, directive and strategic controller files. |
| Isolated correction Ruff | Passed on three changed Python files | `opaque-queue-ruff-final.log`; first pass's single test line-length finding remains in `opaque-queue-ruff.log`. |

Original probe command, from the isolated production worktree:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/home/alexk/civ-arena-reliability-20260904/src /home/alexk/documents/civ-arena/.venv/bin/python -m pytest -q -p no:cacheprovider runs/production-horizon-review-76f4dce/test_horizon_probes.py > runs/production-horizon-review-76f4dce/probes.log 2>&1
```

## Smallest coherent snapshot proposal, not implemented

Use one explicit controller observation operation for map, units and cities.
The live adapter already reads units in GameCore and cities in InGame to derive
visibility (`firetuner.py:612`), then reads the visible terrain. Reuse those exact
actor rows for the controller's unit/city observations. Keep the two VM reads
sequential on the single tuner client; this is a sequential read group, not an
atomic engine snapshot.

A bounded controller-only facade capability can call a referee observation-group
method. The referee validates the existing lease and phase, asks the adapter for
an anchored map/actor bundle, projects every member against the visibility sets
from that same bundle, and emits the existing get_visible_map/get_units/get_cities
TOOL_CALL/TOOL_RESULT pairs in their existing order. Strategy observation digests
and result-sequence provenance are retained. The curator consumes the three
projected results together, then makes its existing overview and demand-driven
catalog reads. No omniscient actor rows or provider-facing tool expansion are
needed. Adapters without the optional grouped capability use the existing path.

The bundle should exist only within that operation and be consumed once. Bind it
to game-instance/connection generation, player, active lease identity, normalized
turn, local action generation, source query sequence IDs and VMs, and the exact
visibility/remembered-set digest. Copy outputs so mutable callers cannot alter
provenance. Add native turn/player stamps to the existing source responses where
the already-supported accessors allow it; reject inconsistent members. Do not
claim a cross-VM transaction or invent a new host API. If phase evidence is
unavailable, fall back to the current separate reads.

Serialize the group with local actions. Invalidate/discard on every action
attempt (including rejection and deduplication), phase or lease change,
connect/disconnect, raw driver commands in either VM, UI recovery/handoff,
partial reads, parse failure, timeout or cancellation. Each post-action curator
refresh obtains a new group. Avoid a TTL cache or reuse across model thinking,
turn boundaries, or independent facade calls.

Keep separate fresh ownership/legality reads (`referee.py:582`), production hash
verification (`firetuner.py:771`), post-command digest refresh (`:776`), mutation
receipts, unmoved-unit closure checks, final sweeps and movement-allowance owner
reads. Those are different proof purposes and must not consume this controller
bundle. This first change therefore removes exactly the duplicate actor reads
in the controller map refresh, without weakening command or terminal checks.

The retained failed-run profile at
`/home/alexk/civ-arena-reliability-20260904/runs/100-round-live-20260906T181834Z/retained-latency-profile.json`
records 2,970 wire RPCs totaling 987.5406s, with 541 units RPCs, 415 cities RPCs,
and 123 terrain RPCs; 14 strategy request-response spans total 50.69199s. These
are overlapping descriptive lanes and cannot be added or treated as a controlled
speed comparison. If all 123 map reads had one immediately duplicated unit/city
pair, this proposal would remove at most 246 RPCs, about 81.8s at the retained
mean, roughly 8.3% of recorded wire time. That is an upper-bound estimate requiring
trace correlation, not a measured speedup. Do not claim the entire units/cities
318.389s would disappear, or remove the 306 digest RPCs to inflate the estimate.

## Follow-up probes

- Freeze and independently review the queue correction. Integrate only through
  the parent's lane; the current fresh game remains on its original commit.
- During the ongoing authorized run, parent-owned monitoring can inspect real
  production_targets_satisfied replies, retained overrides, active queues and
  accepted choices. No such provider result was generated by this review.
- For snapshot implementation, test exact 5-to-3 raw-read reduction in a full
  map/actor refresh, unchanged projected observations and existing event order,
  foreign-entity visibility with moved anchors, stale generations, seat switches,
  rejected actions, partial/cancelled reads, and preserved post-action/final
  verification counts. First benchmark the matched raw-read trace, then a fresh
  authorized live stage. These checks are proposed, not performed.

## Blocked checks

No Lua interpreter was available in this worktree environment for executing the
translated chunks against a fake GameInfo surface. Catalog omissions were
verified from generated source and Python parser/action signatures, not from
new native host calls. District placement and project APIs need a separately
owned native capability probe before any implementation; they were not guessed.
Full release validation and native gameplay were outside this read-only scope.

## Evidence gaps

There is no new provider, browser, tuner or live-engine proof in this report.
The inherited/captured unsupported-queue trigger is a concrete conditional
reproduction, with no observed occurrence in the current fresh run. The modern
unit rows in probes are synthetic exact-ID fixtures. Neither the capacity limits
nor the snapshot timing estimate establish failure or success by turn 100.
