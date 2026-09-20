# civ-arena Research Ledger

Append-only record of the research loop: one section per iteration, written by the lane that ran it, never edited after the fact.

## Iteration 001 — Batch 1 baseline (2026-09-17)

- 16 games, 16 clean / 0 dirty. Roster [turtler, turtler, expansionist,
  expansionist], 4 seeds x 4 Latin rotations, 40 turns.
- Per-strategy (descriptive, integer-first): turtler mean_rank 2.3125
  (rank_sum 74/32, wins 9), expansionist mean_rank 2.6562 (rank_sum 85/32,
  wins 7). Mean scalar 1018.6 vs 1004.4.
- STRUCTURAL FINDING (seat axis): seat2 mean_rank 4.0 in ALL 16 games for
  BOTH doctrines; seat3 1.25, seat0 1.75, seat1 2.25. Doctrine cannot
  explain a constant last place across every seed and rotation — this is a
  seat/map artifact (suspects: duel-hardcoded doctrine geometry
  scripted.py:38,109,153 pointing seat2 walkers into the most contested
  corridor; phase-order interaction). Investigate before judging rosters.
- Next: flash roster proposal (user-approved), then batch 2 on the same
  seed/rotation plan.

## Iteration 001b — H1 test: geometry seam removes the seat artifact (2026-09-17)

- Same 16-game seed/rotation plan as batch 1 (batch-001b), re-run after the
  geometry seam (3015cb4: 4-seat march/settler/corner targets derive from
  STARTS_4 instead of duel literals). 16/16 clean. Everything else identical,
  so the diff is attributable to the seam alone.
