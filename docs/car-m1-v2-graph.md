# CAR-M1 V2 legal-action graph kernel

Status: CAR-105 implementation candidate

`ActionGraphCompilerV2.compile(observation, legal_actions)` is a pure,
observable-only compiler. It independently re-enumerates the complete legal
set and refuses a supplied set with any omission, addition, stale observation,
or changed identity. It never calls an adapter, policy, model, referee monitor,
or mutation method.

## Pair classification and dominance

Every unordered node pair has explicit evidence. It receives one or more
authoritative edges when multiple directed facts apply, or exactly one derived
`COMMUTES_WITH` edge. An absent edge is not an independence claim.

The compiler applies these rules in order:

1. Research and per-city production slots are `MUTEX`. A consumed unit also
   excludes another claim on that unit.
2. Rules-proven same-actor sequences and terminal ordering are explicit
   `MUST_PRECEDE` edges. A move that leaves movement and places its unit in
   attack range also carries `ENABLES`; the enabled action is still rechecked.
3. Combined observable resource claims above the current budget are
   `CONSUMES_SHARED_RESOURCE`. An unobservable budget fails closed as
   `CONFLICTS_WITH`.
4. Affordable shared-resource actions invalidate one another's current
   authorization. Purchases are the primary example: either may execute first,
   but the other must be rebuilt against the new gold balance.
5. Effects that contradict a precondition produce directed `INVALIDATES`;
   an effect that is the only visible proof of a target precondition produces
   directed `REQUIRES`.
6. Disjoint affected regions, resources, effects, and preconditions prove
   `COMMUTES_WITH`. Stochastic attacks do not commute with each other because
   they share an unobservable RNG stream.
7. Any remaining pair is `CONFLICTS_WITH`. Unknown or incomplete effect claims
   therefore cannot manufacture independence.

Constraint, resource, and invalidation evidence always prevents a commute
edge. Symmetric edges use canonical endpoint order. Directed cycle validation
and its deterministic action-ID witness remain part of the frozen graph
contract.

## Canonicalization and metrics

`canonical_action_order(graph, selected_ids)` rejects unknown/duplicate IDs,
prohibited co-selection, and a target selected without its `REQUIRES` source.
It then topologically sorts `MUST_PRECEDE` and `REQUIRES`, choosing the smallest
stable action ID on every frontier. Proposal order and enumeration position
have no effect.

Metrics use integer-only, deterministic definitions:

- `raw_permutation_estimate` is the exact count of all non-empty ordered
  selections, `sum(P(n,k), k=1..n)` (and 1 for an empty graph).
- `canonical_plan_count` is the exact count of non-empty compatible subsets
  through 20 actions. Above 20 it is the deterministic upper bound `2^n-1`,
  labeled in `rejected_reasons`; no exponential production enumeration is
  attempted.
- `independent_action_groups` is the component count after joining every
  non-commuting pair.
- `reduction_ratio_fixed` is raw/canonical at scale 10,000.
- `compile_duration_ms` is zero in this deterministic implementation and is
  normalized out of graph identity by the contract.

## Complexity and known boundary

Pair classification is `O(n^2)` time and space because the contract records
positive commute witnesses rather than inferring them from missing edges. The
schema limit is 1,024 actions; the enumerator fails closed before truncating an
oversized set. Exact compatible-subset counting is deliberately bounded as
described above.

The graph describes legal actions at one observation. It does not promise that
an `ENABLES` target will remain or become legal, infer facts through fog, or
reuse an action identity after mutation. CAR-107's executor must re-observe,
re-enumerate, and recompile the complete graph after every accepted mutation.
The V1 plan-local DAG remains historical substrate only and is not an authority
for V2 execution.
