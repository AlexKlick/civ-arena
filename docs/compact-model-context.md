# Compact model observations

The fresh 100-round run `minimax100-20260906T181834Z` stopped on player 1,
turn 28 after 27 complete rounds and 55 completed seat turns. Its terminal
reason was `critical owned state and nearby terrain exceed context budget`.
There were no watchdog violations, and cleanup completed. This is a failed
100-round attempt, with the active lease and cached final observation retained
in its summary. It does not establish 100-round reliability.

The retained final unit read contains one Sumerian warrior and four scouts,
with another scout queued. The context fixture reconstructs the final reads
through the parser and player projection. It includes the last currently
derived-visible terrain, not the adapter's earlier remembered-tile history.
Even this subset reproduces the original size failure.

`ContextCurator` now uses a compact representation when required state would
otherwise exceed its budget. The configured 8,000-character whole-request cap,
provider settings, model cadence and game-action limits remain unchanged.

1. Terrain rows share explicit column names and a native-terrain palette.
   Coordinates, normalized movement class, native metadata, observed ownership,
   and city identity are retained. Only retained rows contribute palette entries.
2. If that is insufficient, actor tables share column names too. Every owned
   unit and city remains present. Groups containing explicit null values stay
   as objects, preserving the distinction between null and an absent field.
   Terrain with explicit-null metadata or ownership also remains as objects.
3. Every required adjacent terrain tile remains included. Optional farther
   terrain still has an explicit omitted count. An unrepresentable critical
   state still aborts before a provider call.

The packet describes its column order and null semantics. In compact terrain,
`-1` ownership and an empty city string retain their observed meanings; null
means the field was unavailable. Evidence readers can expand the representation
with `terrain_rows` and `entity_rows` from `agents/llm/terrain_context.py`.
Controller planning continues using its unchanged full projected observations;
the compact tables are a serialization format for the model request only.

An optional source-classified `is_barbarian` observation is retained in model
actor rows. Its absence is unknown, and ordinary foreign units are not thereby
classified as hostile. Native collection/projection is a separately tested
change.

Honest hp note (M4, 2026-09-08): the parser no longer synthesizes the
legacy `hp: 100` placeholder on city rows — the CITIES\|2 extended read
carries the engine's real `hp`/`max_hp` when it answers, and an unread hp
is ABSENT, never a fabricated 100 (the planner's belief layer documents
its 100 default as a prior, not an observation). A city row without `hp`
therefore means "hp not observed", not "healthy".

Focused verification: 31 tests passed, zero failed/skipped; Ruff passed. Tests
cover the retained failure, whole-request metadata accounting, exact expansion,
absence/false/zero/null distinctions, all adjacent tiles for a dispersed
16-unit roster, and failure when critical observations still cannot fit.
Logs are retained under `runs/100-round-live-20260906T181834Z/compact-context-*`.
This proves repository behavior, not model comprehension or a successful live
100-round run. The first Ruff invocation reported one import-order issue;
the corrected invocation passed.

Independent review of `418cfa2` found that explicit-null terrain ownership
collapsed into absent fields. The correction preserves those terrain objects
and adds native-metadata/null ownership coverage, including the actual curator
render path under a 7,000-character budget. The original NO-GO and subsequent
review results are preserved separately; the earlier implementation is not
claimed as lossless for that corner case.
