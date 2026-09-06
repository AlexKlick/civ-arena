# Model diplomacy and voluntary treaties

Design proposal, 2026-09-06. Source binding: `b1af7086c5b5046bc46e22cf2ad5cb2b64e14491`,
tree `35bf8e1a92ac4df401911d5b72eb537f970b740c`. This document adds no runtime
behavior and does not change the running 60-round match. The first implementation
slice is an offline, deterministic protocol and evidence projection. The subsequent
[D1 foundation](diplomacy-offline-protocol.md) records its implemented subset. Live engine,
provider, browser, and full-match acceptance remain separate gates.

The models should negotiate outside Civ VI's limited diplomacy interface: exchange
messages, coordinate plans, offer precise commitments, counteroffer, cooperate,
and choose whether to honor promises. Agreements are voluntary. A breach can
change another model's beliefs and future willingness to cooperate; it does not
make an otherwise legal game action illegal. Mixed or mechanically enforced terms
are a future, explicit match rule, never inferred from a model's request.

This channel is communication between the configured game agents. It does not send
email, chat, or any other message to a person or external service. The operator can
watch negotiations in the local browser.

## Existing affordances and gaps

| Surface at the bound commit | Reuse | Gap to close |
|---|---|---|
| [`PlayerSession`](../src/civ_arena/session/player_session.py), [`SessionCtx` and `ToolFacade`](../src/civ_arena/session/tools.py) | Server binds author identity and current lease. | No diplomatic commands or inbox. A recipient is an explicit destination, never a caller-supplied author. |
| [`Referee._emit_pair`](../src/civ_arena/arena/referee.py) and diary/strategy tools | Validated non-actions emit `TOOL_CALL`/`TOOL_RESULT` without game mutations. | Add protocol results, membership checks, and durable reducer application. Existing non-action tools are not themselves a negotiation bus. |
| [`EventLog`](../src/civ_arena/arena/events.py) | Fsync, contiguous `seq`, match/seat/agent identity, one authority. | Visibility labels alone do not enforce readership. Replay needs a diplomacy-specific comparable result. |
| [`VisibilityPolicy`](../src/civ_arena/arena/visibility.py) | Player-scoped observations, closed foreign-field allowlists. | `bilateral` currently returns `{"status":"stub","participants":[]}`. A treaty needs an explicit reader set and recipient projection. |
| [`StrategyStore.from_log`](../src/civ_arena/strategy/store.py) | Rebuild accepted, namespace-matched claims from a retained log prefix. | Existing goals/predictions are unilateral; there is no multilateral consent or contract lifecycle. |
| [`strategy.scoring`](../src/civ_arena/strategy/scoring.py) | Distinguishes claims and observation-derived facts. | Its own-metric `>= target` scoring is insufficient for promises, conditional obligations, negative obligations, or hidden adversary conduct. |
| [`ContextCurator`](../src/civ_arena/agents/llm/context_curator.py), [`StrategicController`](../src/civ_arena/agents/llm/strategic_controller.py) | Gather context once; JSON directive on cadence/triggers; quiet-turn autopilot. | Project inbox and active terms into the same budget, add a bounded negotiation trigger, validate a new directive version. |
| [`dashboard.py`](../src/civ_arena/dashboard.py) | Local operator view, derived from retained artifacts. | Current `/api/run` projects both seats and has no player authentication. It must not be supplied as an agent inbox or called a private-player endpoint. |
| [`replay.py`](../src/civ_arena/replay.py) | Recorded tool execution without a model; live mode explicitly excludes engine-state hash equality. | New tools need replay argument support and comparison of protocol/delivery results, not just tool names and statuses. |

## Authority and state ownership

Add a pure `DiplomacyStore` and reducer under `src/civ_arena/diplomacy/`. It accepts
validated commands plus a server-owned context, and returns protocol changes and
receipts. It imports no adapter, provider client, desktop controller, or network
library. Engine actions continue through the existing facade/referee. A treaty
never authorizes foreign-unit control, resource creation, a hidden observation,
extra movement, or a mutation outside normal engine receipts.

