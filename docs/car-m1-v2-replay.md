# CAR-M1 V2 ledger verification and exact fake replay

Status: implemented by CAR-103/CAR-106 (`a5ea3c2`, `b443673`)

`events.jsonl` is the only episode trust root. Every line is canonical JSON,
schema 2, sequence-bound, semantically hashed, and chained to the preceding
event. A V2 tail that is incomplete is evidence of a recovered crash, not an
invitation to repair the original file: the reader refuses it and a resumed
run must create a child episode bound to the parent's terminal event hash.

## Artifact custody and terminal closure

Observations, legal-action sets, graphs, proposals, and validation receipts are
stored as canonical content-addressed objects below
`objects/sha256/<prefix>/<digest>.json`. The verifier checks the referenced
byte length, digest, canonical encoding, and declared JSON Schema before using
an object. Two different references for one digest are refused.

The first event must be the unique `EpisodeStarted`; an
`EpisodeTerminated` event, when present, must be unique and final. Its embedded
`EpisodeReceiptV2` is bound to that terminal event hash, repeats the exact
environment descriptor used at episode start, and lists exactly the artifact
references reachable from the event log. Deletion, insertion, reordering,
payload alteration, object alteration, and object deletion therefore fail
before replay.

The single-writer lock is nonblocking. V2 never opens an existing episode for
append and never truncates it. Non-scored recovery creates a new child episode;
scored episodes prohibit recovery through resume.

## Exact deterministic fake replay

`replay_fake_episode_v2()` accepts only a terminal episode whose descriptor
declares the deterministic fake adapter and whose recorded descriptor exactly
matches the implementation available to the replay process. It resets a fresh
fake environment with the recorded seed, then walks the event state machine:

1. re-create each player-scoped observation;
2. re-enumerate the complete legal-action set;
3. recompile the complete action graph;
4. bind each authorization to its recorded proposal, observation, graph, and
   unique current legal action;
5. execute only that authorized registered action;
6. compare the recorded action result and the fresh post-mutation observation;
7. require postcondition and graph-invalidation closure before another
   authorization or graph; and
8. compare the terminal turn receipt with the exact graph, authorization, and
   result histories.

A completed turn must end with an accepted or duplicate `end_turn`, with the
fresh post-observation no longer in the active-turn phase. Successful episodes
cannot conceal a failed or incomplete turn.

The persisted equivalence boundary is deliberately observable: replay compares
every fresh player-scoped `ObservationV2` identity and all derived public
artifacts. The private referee state hash is computed only in process and
returned as a diagnostic; it is neither an event payload nor an object. This
keeps watchdog ground truth out of policy, graph, receipt, error, and replay
artifacts while still allowing a test harness to prove deterministic fake
execution.

## Frozen V1 compatibility

Schema-1 logs remain readable and replayable through
`load_v1_events_read_only()`. The compatibility surface has no writer, repair,
truncate, resume, or migration operation. It accepts the historical event-kind
set, checks contiguous sequence numbers, reports an incomplete final record,
and fails closed on corruption anywhere earlier in the file. V1 evidence can
therefore still be replayed by the historical replay command, but a V1 episode
cannot be resumed through the V2 runtime or opened by the V2 writer.

Exact replay here is fake-engine proof only. A FireTuner episode can receive
the same structural ledger verification, but deterministic live replay is not
claimed; live evidence remains a separate CAR-109/CAR-110 gate.
