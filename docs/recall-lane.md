# Recall lane — M13: the in-game cross-match memory loop

M12 left the graph and a gate: ≥3 claim-bearing matches. The seeding runs
(003/004) opened it with a replication bonus — 3/3 memory-plane wins on
fresh seeds — and M13 closes the original architecture's loop: **prior
matches' lessons now flow back into live play.**

## The design in one paragraph

`recall_lessons(query)` is the 20th agent tool: a validated non-action in
the `get_strategy` shape. The corpus is the agent's OWN lessons from prior
runs' event logs — read straight from the logs at match construction (the
trust root resume/replay use; the Neo4j graph stays the human query
surface, so a match never depends on a container or a projection step).
Retrieval is deterministic lexical scoring (term-set overlap, tie-break by
match/turn/lesson, top-5, zero-score never returned) — no embeddings, no
LLM. The result rides an additive `recalled` digest on the TOOL_RESULT:
the log carries exactly what the model was fed.

## The contract details that cost review rounds

- **`recalled` is replay-COMPARED, unlike `observed`.** Observations
  re-derive from replayed game state — identical by construction — but a
  recall re-queries an EXTERNAL corpus. `_strip` therefore includes the
  payload: a changed/mutated prior corpus breaks replay loudly instead of
  silently certifying a run that would have fed different lessons.
  Pre-M13 logs (field absent) still compare equal.
- **Corpus members are finished, identity-matched, well-formed.** Every
  record binds to the requested `match_id` (concatenated logs cannot
  relabel foreign lessons); exactly one `MATCH_START` and one TERMINAL
  `MATCH_END` required (a run still being written can never feed a
  crash-resume rebuild); rosters fail closed on any entry not exactly
  (str, non-bool int, str) and unique on both axes (no cross-attribution).
- **The log-vs-feed equality holds for ANY lesson content.** The referee
  bounds the digest's SERIALIZED length (≤3900, measured with the runtime's
  own serializer — JSON escaping can sextuple non-ASCII) by dropping WHOLE
  lessons; config enforces `max_result_chars ≥ 4000` when `recall_runs` is
  set. The envelope over the digest is a constant 62 chars: full doc ≤
  3962 < 4000, provably untruncated.
- **Config**: `match.recall_runs: [match_id, ...]` — charset-validated
  bare ids (path aliases rejected), self always excluded, absent/empty ⇒
  the tool answers an honest `tool_unavailable` rejection. Replay passes
  the SOURCE run's root as `recall_root`, so a custom `--replay-dir`
  cannot move or shadow the corpus.

Review: 3 Codex rounds (gpt-5.6-sol), 6 → 3 → 0, every repro verified by
execution before fixing; gates 331 passed + 1 skipped, ruff clean at
`203473e`.

## Live validation — 2026-08-29 (005, seed 571903)

Corpus: 002+003+004 (66 lessons). Smoke first (2 turns: 3 recalls, 0
violations, replay OK), then the paid 40-turn run:

| match | memory | ROME (LLM) | result |
|---|---|---|---|
| 001 | none | 2 / 13 / 86 / 24 | draw |
| 002 | M11 | 4 / 21 / 509 / 24 | win |
| 003 | M11 | 3 / 15 / 248 / 21 | win |
| 004 | M11 | 4 / 20 / 14 / 33 | win |
| **005** | **M11 + recall** | **6 / 25 / 414 / 28** | **strongest win** |

0 violations; REPLAY OK 3,122 events — including 44 recall calls whose
digests compared equal (the corpus reproduced bit-for-bit). Tokens 647k
in / 172k out (the recall overhead: ~+18% input vs 002 — 44 pulls plus
larger contexts).

**Adoption and steering.** The model recalled 44 times — four of them in
turn 1 ("early game strategy opening", "combat archers warriors defense",
"**hex neighbor coordinates movement**", "research order early tech") —
and wrote 26 new lessons. Queries tracked the game's phases (settler
escorts, third-city placement, Korea scouting, siege). All three prior
matches served (002 ×124 servings, 003 ×59, 004 ×31). The signature
transfer: 002's axial-neighborhood lesson — knowledge that cost the 002
model **21 turns** to discover — was served at t10/t11/t28 of this run.
The empire outcome (6 cities, the largest of any run) is one seed's
evidence, n=1 for recall; the mechanism, determinism, and honesty of the
record are what this milestone pins.

Honest caveats: no ablation (a 005-shaped run WITHOUT recall on the same
seed would isolate the effect — the corpus knob makes that a config away);
lexical retrieval means the model's query vocabulary gates what it finds;
and 26 new lessons per match compounds the corpus faster than its quality
is verified.

## What's next (not scheduled)

The arena now has the full memory stack: diary → typed claims (M11) →
cross-match graph (M12) → in-game recall (M13). Natural next rungs, none
committed: an ablation pair on fresh seeds; recall quality scoring (did
the served lesson's match outcome corroborate it); exporting the loop to
a second LLM agent (the corpus is own-agent-scoped, so a different model
starts cold — by design).
