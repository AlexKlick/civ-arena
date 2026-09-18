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

