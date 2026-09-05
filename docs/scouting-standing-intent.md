# Persistent scouting assignment and fortification

This isolated correction starts at `30adf0349a32875957c29120b32a173c35fbb8d4`.
It does not change the implementation of the running
`minimax60-20260905T044417Z` match, its game state, or its completed-turn evidence.

The policy defect was observed on seat 0: event 25 assigned warriors to scouting;
event 49 recorded an accepted movement request whose subsequent position stayed
at `39,14` while movement changed from frozen zero to two. Completion rejected
the open turn at event 64, and events 65/66 applied an accepted fortification.
On turns 2–6, the persistent scouting assignment remained active, but graphs
150, 205, 259, 313, and 373 selected `existing_standing_order` and no action.
Turn 6's new model directive reaffirmed the assignment without a tactical
override. The unit remained idle. This is a strategic execution defect; it is
not a missing completed seat turn or proof of a watchdog failure.

The read-only capture is `/tmp/civ-standing-order-policy-review.log`. It binds
319,881 bytes and 539 records through sequence 538 of the growing event log;
the captured-byte SHA256 is
`c866cc06196f0a81e0bbbe421ff3f0388234ff4c4a53af3f19fc2c70e57f7440`.
Later live progress is outside that capture.

The resolution is a precedence rule in
[`scouting.py`](../src/civ_arena/agents/scouting.py):

1. Actual spent movement still prevents an attempt. The existing explicit
   opening-frozen-roster exception remains the only exception.
2. A current-turn tactical override wins, including an explicit hold.
3. A unit type listed in the latest persistent `scouting.unit_types` directive
   is assigned to scouting. Reevaluate its candidates even if it is fortified.
4. An unassigned fortified unit retains its standing order.

This gives role assignment a clear meaning for all matching owned units. It
does not infer who caused an existing fortification or require a new provenance
store. An explicit tactical hold lasts its issuing turn, as the directive
contract already specifies; the persistent scouting role resumes on a later
quiet turn. Removing a type from `scouting.unit_types` preserves that type's
fortified units. Persistent per-unit defensive roles remain a future directive
extension, rather than an inferred lifetime for a one-turn override.

The graph records `standing_order_resolution: persistent_scouting_assignment`
when it reevaluates a fortified assigned unit. Actual pre-action fortification
and movement readings remain unchanged in the execution audit. The planner uses
normal facade commands; it does not send a separate wake command, refill
movement, bypass engine checks, or retry an accepted no-op in the same turn.
Threat exclusions, unknown terrain, per-unit attempt bounds, and the existing
turn-closure repair remain in force.

[`test_scouting_standing_intent.py`](../tests/test_scouting_standing_intent.py)
uses the real runtime, strategic controller, curator, scouting executor, and
closure logic with a projected-state facade and fake model. It covers an
accepted request with unchanged position, forced completion fortification, then
a successful scouting attempt on the next quiet turn with zero additional
provider requests. Separate cases preserve a current tactical hold, unassigned
fortification, spent movement, and conservative threat exclusions. These are
local behavioral checks. A fresh live test after integration is still required
to establish the engine's observed movement under this policy change.

Focused verification: **76 passed in 0.51s** across the new transition fixture,
scouting, strategic-controller, and context-curator tests, captured in
`/tmp/civ-scouting-holds-pytest-r1.log`. Focused Ruff passed in
`/tmp/civ-scouting-holds-ruff-r1.log`. The worktree creation record is
`/tmp/civ-scouting-holds-worktree.log`; no live input was used by this work.
