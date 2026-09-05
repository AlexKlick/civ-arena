# Command-bound tribal-village population receipt

The preserved `minimax60-20260905T052156Z` run stopped after 42 seat turns.
Its city population changed from 3 to 4 during player 1's turn 21, immediately
after scout `u1:327683` moved from engine `(29,13)` to `(30,13)`. The final
watchdog reported that row at turn end because the mod's undeclared ledger is
drained there. The event timing does not support a post-deactivation growth
explanation. A tribal-village reward remains a hypothesis for that historical
run: no reward event was captured and the failed run cannot be reclassified.

Mod 0.3.8 prospectively binds native `Events.GoodyHutReward` observations to a
single move window. The native event supplies player, raw unit ID, reward type,
and reward subtype. The controller assigns the turn and command nonce when it
opens that window; those are not native event fields. The adapter opens the
window before restoring the unit and dispatching the move, then closes it with
the command diff. Transport retries reuse the same completion and provenance.

The only added authorization is one exact owned-city population `+1` row when:

- The current lease, player, unit, turn, nonce, and diff sequence match.
- The destination initially contains a tribal village and it is subsequently removed.
- Exactly one matching native event names `GOODYHUT_SURVIVORS` / `GOODYHUT_ADD_POP`,
  and the unit is at the command destination when the event is observed.
- The active modifier is `MODIFIER_PLAYER_NEAREST_CITY_ADD_POPULATION` with
  `Amount=1`; no game-speed-scaled reward amount is inferred.
- The nearest owned city is unique, existed at the opening boundary, and is the
  only city whose population changed. Its rolling ledger baseline equals the
  opening population and its observed population increased by exactly one.

The exact mutation is returned as a commanded effect with `causal_receipts` in
the audited tool result. Other native rewards carry bounded numeric type/subtype
and classification diagnostics. Gold, faith, science, spawned-unit rewards,
ambiguous targets, missing events, duplicate events, and other unmatched drift
receive no additional authorization. If a known village disappears without a
matching native event by window close, the mod quarantines subsequent moves,
including across lease transitions, and records `missing_consumption_event`.
The adapter immediately aborts on that completion, including on the final
move of the final turn. Late events cannot lift the quarantine. Existing
movement-drift accounting stays
unchanged. No baseline reset, growth allowance, or handoff change is introduced.

Source references are the installed base `gameplay/data/goodyhuts.xml` (reward,
modifier, and amount), plus shipped Pirates/CivRoyale minimap handlers showing
`OnGoodyHutReward(player, unitID, rewardType, rewardSubType)`. The parent retained
read-only host API checks in `runs/60-round-live-20260905T052156Z/`: both VMs
expose the event registration API; the subtype has Index 16 and no Hash field;
the modifier and amount accessors and plot/city APIs returned the expected data.

Local tests execute the actual Lua mod in a mocked native-engine environment and
exercise the Python adapter and parser. They establish implementation behavior,
not delivery of a real native reward event. Registration capability is required
at preflight. Actual event delivery/order and receipt production still require
a fresh live game; unavailable causal proof must preserve an honest failure.
