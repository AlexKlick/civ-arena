# Graph lane — the M12 projection (Graphiti-shaped, no LLM)

M11 left the schema "shaped for" a graph. M12 makes the graph real — but
backwards from the usual direction: **the artifacts are the truth, the DB is
a derived index.** `project(records)` is a pure function of the event log;
`runs/<match_id>/graph/*.jsonl` are its deterministic output; the Neo4j
loader MERGEs those artifacts by uuid and can be dropped and rebuilt at any
time. No `graphiti-core` dependency, no LLM extraction — the claims and
observation digests on the log are already structured, so nothing needs
"understanding".

## The projection

`python -m civ_arena.graph.project runs/<match_id> [--load]` (offline and
deterministic; `--load` needs the optional `graph` dependency group and a
running DB). Internals: roster from `MATCH_START`, horizon from `MATCH_END`
(falling back to the last `TURN_END` turn for a resume-truncated log), state
from `StrategyStore.from_log`, verdicts recomputed as-of deadline — the
same `scoring` the turn header renders with, never a second opinion.

| Node | uuid | group_id |
|---|---|---|
| Claim revision | `{match}:main:claim:p{pid}:{cid}:r{rev}` | `{match}:main` |
| Entity | `{match}:main:entity:{eid}` | `{match}:main` |
| Player | `{match}:main:player:p{pid}` (agent_id, policy, civ) | `{match}:main` |
| Outcome | `{match}:main:outcome:p{pid}:{cid}:r{rev}` | `{match}:main` |
| Match | `match:{match_id}` (seed, final_turn, scores_json) | `cross_match` |
| Agent | `agent:{agent_id}` | `cross_match` |

Refinement of the M11 sketch (`strategy-lane.md`): claim nodes are
per-**revision** (`:r{rev}` suffix) so `SUPERSEDES` connects distinct nodes;
the M11 template named the claim prefix. The agent/match spine is what makes
retrieval CROSS-match: agent uuids are stable across games, so two matches
by one agent MERGE to a single node — verified live (4 roster rows → 2
`Agent` nodes across 001+002).

| Edge | rel | notes |
|---|---|---|
| Player → each revision | `AUTHORED` | one per revision, history included |
| Revision n → n−1 | `SUPERSEDES` | carries `amend_turn` |
| Prediction/lesson → claim-or-entity | `REFERENCES` | `subject_id`/`about`; resolves to the revision valid at the referencing claim's `created_turn` (see below) |
| Player → entity | `OBSERVED` | per-observer `last_seen_turn/seq` + `fields_json` snapshot |
| Claim revision → outcome | `VERDICT` | only claims due at the projection horizon |
| Agent → player / match | `PLAYED_AS` / `PLAYED_IN` | the cross-match spine |

**Reference resolution.** A reference resolves to the target claim's
revision that was authoritative when the referencing claim was WRITTEN —
the last revision whose `created_seq` precedes the referencing claim's own
(`created_seq` is the seq of the authoring TOOL_CALL, totally ordered by log
position). Seq, not turn: a lesson written after a same-turn amend must
point at the amended revision, which turn-window matching cannot express.
References naming a claim that did not exist yet are dropped and reported in
`projection.json`, never guessed. Two real defects shaped this rule, both
found on the 002 run: a verdict lesson CLOSES the prediction it is about
(`valid_to = lesson_turn − 1`), so at-turn window matching orphaned exactly
the references that embody verdicts (l4/l9 → p1/p3) — and the naive
same-turn reading pointed lessons at stale revisions.

