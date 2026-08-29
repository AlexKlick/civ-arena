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

Determinism pins (tests): same log in, same bytes out — sorted by uuid,
canonical sorted-key JSONL, `ts` envelope fields never read (pinned by
mutating them and comparing projections).

## The loader and the DB

`load.py` is the only module importing the `neo4j` driver (`graph`
dependency group — the core imports and tests run without it). It reads
ARTIFACTS (never the live store), MERGEs by uuid — one UNWIND query per
label-set/rel-type, batched 500, nodes before edges so endpoint MERGEs
never mint bare stubs. Labels and relationship types are regex-validated
before cypher interpolation. `SET n += props` never clears stale props, so
re-loading changed artifacts is additive; after a log legitimately changes,
drop and rebuild (below). Config: `CIV_ARENA_NEO4J_URI` (default
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
- gate: full suite **289 passed + 1 skipped** (the live DB round-trip,
  opt-in via `CIV_ARENA_NEO4J_TEST=1` with the container up — normal gates
  never require a running DB; it passed 30/30 in the live run), ruff clean.

## M13 (deferred, decided with the operator)

The in-game loop: a `recall_lessons` non-action tool feeding prior-match
lessons back into prompts — riding the M11 `observed`-digest trick (a
`recalled` digest on the TOOL_RESULT keeps model-free replay deterministic).
Deferred because the graph holds ONE claim-bearing match today; a recall
tool over a near-empty graph buys tokens for nothing. Gate for M13: ≥3
claim-bearing matches, which the arena now produces cheaply (any LLM match
with the M11 tools).
