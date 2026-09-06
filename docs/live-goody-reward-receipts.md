# Command-bound tribal-village population receipt

The preserved `minimax60-20260905T052156Z` run stopped after 42 seat turns.
Its city population changed from 3 to 4 during player 1's turn 21, immediately
after scout `u1:327683` moved from engine `(29,13)` to `(30,13)`. The final
watchdog reported that row at turn end because the mod's undeclared ledger is
drained there. The event timing does not support a post-deactivation growth
explanation. A tribal-village reward remains a hypothesis for that historical
run: no reward event was captured and the failed run cannot be reclassified.

Mod 0.3.10 prospectively binds native `Events.GoodyHutReward` observations to a
single move window. The native event supplies player, raw unit ID, reward type,
and reward subtype. The controller assigns the turn and command nonce when it
opens that window; those are not native event fields. The adapter opens the
window before restoring the unit and dispatching the move, then closes it with
the command diff. Transport retries reuse the same completion and provenance.

The only added authorization is one exact owned-city population `+1` row when:

- The current lease, player, unit, turn, nonce, and diff sequence match.
- The destination initially contains a tribal village, or a Sumerian barbarian
  camp with its active civilization/trait/modifier chain validated, and that
  exact site type is subsequently removed.
- Exactly one matching native event names `GOODYHUT_SURVIVORS` / `GOODYHUT_ADD_POP`,
  and the unit is at the command destination when the event is observed.
- The active modifier is `MODIFIER_PLAYER_NEAREST_CITY_ADD_POPULATION` with
  `Amount=1`; no game-speed-scaled reward amount is inferred.
- The nearest owned city is unique, existed at the opening boundary, and is the
  only city whose population changed. Its rolling ledger baseline equals the
  opening population and its observed population increased by exactly one.

A camp is eligible only for `CIVILIZATION_SUMERIA`, linked through
`CivilizationTraits` to `TRAIT_CIVILIZATION_FIRST_CIVILIZATION`, then through
`TraitModifiers` to `TRAIT_BARBARIAN_CAMP_GOODY`. The active modifier must be
`MODIFIER_PLAYER_ADJUST_IMPROVEMENT_GOODY_HUT`, with owner collection and
`EFFECT_ADJUST_IMPROVEMENT_GOODY_HUT`, and exactly the arguments
`ImprovementType=IMPROVEMENT_BARBARIAN_CAMP` and
`GoodyHutImprovementType=IMPROVEMENT_GOODY_HUT`. Missing or changed links do not
establish camp eligibility. Ordinary other-civilization camps create no reward
expectation or missing-event quarantine. The causal receipt records the
original eligible improvement type.

The exact mutation is returned as a commanded effect with `causal_receipts` in
the audited tool result. Other native rewards carry bounded numeric type/subtype
and classification diagnostics. Gold, faith, science, spawned-unit rewards,
ambiguous targets, missing events, duplicate events, and other unmatched drift
receive no additional authorization. If an eligible reward site disappears without a
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

## Native subtype hash binding correction

Run `minimax60-20260906T172847Z` stopped after 25 rounds / 50 completed seats.
It retained one native event: reward type `1892398955`, subtype `1038837136`,
followed by an undeclared population `3 -> 4` row. The event was classified
`unsupported_or_unmatched`; it did not carry an authorized causal receipt.
The failed run remains failed.

A subsequent read-only GameCore probe verified that
`DB.MakeHash('GOODYHUT_ADD_POP') == 1038837136`, while
`GameInfo.GoodyHutSubTypes[1038837136]` is absent. The table's string key and
ordinal 16 both resolve the active population reward row. Version 0.3.10 checks
the native event against the engine-computed hash, then resolves the named row
and retains all modifier, identity, destination, and exact-delta checks.
The numeric row ordinal is not accepted as a native event subtype. Missing hash
capability fails preflight. No population drift allowance is added.

Evidence: `runs/60-round-live-20260906T172847Z/poststop-native-subtype.log`,
`native-reward-terminal.json`, and `independent-audit-compact.json`.
This establishes the identifier mismatch, not every missing causal condition
in the preserved run. A fresh event and matching receipt remain live checks.
