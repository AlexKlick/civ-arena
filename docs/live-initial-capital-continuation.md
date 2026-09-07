# Initial capital continuation

With `growth_autopilot: true`, the first explicit tactical move of a sole observed
owned `SETTLER`, while there are no owned cities, now records that destination as
an initial-capital intent. The move still executes as an ordinary one-turn model
order. Observed arrival enables one automatic `found_city` request on a later
quiet turn at that same site. It does not require a production catalog or spare
escort, which depend on the capital already existing.

The existing `GROWTH_SYSTEM` prompt discloses this behavior. The directive schema
and count/admission/generation body construction are unchanged. No site is chosen
automatically and no optimality is claimed. An explicit immediate founding order
continues to use the existing tactical path. Growth-disabled behavior and ordinary
escorted expansion missions retain their previous behavior.

Before automatic founding, the current projection must show the same owned
settler at the selected site, verified recovered health, known land with known
unowned/own ownership, no existing city within four hexes and no observed foreign
unit within two hexes. These are conservative feasibility guards, not native
legality or safety proofs. The engine checks the request through the existing
facade. Current tactical orders and recovery holds have priority. Existing
untouched opening frozen movement may be used once; no new movement restoration
or native operation is introduced.

Input acceptance alone cannot complete the plan. Completion requires an accepted
automatic founding request, a new owned city at the selected site, absence of the
founder in the current projection and then verified turn closure. Rejected,
ambiguous and superseded input never repeats automatically. A terminal city
observation without that receipt cannot later be upgraded into causal completion.
The initial-capital path stays disabled once an owned city is observed, including
after later city loss.

One additional model review is available for a missing choice or blocked progress;
quiet turns do not each trigger a provider call. A plan expires after twelve own
turns from selection. This is a bounded continuation heuristic, not an empirically
optimal travel limit. Existing match deadlines and ordinary strategy cadence
remain unchanged. The site is immutable; recovery/repositioning needs explicit
model orders, and there is no automatic relocation or save recovery.

## Verified findings

The read-only retained prefix from `minimax100-20260907T011202Z` established the
trigger. P1 turn 1 directive sequence 119 moved settler `u1:65536` from `-4,26` to
`-3,25`. Turns 2 through 5 contained no tactical orders and no settlement mission.
Turn 6 sequence 541 moved warrior `u1:131073`; the settler remained cityless. These
are immutable prefix observations, not a terminal result for that active match.

Local affected tests: **363 passed, 0 failed, 0 skipped**, complete capture in
`runs/initial-capital-evidence-20260907/focused-release.log`. The isolated regression
file covers 29 cases, including quiet-turn founding, no catalog, health/site guards,
frozen allowance, rejected/ambiguous input, bounded review, expiry, cancellation,
closure failure and compatibility. The initial 24-pass/2-fail run used a fake model
that replayed its first relocation at every review; that fixture was corrected to
return an empty later directive. All initial logs remain preserved.
Full Ruff over `src tests scripts` passed in the same directory's `ruff-release.log`.
The final cases also cover P1 identity/capture and exact counted adaptive dispatch
followed by quiet-turn founding with no further provider POST.

## Follow-up probes

Independently review this frozen commit and validate any later integration. A
fresh native match should confirm the selected initial site, subsequent facade
founding, new city, consumed founder, lease release and request accounting. The
active `538f84b` match is not changed by this worktree.

## Blocked checks

No external blocker affected repository implementation. Native execution is
pending integration and qualification; it is not established by these fixtures.

## Evidence gaps

There is no claim of an optimal capital site, full-game strategy, live 100-round
completion or a live pass for this new continuation. Foreign proximity does not
establish war, and remembered terrain does not prove the absence of hidden units.
Multiple initial settlers are intentionally ambiguous: no automatic selection is
made. Failed or superseded plans require explicit model action; automatic replanning
is outside this correction.