- H1 CONFIRMED. Seat-2 pooled mean_rank 4.0 -> 2.875 (prediction < 3.5);
  pooled seat means 2.375 / 2.438 / 2.875 / 2.312 (was 2.25 / 2.438 / 4.0 /
  1.25) — seat spread collapsed 2.75 -> 0.56 rank points. The batch-1
  anomaly was the hardcoded geometry (pre-seam, seats 1–3 all marched at the
  same duel literal near seat-2's doorstep), not doctrine quality or a map
  asymmetry.
- Secondary finding the confound was masking: the doctrine gap WIDENED
  post-seam. turtler mean_rank 1.75 (wins 14/16 games, mean_scalar 1092.4)
  vs expansionist 3.25 (2 wins, 987.4); pre-seam 2.31 vs 2.66. At 40 turns
  in the 4-seat arena the 2-city walls/defense profile out-scores 4-city
  settler expansion on the scalar (100*cities + 20*pop + 30*techs +
  10*units + 1*gold). Caveat: 2 doctrines only, n=16, descriptive — the
  upcoming 4-doctrine batch is the real test of whether anything beats
  concentrated turtling.
- Residual: seat2 mildly elevated (2.875 vs ~2.37 elsewhere) — small, n=16,
  watch it across future rosters before treating it as map signal.
- Artifacts: configs/research/batch-001b.yaml, runs/research/batch-001b/
  (gitignored; aggregates in results.md there, H1 outcome in
  research/hypotheses.json).

## Iteration 002 — the flash-proposed roster ran; broker mechanism proven; analysis leg degraded-transport (2026-09-17)

- Roster: glm-5.3-flash proposal (iteration 000) transcribed verbatim —
  hyperwide_flood (wide, 6 cities), granary_engine (tall, 3),
  archer_horde (military, ARCHERY beeline, aggression 6), settler_broker
  (economic, gold->settler purchases). User-approved before
  implementation (02924b6); gate 2554 passed / 2 skipped / 1 pre-existing
  base failure.
- 16 games, 16/16 clean, same seed blocks x Latin rotations (batch-002).
  Per-strategy mean_rank: hyperwide_flood 1.5625 (11 wins, scalar
  1137.9), archer_horde 2.0625 (3, 1044.5), granary_engine 3.0 (2,
  977.4), settler_broker 3.375 (0, 899.1). Seat decomposition shows no
  structural seat artifact (H1's fix holding on a new roster).
- VERIFIED MECHANISM (H2, trajectories + rules arithmetic): settler_broker
  never founds a 3rd city because the purchase gate (turn%3==0,
  gold>=120) fires while SETTLER (160 gold) is unaffordable and the next
  preference GRANARY (exactly 120) drains the settler bank. Gold clears
  160 BETWEEN gate turns (162@t25, 173@t35 in s300003-r2) but the bank
  never survives TO a gate turn. Cities flat at 2 all game in every
  inspected match; in horde-adjacent matches its units collapse to 0 and
  stay there (farmed while turtling at pop 14).
- Cross-batch tension: batch-001b's expansionist (4 cities, march on)
  LOST to turtler, but hyperwide_flood (6 cities, march off, settler
  first) dominates everything. Leading reconciliation: the losing variable
  is military spending/marching, not city count — flood pays zero army
  tax and out-compounds the field. Untested against turtler directly
  (different rosters, never met on a map).
- FLASH ANALYSIS LEG: DEGRADED-TRANSPORT. 4 consecutive full retry
  budgets (~14 attempts, 480s timeout each) returned 0 bytes while the
  512-token probe passed throughout — the endpoint hangs on this prompt
  size tonight. Typed http_error recorded in
  research/iterations/002/analysis.json (fail-soft held: no crash, no
  fabricated data, spend trail complete). This is a retry condition, not
  the 2x-unparseable termination criterion; retry analyze when the lane
  recovers, possibly with a slimmer prompt (drop the static briefs).
- Standing questions for batch 3: (a) does the H2 bank_floor fix rescue
  the broker (prediction registered in hypotheses.json); (b) does
  hyperwide_flood beat batch-001b champion turtler head-to-head; (c) do
  adaptive switcher seats (flood->turtler under pressure) beat both
  static parents.

## Iteration 003 — long games, pivot seats enter, per-bot flash audit (2026-09-17/18)

- Batch-003 (operator directive: much longer games, test pivoting, deep
  per-bot execution analysis): 160 turns, 2 seed blocks x 4 Latin
  rotations, 8 games, 8/8 clean. Roster: hyperwide_flood + turtler (the
  never-met champions) + flood_pivot (expand until punished -> turtler
  on hp-below-0.5 or 4 foreign units seen) + turtle_pivot (turtler ->
  flood at turn>=30 with 2 cities). 6973b93.
- HEADLINE — A PIVOT SEAT WON THE BATCH. flood_pivot mean_rank 1.50, 5/8
  wins, mean_scalar 2821 — beating BOTH static parents (turtler 2.00/2
  wins/2436; hyperwide_flood 2.625/1 win/1864). turtle_pivot 3.875, 0
  wins, 1490 — WORSE than its own parent. Pivoting direction and timing
  decide everything: early state-triggered pivot = champion; late
  clock-triggered pivot = dead last.
- Mechanism (local, from trajectories): flood_pivot founded 2-3 cities
  in flood's opening, pivoted at t10-t30 when pressure arrived, then
  defended what it kept (units ramp 6->253; wins came holding 2-3
  cities). 6/8 games = one clean switch held; 2/8 oscillated (memoryless
  flip-flops). turtle_pivot switched at exactly t30 in 8/8 games (clock
  trigger + cities>=2 true since t10 = deterministic) and founded ZERO
  cities after — the map's founding space was gone; it played flood's
  build order from a 2-city base for 130 turns.
- PER-BOT FLASH AUDIT (new lane, one glm-5.3-flash analyst per seat;
  facts computed locally, model audits — research/iterations/003/deep/):
  all four EXECUTING=True; execution fidelity turtler 8/10 (cap honored
  perfectly, exact tech curve, monotonic unit treadmill), flood_pivot 6,
  turtle_pivot 4, hyperwide_flood 3/10.
- AUDIT'S BIG CATCH: hyperwide_flood's thesis NEVER materialized —
  expected 5-6 cities by t30, actual peak 3 in every game INCLUDING its
  batch-002 championship (founding space exhausts at 4 seats). Its
  batch-002 win was opposition-dependent; against turtler + another
  early expander it collapsed (1 win) and converted production into
  60-131 units instead of settlers. The champion's stated theory of
  victory was wrong even when it won.
- Universal structural findings (all four seats): research lists
  exhaust by t20-30 and science freezes for 130+ turns; late-game unit
  overproduction regardless of doctrine (100-250 units by t160); pop
  plateaus mid-game everywhere. The scalar's late game is a unit-count
  contest between defenses, not an economy contest.
- Batch-004 candidates (flash-recommended, grounded): pivot hysteresis
  (dwell time / 2-consecutive-check triggers), radius-restricted threat
  predicates (exclude scouts), unit caps tied to city count, research
  overflow after list completion, earlier signal-gated turtle_pivot
  (t12-18), settler escorts. H3/H4 registered in hypotheses.json.


## Iteration 004 — H2 refuted, the incumbent pivot replicates (2026-09-20)

- Batch-004 (26cf3a9): 160 turns, 4 seed blocks x 4 Latin rotations,
  16/16 clean. Roster: turtler (static benchmark) + flood_pivot (the
  batch-003 incumbent VERBATIM) + flood_pivot_sticky (hysteresis as
  stickiness: single {min_foreign_units_seen: 1} trigger, returns to
  flood only when contact fully clears — the memoryless proxy for the
  deferred dwell-time extension) + settler_broker_banked (H2's fix:
  doctrine-level bank_floor=160; the gate waits for the top preference).
- HEADLINE — H2 REFUTED IN ITS STRONG FORM. The mechanism was real but
  not the binding constraint: the banked broker's gold clears 160 in
  13/16 games (peaks 148-213, so purchases CAN fire) yet cities@t25 =
  3 in only 4/16 (prediction: majority; old broker was 2/16-of-16 at
  2.00, banked mean 2.25). Mean_rank 3.9375, 0/16 wins — no >=0.5
  improvement. Founding-space competition (H3's mechanism) dominates a
  gold-banking strategy: seats founding via production from turn 1 take
  the legal spots before a banked 160-gold settler can walk to one.
- H4A — STICKINESS IS NOT THE IMPROVEMENT. flood_pivot_sticky 5/16,
  mean_rank 2.0 — beats turtler (3/16, 2.125) but does NOT beat the
  incumbent dual-trigger flood_pivot (8/16, 1.875). Always-turtle-on-
  first-contact is better than the late clock pivot (turtle_pivot 0/8)
  but worse than pressure-responsive pivoting: the incumbent's
  hp-or-count triggers capture "punished enough to stop" better than
  "any contact". The true dwell-time/2-check extension remains
  untested (needs the history-dependent trigger machinery).
- THE INCUMBENT REPLICATES: flood_pivot wins 8/16 at rank 1.875
  (batch-003: 5/8 at 1.50, overlapping field composition). The
  state-triggered pivot result is now n=24 across two batches — the
  loop's most robust finding alongside H1/H3.
- Seat decomposition: flood_pivot@seat2 = 3.0 vs @seat1 = 1.0 (n=4
  cells) — the seat-2 mild elevation from batch-001b's residual is
  visible again in one doctrine; descriptive only.
- Batch-005 candidates from this batch's evidence: (a) H3-directed —
  a broker that founds via PRODUCTION early and banks LATE (bank_floor
  only after own_cities >= 3); (b) the research-overflow fix now that
  every doctrine's science freezes by t30; (c) unit caps tied to city
  count (the t160 unit treadmill); (d) settler escorts (purchased
  settlers walking through contested space).