The event log remains authoritative. Use existing `TOOL_CALL` and `TOOL_RESULT`
kinds for diplomatic non-actions and `HEARTBEAT(audit="diplomacy_boundary")` for
server-derived round transitions. Include `protocol_version`, a result digest,
and the explicit readers of each protocol object. The envelope's `player_id`
remains the author; a private pair is not represented by overwriting that identity.
Do not put diplomatic text into engine mutation receipts or change engine hashes.

Validate first, persist the accepted result, then apply its deterministic reducer
change before acknowledging success. If durable append fails, do not acknowledge
or continue using the proposed state. A crash between persistence and application
rebuilds from the accepted pair. An unpaired call changes nothing. Log-prefix
rebuild must verify complete call/result namespace and argument/result bindings,
not merely adjacency or `status=accepted`.

Diplomacy has its own canonical, integer-only state digest. No wall-clock expiry,
random negotiation IDs, floating-point confidence, or external knowledge enters
that digest. Operational request/deadline clocks stay in the existing envelope.
A future checkpoint may bind the diplomacy prefix/digest, but that is a separate
contract from the engine snapshot. First integration remains fresh-only, matching
the current strategic controller; do not claim resume support from a pure reducer
test alone.

## First-slice protocol

The first slice supports two-party private threads and public messages to all
configured model seats. Public roster identity is already available; arena
communication does not imply the civilizations have met on the map. Contact-gated
communication, if desired later, must be an explicit match setting with a proved
contact observation. City states and unconfigured seats are not message targets.

One agent-facing `diplomacy` non-action accepts a tagged JSON operation:
`message`, `offer`, `counteroffer`, `accept`, `reject`, or `withdraw`. The session
supplies author, match, turn, and lease. An opaque command key makes retry of an
identical operation return its original receipt; reuse with different bytes is
rejected. No caller-supplied signature, author, event sequence, or compliance
verdict can grant authority.

- A message contains audience, bounded literal text, and optional references to
  offers or claims that the author is entitled to see. Text is an attributed
  statement, not an instruction to the recipient's controller.
- An offer contains an exact participant set, audience, versioned terms, offer
  expiry, and `enforcement: "voluntary"`. The proposer consents to that exact
  version by proposing it. No obligation applies before the other party accepts.
- A counteroffer names the current offer ID/version/hash, creates a new immutable
  revision, and supersedes the old open revision. It grants the counterproposer's
  consent only. Prior acceptance never transfers to new terms.
- Acceptance names the exact offer ID/version/terms hash. It rejects stale,
  expired, superseded, withdrawn, wrong-audience, or nonparticipant requests.
- Rejection and withdrawal close an open offer. An active treaty cannot be erased
  with `withdraw`. Its terms must specify prospective termination rights; without
  them a repudiation is a recorded statement and obligations remain historically
  assessable. The first slice can omit early termination entirely.

Example model operation, assuming server-bound author is seat 0 and the last
completed round is 9:

```json
{
  "op": "offer",
  "command_key": "border-plan-1",
  "audience": {"kind": "private", "recipients": [1]},
  "participants": [0, 1],
  "expires_after_round": 12,
  "terms": {
    "version": 1,
    "enforcement": "voluntary",
    "activation": "next_complete_round_boundary",
    "duration_rounds": 5,
    "obligations": [
      {
        "id": "peace-0",
        "actor": 0,
        "kind": "refrain_from_attack",
        "beneficiary": 1,
        "condition": {"kind": "always"},
        "evidence_policy": "recipient_visible_only"
      },
      {
        "id": "peace-1",
        "actor": 1,
        "kind": "refrain_from_attack",
        "beneficiary": 0,
        "condition": {"kind": "always"},
        "evidence_policy": "recipient_visible_only"
      },
      {
        "id": "report-0",
        "actor": 0,
        "kind": "send_report",
        "topic": "scouting-east",
        "to": [1],
        "due_after_activation_rounds": 2,
        "condition": {"kind": "always"},
        "evidence_policy": "protocol_delivery"
      }
    ]
  }
}
```

