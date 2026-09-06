# Offline diplomacy protocol foundation

Implemented in the isolated diplomacy branch, 2026-09-06. The design and source
map are in [the protocol proposal](diplomacy-protocol-next-step.md). This module
is not imported by the live driver, controller, facade, provider, or dashboard.
It does not alter the match currently running on the reviewed reliability branch.

[`DiplomacyStore`](../src/civ_arena/diplomacy/protocol.py) is a pure in-memory
foundation for exact, voluntary agreements. It accepts strict JSON messages,
offers, counteroffers, acceptance, rejection, and withdrawal. Messages and
proposed terms are explicitly attributed; no engine observation, automatic
compliance verdict, reputation score, or mechanical enforcement is fabricated.

## Implemented contract

A trusted owner constructs `ServerContext(match_id, author, turn, lease_id)` and
calls `begin_turn` before applying commands. The model's JSON cannot supply an
author, player ID, lease, signature, or verdict. The reducer validates context
shape, match, roster, expected turn, and the consistent lease label. **It cannot
certify an actual engine lease**: that remains the future facade's responsibility.
Likewise `advance_round([[turn, seat0], [turn, seat1]])` checks the complete ordered
configured roster and preceding `begin_turn` calls, but the caller must establish
actual completed/released engine turns. Supplying an integer pair is not proof of
a live seat turn.

The raw JSON entrypoint rejects duplicate keys, nonfinite/floating-point values,
unknown fields, boolean identity aliases, invalid Unicode, excessive size/depth,
and unsupported terms. Python-dict callers must already have rejected duplicate
keys at their transport decoder; that information is lost after ordinary parsing.
The protocol version is integer 1 and enforcement is exactly `voluntary`.

An offer's SHA-256 binds canonical protocol version, match, complete parties,
canonical private audience, expiry, and exact terms. Both parties must consent to
the same revision and digest. A counteroffer replaces the current open revision
and clears prior consent; immutable old revisions survive in protocol records and
recipient notices. The proposer consents when proposing. The other party must
receive the offer at its own lease boundary before accepting. Acceptance creates
`agreed`; the next completed-round boundary activates the treaty for its resolved
future interval. `elapsed` records the interval's end, not fulfillment. Every
agreement keeps `compliance: "unassessed"` in this foundation.

The current term shapes are `refrain_from_attack`, `send_report`, and explicitly
unadjudicated `statement`, each with `condition: {"kind":"always"}`. Report terms
carry exact topic, destination, and relative deadline, but no evaluator yet
matches messages to fulfillment. Conditional dependency predicates, observations,
breaches, reputation, termination, disclosure, multilateral treaties, and
mechanical enforcement remain unimplemented. Their unsupported tags reject.

New private/public messages are retained immediately and delivered once at the
recipient's next own `begin_turn`. Calling it again in the same lease does not
refresh the inbox. Local results and an author's own notices are immediately
visible to that author. A public message targets all other configured seats;
a private thread targets one. Offers have exactly two parties. The constructor
accepts 2–8 configured seats for offline privacy fixtures and future channels;
the current live game still has two seats.

`project(player)` returns deep copies containing only that player's delivered
messages and offer notices. It omits global event sequences, global hashes,
other threads, and other readers' counters. IDs and capacity are channel-local,
so activity in an unrelated private thread cannot shift a public message's ID
or cause an unrelated channel's offer-capacity rejection. The operator-only
`records()` and `state_digest()` are never player projections. These are Python
APIs, not an authenticated browser endpoint.

Bounds are fixed module constants for this first slice: 4 new operations and
2 new offers/counteroffers per own turn; 8 open offers and 8 agreed/active treaties
per private channel; 8 obligations per treaty; 600 message characters; 1,600
characters for the canonical consent document; 4,096 characters for a canonical
command; 12 levels/256 JSON nodes; 10,000 protocol records. Projection retains the
latest 16 delivered messages plus an authorized omission count. Offer history is
bounded by the protocol-record limit; future context integration still needs a
separate budgeted selection of active/due terms. These constants do not modify
provider request limits, token limits, game deadlines, or movement allowance.

Identical command-key retry returns the original receipt without new state,
record, or cost; conflicting reuse rejects. The key is scoped to author and match
and survives own-turn changes in the offline store. Rejected operations mutate
nothing. Accepted operations are applied to a draft and committed atomically in
memory. Returned commands, records, views, and receipts cannot alias internal state.

`from_records` re-executes complete owner inputs without a model and compares
every resulting record using typed canonical JSON, including delivery results,
consent documents, and command results. It rejects sequence errors, duplicate
records, wrong contexts, changed results, and boolean/int aliases. Prefix rebuild
reconstructs only that prefix. This does not authenticate a wholly rewritten
journal; custody and durable storage remain the event log owner's responsibility.
The state digest belongs to the protocol plane, not Civ VI or simulator state.

## Verified findings

The isolated behavioral suite reports **59 passed, 0 failed, 0 skipped** in
`/tmp/civ-diplomacy-d1-pytest-3.log`; the targeted Ruff log is
`/tmp/civ-diplomacy-d1-ruff-2.log` (`All checks passed!`). Reproduction:

```bash
PYTHONPATH=src /home/alexk/documents/civ-arena/.venv/bin/python -m pytest -q tests/test_diplomacy_protocol.py > /tmp/civ-diplomacy-d1-pytest.log 2>&1
/home/alexk/documents/civ-arena/.venv/bin/python -m ruff check src/civ_arena/diplomacy tests/test_diplomacy_protocol.py > /tmp/civ-diplomacy-d1-ruff.log 2>&1
```

Coverage includes exact consent, stale revision, expiry/activation boundaries,
skipped/duplicated seats, private projection and delivery timing, spoofed authors,
unknown term capabilities, bounded payload/cost/history, replay tampering, and
alias isolation. An initial test run exposed replay's Python bool/int equality
alias: **52 passed, 1 failed**. Canonical typed comparison fixed it; the next run
reported **53 passed**, and additional bounds/capacity cases produced the final
59. These are offline source tests, not live negotiation acceptance.

## Follow-up probes

A read-only review should check this exact implementation before integration.
D2 needs an event-log persistence adapter with prepare/persist/apply/ack semantics,
server-owned lease validation, accepted call/result pairing, result digest
comparison in existing replay, and crash/torn-record tests. The in-memory store
does not itself implement fsync or claim durable acceptance. A failed durable
append must not leave an acknowledged or continuing protocol state.

D3 needs actual strategic-directive schema support, private curated inboxes under
the existing total context budget, bounded decision triggers, and browser
projections with separate operator/player authority. D4 needs actual model
exchanges and a fresh live reliability sequence after integration.

## Blocked checks

No host or provider dependency blocked this offline slice. Engine, provider,
browser, and actual unattended-match checks are outside it and were not run.
The full repository release gate is deferred until integration is authorized
against a settled reviewed implementation.

## Evidence gaps

No current model has received a diplomatic message from this module. No actual
treaty was negotiated or enforced in Civ VI. No private browser endpoint,
player-visible compliance evaluator, calibrated reputation, durable protocol
adapter, strategic context integration, or live replay proof is provided here.