**Verdict policy.** Outcome nodes come only from claims due at the horizon
(`scoring.due_goals`/`due_predictions` at `MATCH_END`'s final turn) — the
same sticky as-of-deadline verdicts the memory view renders. Claims resolved
another way carry that on the claim node itself: a goal closed `done` or
`dropped` keeps its status property (queryable), and a prediction closed by
its verdict lesson is visible as that lesson's `REFERENCES` edge. On 002
this means zero `VERDICT` edges — honest: the model self-assessed every
verdict through lessons, and the only metric goal still open (g9, by t50)
was past the t40 horizon.

**Edge uuids are hashes.** Node uuids keep their readable template (the
`chains` query matches by prefix); edge uuids are `e:<sha256-40>` over the
canonical `[source, rel, target]` tuple — ids from config and belief
digests are legal arbitrary strings that may contain `|`, and a pipe-join
is not injective (review round-1 P1). Every edge carries a `group_id`
(match group, `cross_match` for the spine) so the loader can reconcile one
match's graph without touching another's.

Determinism pins (tests): same log in, same bytes out — sorted by uuid,
canonical sorted-key JSONL, `ts` envelope fields never read (pinned by
mutating them and comparing projections). Lifecycle records must agree on
ONE match: concatenated or mismatched logs fail loudly, and every record is
bound to the selected `match_id` before `from_log` runs. The CLI loader
follows `EventLog._load` exactly: only the final line may be torn, `seq`
must equal position — anything else fails closed rather than project a
valid-looking truncated prefix.

## The loader and the DB

`load.py` is the only module importing the `neo4j` driver (`graph`
dependency group — the core imports and tests run without it). It reads
ARTIFACTS (never the live store) and MERGEs by uuid — but a load is more
than an upsert: it RECONCILES, atomically:

- the whole load is **one transaction** — `load_plan` validates and
  materializes every statement first (an unsafe token can never surface
  after earlier batches committed), then a single `execute_write` runs it;
- `SET n = props` **replaces** the property map (a re-loaded projection
  fully defines the node; omitted props clear) and stale labels from the
  projection's label universe are **removed** — an entity whose settled
  kind changed reconciles Unit → City. The `GraphNode` MERGE-anchor label
  is deliberately never removable: stripping it orphans nodes from future
  MERGEs and mints duplicates (a live-DB-test catch, pinned);
- match-scoped nodes and edges absent from the artifacts are **deleted**
  (scoped by the edge/node `group_id`; the `cross_match` spine is never
  touched) — a re-projected match whose outcome nodes disappeared leaves
  no residue.

Labels and relationship types are `fullmatch`-validated before cypher
interpolation. Config: `CIV_ARENA_NEO4J_URI` (default
`bolt://127.0.0.1:7687`), `CIV_ARENA_NEO4J_USER`, `CIV_ARENA_NEO4J_PASSWORD`.

`infra/neo4j/docker-compose.yaml`: `neo4j:5.15-community`, loopback-only
ports (127.0.0.1:7474/7687), named volume, `NEO4J_AUTH=none` — a
single-user dev graph with no secrets to manage; anything sensitive never
leaves the machine anyway (the artifacts contain exactly what the log
contains). Runbook:

```bash
docker compose -f infra/neo4j/docker-compose.yaml up -d
uv sync --group graph
uv run --group graph python -m civ_arena.graph.project runs/<id> --load
uv run --group graph python -m civ_arena.graph.query stats
# full rebuild from artifacts (the DB is disposable):
docker compose -f infra/neo4j/docker-compose.yaml down -v && \
docker compose -f infra/neo4j/docker-compose.yaml up -d
```

## The query surface

`python -m civ_arena.graph.query <subcommand>` — `lessons --agent`,
`goal-outcomes --agent [--metric]`, `chains --match --player --claim`,
`observed --match --player`, `matches`, `stats`. Query functions are
session-duck-typed (unit-tested without a DB); cypher is parameterized,
never interpolated.

## Live validation — 2026-08-29

Both real runs projected and loaded (001 pre-M11 → spine-only: 5 nodes;
002 → 103 nodes / 143 edges; 35 goal revisions across 9 ids, 5 prediction
revisions across 4, 20 lessons, 38 entity sightings, 27 SUPERSEDES, 14/14
references resolved, 0 dropped):

- **byte-stability**: re-projecting 002 twice → `diff` clean on
  nodes/edges artifacts;
- **idempotence**: re-loading 002 → node/rel counts identical;
- **cross-match join**: 001+002 share the same agent pair — 4 roster rows
  MERGE to 2 `Agent` nodes, `PLAYED_AS` ×4;
- **restart survival**: `docker compose restart` → 106 nodes / 147 rels
  unchanged (named volume);
- **disaster rebuild**: `down -v` + `up` + re-load → identical 106/147 —
  the derived-index claim proven end-to-end;
- **cross-match queries**: `matches` shows the M11 headline in two lines
  (same seed: baseline 2 cities/86 gold vs memory 4/509); `goal-outcomes
  --agent minimax-m3` tallies {done 6, dropped 2, active 1}; `chains
  --claim g1` walks all 11 revisions; `observed --player 0` lists the
  enemy city and respawned defenders last-known at t40/t39;
- gate: full suite **301 passed + 1 skipped** (the live DB round-trip,
  opt-in via `CIV_ARENA_NEO4J_TEST=1` with the container up — normal gates
  never require a running DB; it passed 42/42 in the live run), ruff clean.

## Review — Codex round 1 (gpt-5.6-sol), 8 findings → all fixed and pinned

3×P1: pipe-bearing legal ids collided edge-join uuids (now hashed); the CLI
loader silently truncated mid-file-corrupted logs (now the fail-closed
`EventLog._load` discipline); same-turn amendment resolution (self-found
mid-review at `7014c78`, its amended-prediction variant pinned in the
round). 4×P2: reload residue (now full reconciliation — replace props,
remove stale labels, delete group-scoped absentees), load-order-dependent
Agent policy (now on the match-scoped edge), non-atomic multi-transaction
loads (now one prevalidated transaction), concatenated-log envelope mixing
(now lifecycle consistency + per-record match binding). 1×P3: `$` before a
trailing newline in token validation (now `fullmatch`). Plus one the
reviewer could not catch from code: `LABEL_UNIVERSE` included the
`GraphNode` MERGE anchor, so every node lost its anchor label and later
MERGEs minted duplicates — the live DB test caught it, which is exactly why
that test exists despite being opt-in.

Every reproduction was verified by execution before fixing; every fix is
pinned. The real graph was rebuilt from artifacts after the round (same
106 nodes / 147 rels — the fixes changed uuid formats and DB semantics, not
the projected content).

## Review — Codex round 2 (gpt-5.6-sol), 6 findings → all fixed and pinned

The M11 lesson repeats: every finding targeted round-1 fix code. 2×P1: a
match_id containing `:` could mint a match-scoped uuid identical to another
match's spine uuid — reconciliation would then delete spine data (now
match_id and roster agent_ids are charset-validated
`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`, making the collision structurally
impossible while keeping the readable templates); pre-rework pipe-uuid
edges with no `group_id` were invisible to reconciliation and silently
duplicated on upgrade (every load now sweeps them first — no-op on current
DBs). 2×P2: the fallback horizon was computed before match binding, so a
foreign `TURN_END` could inflate it and mint premature outcomes (the
fallback now runs over match-bound records); a `subject_id` naming a
not-yet-existing claim fell through to a same-id entity and misbound
silently (claim-namespace hits with no authoritative revision drop and
report). 2×P3: the injectivity wording (SHA-256 prefixes are
collision-resistant, and a collision still surfaces as the loud
duplicate-uuid error); the live test's hand-built fixtures (it now
generates them through `project()`, asserts scoped cardinality and zero
duplicate uuids DB-wide, and seeds a legacy edge to pin the migration).