The response supplies an opaque `offer_id`, revision 1, and a SHA-256 over the
canonical full consent document: protocol version, match ID, participants,
audience, expiry, and terms. The
field may be named `terms_hash`, but it must bind all those fields. Canonicalization
rejects duplicate keys, unknown fields, booleans in integer positions, nonfinite
numbers, overlong text, and ambiguous identifiers. Use exact UTF-8 bytes and the
repo's integer-only canonical rules; do not normalize signed text after consent.
An accept operation carries that returned hash verbatim; an example need not invent
a hash that was never calculated.

`send_report` means delivery of a message tagged with that topic by the deadline.
It proves delivery, not that the report is true, useful, complete, or strategically
honest. Bluffing and misleading claims remain attributable model speech. The
system must never manufacture an observation or label an unverified report as
engine evidence.

Keep the first executable term language small: `send_report`,
`refrain_from_attack`, and conditions `always` or `after_protocol_receipt` referring
to an earlier, visible obligation receipt. `refrain_from_attack` may stay unknown
under fog. Coordination such as scouting regions or joint battle timing may be
negotiated as explicitly unadjudicated `statement` terms until a checked evaluator
exists. Reject an unsupported machine-evaluable predicate instead of silently
coercing it to prose. No arbitrary Python, Lua, SQL, regex, or free-form evaluator.

A later typed expression tree can add `all`, `any`, comparisons, observed position
regions, resource transfer receipts, and synchronized action windows. It needs
bounded depth/nodes, cycle rejection, three-valued condition logic, and capability
binding for each predicate. An unknown prerequisite does not become false or
satisfied. Future non-aggression, territorial, intelligence, trade, defense, and
coalition templates should compile to that same term language, not separate
special cases with inconsistent consent rules.

## Time, consent, and delivery

`seq` orders commands. The round clock advances only after both expected seats
complete and release the same engine turn in order. An engine turn counter alone
must not expire an offer or satisfy a duration. Acceptance commits consent when
its durable receipt is written. The treaty activates at the next completed-round
boundary after all consents, before either seat acts in the following round. This
prevents an agreement accepted by seat 1 from retroactively governing seat 0's
already completed actions.

Define an active interval as the next five complete rounds for `duration_rounds=5`;
record its resolved inclusive first/last engine-turn pair at activation. `due_after_
activation_rounds=2` means the end of the second such complete round. Offer expiry
runs after processing that boundary and is exclusive of future seat turns. Offers
must leave at least two future complete rounds for consideration. Deadline tests
must bind these boundary definitions, including a partially completed round.

Persist message acceptance immediately; expose new messages to a recipient at the
start of its next own lease. The per-seat inbox snapshot remains fixed during that
seat's decision. Sequential hotseat means a seat 0 message can reach seat 1 in the
same engine turn, while a seat 1 reply reaches seat 0 next turn. Record accepted,
delivered, and model-context-included as distinct facts. Delivery is never consent;
silence is never acceptance. No recursive model-to-model call loop runs between
engine turns.

## Secrecy, claims, and compliance

The operator can see all retained protocol records on the local machine. Agent
privacy comes from projection, not encryption or secrecy from the operator. The
raw event log and current operator `/api/run` response never become model input.

Every projected inbox, treaty, claim, receipt, and graph query checks membership
using the server-bound recipient. Unknown and unauthorized IDs have the same
response shape. Public messages are visible to the configured audience; private
messages are visible only to author and named recipient. Replies cannot broaden
the original thread audience implicitly. Publicizing private statements is a new,
attributed disclosure operation with an explicit policy, not an automatic view
change. First slice does not expose such an operation.

