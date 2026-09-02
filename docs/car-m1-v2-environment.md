# CAR-M1 V2 environment boundary

Status: CAR-104 implementation candidate

The V2 environment is split into two independently handed-out objects:

- `ObservableExecutionFacetV2` exposes only `reset`, `begin_turn`, `observe`,
  `execute_authorized`, and the immutable environment descriptor.
- `PrivateRefereeMonitorV2` exposes snapshot/restore, state hashing, and the
  mutation journal. It is retained by the coordinator/watchdog and is never a
  policy input or a receipt payload.

Both simulator and FireTuner adapters enter through `split_adapter_v2`. The
simulator descriptor is repository-derived and deterministic. A FireTuner
descriptor requires the caller to supply handshake-verified game, ruleset,
mod, and adapter identities; no local default is presented as live evidence.
The fake tuner exercises the same projection and enumeration path, but that is
source/rehearsal proof only, not a live Civ VI result.

## Observation construction

The adapter's omniscient V1 reads remain private to the core. Each read is
first projected by the existing visibility policy and then translated to the
closed `ObservationV2` fact vocabulary. Hidden entities are absent. Foreign
facts use the schema's reduced field set. Empty research is represented by the
absence of a `researching` value plus an explicit mandatory action kind, never
by a privileged sentinel.

Live production rows now optionally carry the engine-observed gold purchase
cost. Frozen five-field V1 rows remain readable. The V2 enumerator uses only
the observed purchase cost; it does not apply the simulator's `cost * 2` rule
to a live game.

Observation sequence is part of observation identity. Emitting a newer
observation makes earlier authority stale. Any accepted/duplicate mutation,
or any adapter exception with an uncertain outcome, invalidates the current
observation before another dispatch can occur. Raw adapter exception text is
collapsed to a bounded safe result.

## Observable legal set

`ActionEnumeratorV2.enumerate(observation)` derives actions solely from the
player observation and static public rules. It includes only moves whose full
path is represented by currently direct-visible tiles; remembered or unseen
terrain is not treated as proof of legality. Visible targets, owned entity
state, available technology/production entries, and observed purchase costs
produce stable observation-bound action identities.

The configured action bound is a fail-closed limit. Overflow raises
`GraphOverflowError`; actions are never truncated. Mandatory research or city
production suppresses `end_turn`. The transactional graph/executor added by
CAR-105 remains the authorization authority and will re-enumerate this entire
set after every accepted mutation.

## Cutover boundary

CAR-104 does not redirect existing policy or live-driver calls. CAR-107 owns
that authoritative cutover, including replacing live-driver housekeeping with
executor-authorized system proposals. Until then, V1 matches remain V1 and do
not produce V2 evidence.
