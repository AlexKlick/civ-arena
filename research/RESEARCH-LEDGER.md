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