Do not leak hidden participants, message counts, global sequence gaps, timestamps,
entity IDs, or hash-addressable objects through a private-player endpoint. Return
recipient-local cursors and object IDs; retain global sequence references only in
operator evidence. Terms disclosed to a recipient can refer to coordinates as
claims, but the controller does not mark those tiles explored or units observed.
Escaped, length-bounded literal text is untrusted data in model context and DOM;
it cannot create tool definitions, change the system prompt, or acquire author
identity. Keep provider credentials, private model scratch work, and unrelated
strategy context out of diplomatic payloads.

A compliance assessment is a separate derived record:
`obligation_id`, `as_of_round`, `view_player`, `verdict`, `evidence_refs`,
`coverage`, and `reason`. Verdicts are `pending`, `observed_met`,
`observed_breached`, or `unknown`. Evidence must have been visible to that view by
the relevant deadline. Later observations cannot retroactively masquerade as
knowledge held earlier. Opponent assertions may support a belief, not a certified
verdict. A missing observation and a proved absence are different conditions.

Protocol delivery is completely observable to its parties. In contrast, failure
to observe an attack over a fogged interval cannot prove non-aggression. The
referee may retain private diagnostics about a party's own accepted attacks, but
must not disclose them to another model as an omniscient treaty verdict. A future
consent-based witness service could release a narrowly specified attestation;
that information disclosure must be explicit in the signed terms and separately
tested. It is not an implicit permission created by accepting a treaty.

Reputation v1 is a per-view count of proved commitments met/breached and unknown
assessments, with source references and coverage. It is not a calibrated honesty
probability or a universal cross-player score. Models may keep their own beliefs,
including suspicions and willingness to betray; label those as claims. Breach does
not automatically void unrelated obligations or authorize retaliation. Conditional
termination or sanctions require exact, agreed terms and implemented capabilities.

## Cost and integration defaults

First live integration should add bounded `diplomacy_ops` to a new strict strategic
directive version, then dispatch validated operations through the same facade.
Accepting malformed diplomacy cannot partially apply a directive: validate its
complete shape and all command references first, then execute sequentially with
explicit per-operation results. An earlier accepted operation is durable if a
later runtime operation rejects; no implicit transaction rollback across engine
and diplomacy effects. Audit that partial outcome and apply the existing failure
policy rather than repeating the entire batch blindly.

Proposed starting bounds, to be made explicit configuration in implementation:

- At most 4 diplomatic operations per decision, 2 new offers per own turn,
  8 open offers and 8 active treaties per private channel, and 8 obligations per
  treaty. Channel-local admission avoids exposing the load of unrelated private
  negotiations through a capacity rejection; the first live match has two seats.
- Each offer's complete consent document at most 1,600 characters; each message
  at most 600 characters; literal claim text remains separate from exact terms.
- Curate due/active obligations, actionable offers, then new messages in a
  deterministic order inside the existing 8,000-character total context limit.
  Report omitted nonessential history counts to its authorized recipient. Never
  omit the exact terms required for a decision while pretending they were shown.
  If required game state plus required terms cannot fit, report a bounded context
  failure; do not silently raise token/request limits or accept from a summary.
- New actionable offers, a proved breach, or a due obligation can trigger one
  strategic decision on that seat's turn. Coalesce all triggers. Repeated text,
  delivery acknowledgements, and an unchanged offer do not trigger more calls.
  Keep the normal five-turn cadence, existing one-repair rule, per-agent 2,000-post
  cap, and 600-second turn limit. No extra negotiation-only round-trip loop.

Autopilot consumes the model's strategic preferences and known commitments as
planning inputs. It may present a conflict and alternatives, and the model may
choose a deliberate breach. A voluntary agreement adds no hard action filter.
For an imminent conflict, a bounded strategic trigger is preferable to silently
changing the agreement's enforcement mode. If a provider call cannot complete,
use the existing explicit failure outcome; do not invent consent or a reply.

## Implementation sequence and acceptance probes

1. **D0, this document:** source-backed contract and unresolved capability bounds.
   No protocol code, external calls, or live behavior is claimed.
