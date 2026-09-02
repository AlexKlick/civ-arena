# CAR-M1 V2 observable turn contract

Status: CAR-102 contract freeze candidate

JSON Schema dialect: Draft 2020-12

Schema namespace: urn:civ-arena:*:2

This contract is the breaking policy/execution boundary for CAR-M1. Schema 1
episode logs remain historical, read-only inputs to the compatibility reader;
they are not valid V2 runtime documents and cannot be resumed as V2 episodes.

The machine-readable source of truth is schemas/v2/. Frozen Python mirrors live
in civ_arena.v2.contracts. Every public from_doc validates the JSON Schema
before construction, every semantic identity is recomputed from canonical
bytes, and every model is deeply immutable. Unknown versions, properties,
action kinds, edge kinds, and enum values fail closed.

## Observable information boundary

ObservationV2 contains only a closed list of observable facts. A fact is a
typed subject, a whitelisted predicate, an explicit KnowledgeValueV2, and its
source: direct, remembered, or derived_visible. There is no generic debug or
metadata mapping into which privileged values can be hidden.

Knowledge has four disjoint representations:

- known carries a value and the turn at which it was observed. Integer zero is
  a normal known value.
- unknown establishes that the value cannot currently be determined.
- not_observed means the property may exist but is outside the authorized
  current or remembered observation.
- not_applicable means the property has no meaning for the subject.

The observation identity binds environment, game/ruleset identity, turn,
active and observing player, phase, observation sequence, canonical facts, and
mandatory action kinds. Consequently a cached observation is not silently
treated as current after any accepted mutation.

### Closed threat channels

| Channel | V2 closure |
|---|---|
| Undocumented adapter fields | Observation schema and fact predicates are closed; extra fields fail construction. |
| Debug endpoints and engine logs | Not present on the policy facet or in TurnContextV2; no raw response payload exists in a V2 receipt. |
| Save-file or branch parsing | Reserved for the private research/referee facet. A policy descriptor is structurally observation-only. |
| Opponent internals and intent flags | No observable predicate represents them; injection fails schema validation. |
| Complete fogged map/resources | Only projected facts may enter an observation; not_observed does not carry a value. |
| Human-invisible RNG state | Absent from every public schema and semantic receipt. |
| Alternate-branch observations | Scored receipts cannot have a parent episode; branch imports are not part of TurnContextV2. |
| Validation/error oracle | Public errors are bounded reason codes. Schema failures expose only structural path and validator keyword. |
| Secrets and provider transport | API keys, authorization headers, raw HTTP bodies, and host identifiers have no schema field. |

Equivalent player-visible worlds therefore produce byte-identical
ObservationV2 documents even when private referee state differs. The private
monitor may compare hidden ground truth for watchdog purposes, but it cannot
return that state to a policy or serialize it into policy artifacts.

## Actions and authorization

ActionIntentV2 is untrusted policy output. Its action kind and exact parameter
names must be registered, but an intent is not authority. The executor maps it
to exactly one LegalActionV2 from the current observation-bound
LegalActionSetV2. A legal action carries normalized preconditions, resource
claims, expected direct effects, affected regions, terminal/mandatory markers,
retry safety, and the observation identity on which legality was established.

AuthorizationV2 binds the intent, mapped legal action (or null on refusal),
observation, and graph. An action absent from the current graph, ambiguous,
unregistered, or bound to an earlier observation/graph is refused before the
adapter. end_turn is terminal and is authorized only after mandatory choices
are absent from a freshly compiled graph.

## Graph semantics

Stable action IDs, not enumeration position, order nodes and topological
frontiers. Symmetric edges use canonical endpoint order. Edge authority is
explicit: constraints are authoritative; COMMUTES_WITH is derived and
requires a proven_disjoint witness.

| Edge | Normative meaning |
|---|---|
| MUST_PRECEDE | Source must execute before target when both are selected. |
| REQUIRES | Target depends on the successful source and is ordered after it. |
| ENABLES | Source may make target legal later; it is never proof of current or future legality. |
| CONFLICTS_WITH | The pair cannot safely coexist in one transaction. |
| MUTEX | The pair represents exclusive choices for the same slot/entity. |
| CONSUMES_SHARED_RESOURCE | Combined observed claims exceed or contend for one budget. |
| INVALIDATES | A successful source requires target or region recomputation before use. |
| COMMUTES_WITH | Disjoint regions, resources, preconditions, and effects prove exact noninterference. |

Constraint, resource, and invalidation evidence dominates commutativity. An
unknown pair is conservatively CONFLICTS_WITH; absence of an edge is never
treated as proof of independence. Directed MUST_PRECEDE/REQUIRES cycles fail
with a deterministic action-ID witness. compile_duration_ms is emitted as a
non-identity metric; the graph semantic identity normalizes it to zero.

## Receipts and event semantics

An ActionResultV2 links its authorization, pre/post observation identities,
observable effects, verification decision, and a bounded safe reason. A
TurnReceiptV2 contains the complete authorization/result chain and all graph
identities used after recompilation. Exhausted replans and unresolved mandatory
choices terminate honestly; they do not synthesize end_turn.

An EpisodeReceiptV2 binds environment, policy/model identity, seed when
knowable, execution limits, termination reason, and content-addressed
artifacts. Scored episodes require dag_tx and cannot resume. A non-scored resume
is a new child episode carrying both parent ID and parent terminal-event hash.

Every EventV2 has:

1. a deterministic semantic hash over event type, schema reference, episode,
   turn, correlation, causation, and typed payload;
2. an event hash over previous hash, sequence, timestamp envelope, and semantic
   hash; and
3. a closed one-field payload selected by event type.

The timestamp is excluded from the semantic hash but included in the event
chain. TurnCompleted embeds its turn receipt and EpisodeTerminated embeds its
terminal receipt. Large observations, legal-action sets, graphs, proposals,
and validation outputs are referenced through ArtifactRefV2 and stored by
canonical-byte SHA-256. Event sequence plus artifact bytes is the episode trust
root; a graph database is not required in the hot path.

## Reproducibility classes

- Exact fake replay re-executes every authorized action and requires each
  observable/state verification hash and terminal receipt to match.
- Exact live replay is unavailable unless a captured environment later proves
  deterministic semantic receipts.
- Live observational comparison uses declared nondeterministic masks and is
  never relabeled exact replay.
- Audit replay validates schemas, artifacts, chain, authorization ancestry,
  and receipts without executing a game.

The contract does not embed search, MCTS/MCGS, learned policy, or provider
architecture. Those remain untrusted proposal-side implementations.