Gate after the round: **304 passed + 1 skipped** (45/45 with the live
leg), ruff clean; the real 002 graph reprojected and reloaded to an
identical 106 nodes / 147 rels, zero duplicates, zero legacy edges.

## Review — Codex round 3 (gpt-5.6-sol), 3 findings → fixed and pinned

Trajectory 8 → 6 → 3, no P1s left. P2: the legacy pipe-uuid sweep was
global — upgrading one match would strip another match's legacy
relationships without recreating them; the sweep is now scoped to the
legacy uuids reconstructed from this load's own (source, rel, target)
tuples. P2 (reviewer-marked theoretical): an id existing in BOTH the
claim and entity namespaces can never be resolved from the bare string —
policy is now never-guess (drop with an explicit `ambiguous claim/entity
id` report line); real logs cannot produce an overlap (claim ids are
g/p/l+digits, sim entity ids u/c+digits) and the 002 run reprojects
identically, 14/14 references resolved. P3: the live test's zero-duplicate
claim now covers relationship uuids too, the charset 64/65 boundary is
pinned, and the seeded legacy edge mirrors what a pre-rework load actually
wrote. Gate: **306 passed + 1 skipped** (47/47 with the live leg), ruff
clean.

## Review — Codex round 4 (gpt-5.6-sol), 3 findings → fixed and pinned

P2: the round-3 scoped sweep reconstructed pipe uuids from the CURRENT
artifacts — it missed ghosts (legacy edges whose triples no longer exist)
and pipe-uuid non-injectivity meant a reconstructed uuid could equal
another match's legacy edge. Both dissolve with ENDPOINT scoping: delete
pipe-uuid edges touching this match's nodes — endpoints cannot lie; the
live test seeds a real-edge duplicate and a ghost, both swept. P2: lesson
`about` is validated at write time as an own claim id — the round-3
ambiguity rule wrongly dropped it when a same-id entity existed, and the
entity fallthrough could bind it; validated claim references bypass both
paths. Gate: **307 passed + 1 skipped** (48/48 with the live leg), ruff
clean; 002 reprojects and reloads identically.

## Review — Codex round 5: CONVERGED

Trajectory **8 → 6 → 3 → 3 → 0** over five adversarial rounds (the M11
shape exactly). Round 5 probed the endpoint sweep's historical edge cases
(renamed/deleted endpoints, PLAYED_IN through the match node), the
`validated_claim` threading, and test vacuousness (parent-logic
substitution fails the new pins) — no real defect on any supported input;
the only theoretical notes concern malformed direct API inputs that the
canonical `load_records` already rejects. Local gates are the authority:
**307 passed + 1 skipped** (48/48 with the live leg), ruff clean.

## M13 (deferred, decided with the operator)

The in-game loop: a `recall_lessons` non-action tool feeding prior-match
lessons back into prompts — riding the M11 `observed`-digest trick (a
`recalled` digest on the TOOL_RESULT keeps model-free replay deterministic).
Deferred because the graph holds ONE claim-bearing match today; a recall
tool over a near-empty graph buys tokens for nothing. Gate for M13: ≥3
claim-bearing matches, which the arena now produces cheaply (any LLM match
with the M11 tools).