2. **D1, offline protocol:** typed commands/terms, strict parser, canonical consent
   digest, reducer, recipient projection, prefix reconstruction, exact receipt
   evidence, fixture-driven report-delivery and unknown-under-fog assessments.
   No engine integration is needed to make this slice concrete and testable.
3. **D2, facade and replay:** session-bound non-action tools, audited results,
   idempotency, paired rebuild, dedicated comparable diplomacy result digest,
   player-local inboxes. Prove zero adapter calls from diplomatic operations.
4. **D3, strategic context and operator GUI:** opt-in directive version, bounded
   triggers, exact shown-context audit, offer revision comparison, consent and
   delivery timeline, obligation graph with evidence/unknown labels. A separate
   player projection powers any player preview; the operator view is labelled.
5. **D4, isolated live proof:** fresh short match with two actual models exchanging
   accepted JSON offers/counters/acceptance and report delivery. Show both private
   contexts and browser network responses independently. Then run the required
   live reliability gates for that changed implementation. Do not introduce
   diplomacy into an already running acceptance match.

Required behavioral probes for D1–D3:

- Spoofed author/lease/party, foreign thread reads, cross-match object IDs, boolean
  IDs, duplicate JSON keys, unknown tags, oversized/deep payloads, and stale
  acceptance reject without state change; both malformed and unauthorized calls
  leave accurate receipts where the log remains writable.
- The exact consent hash changes for every meaningful audience/party/expiry/term
  change. Counteroffer clears old consent. Duplicate identical accept is
  idempotent; conflicting reuse rejects. Concurrent-looking accept/withdraw
  resolves by authoritative sequence, not wall-clock timestamps.
- Partial round, skipped/duplicate seat, final deadline boundary, late acceptance,
  expiration, activation, and unknown prerequisite cannot create phantom consent
  or compliance. A blocked match does not advance the diplomacy round clock.
- Record-before-ack crash, unpaired call, result-namespace mismatch, truncated
  prefix, duplicate result, tampered delivery digest, and model-free replay
  reconstruct or fail explicitly; replay compares actual inbox/result content.
- Inject hidden private statements, opponent roster IDs, secret clauses, private
  evidence, global sequence gaps, HTML, and prompt-like text. Compare both player
  projections against independent allowlists, including API payloads and caches.
- An exact delivered report can meet a delivery promise while its factual contents
  remain unverified. Unobserved attack is unknown; visible, attributable breach
  cites evidence; late evidence and opponent accusations do not invent proof.
- A voluntary treaty does not reject a legal attack or admit an illegal action.
  A reported breach changes only the appropriately scoped diplomacy view and
  planning input. There are zero adapter/provider calls in the pure reducer.
- Quiet turns remain zero-request. Many messages coalesce to one decision trigger;
  original request caps/deadlines hold. Required exact terms either fit with game
  state or produce an honest context-limit outcome.
- Real browser evidence distinguishes an offer from an active treaty, an accepted
  message from delivery, a report from a fact, and unknown from proved breach.
  Hidden private rows must be absent from the other seat's preview and network
  response, not merely hidden with CSS. Operator visibility is documented.

These tests establish protocol, privacy, and accounting behavior. They do not
establish better strategy, honest partners, calibrated trust, full-game victory,
or completed live matches. Complex trade enforcement, game-native diplomacy,
resource escrow, automatic coalition authority, cross-match reputation, and
arbitrary natural-language contract adjudication remain later work.

## Evidence status for D0

Verified findings: the source map above was read at the bound commit; this change
is documentation only. JSON example parsing, relative-link existence, and whitespace
checks are recorded in `/tmp/civ-diplomacy-doc-check.log` and
`/tmp/civ-diplomacy-doc-content-check.log`.

Follow-up probes: D1–D4 above are proposed implementation and validation work.

Blocked checks: none required for this documentation slice. Runtime tests and
live negotiation are not applicable to a design-only change.

Evidence gaps: no implemented bus, treaty consent runtime, player inbox endpoint,
provider exchange, or browser negotiation proof exists in this change.
