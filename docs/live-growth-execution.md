# Observed growth execution

`growth_autopilot: true` is an explicit per-agent option requiring
`decision_mode: strategic_autopilot`. It adds observed native unit capabilities,
local threat assessment, reserved guards and one settler/escort mission to the
curated strategy briefing. Unknown opponent intent remains unknown. Strength sums
are planning heuristics, not combat odds or proof of an optimal army size.

Production counts owned, queued and reserved units across the empire. The default
military cap is eight, the scout cap is two, and city guards plus one expansion
escort create bounded defense demand. Explicit model unit targets are upper
bounds. Confirmed nearby barbarians favor defense; three clear observed own turns
permit growth. Damaged inventory does not create unlimited replacement demand.

The settlement executor sends at most one automatic facade action per completed
seat turn. It advances an escort and settler over observed land, avoiding known
contacts, preserving nearby city guards and giving health recovery priority.
Model tactical orders retain their one-turn priority. Existing movement allowance
is unchanged: only untouched opening frozen movement is available, once per unit.
No additional native movement restoration or direct desktop input is introduced.

Accepted input is not observed progress. Requested displacement must be observed;
founding requires an accepted request, consumed founder and a new owned city at
the requested site. Intervening model actions cannot establish automatic-action
causality. A rejected or ambiguous action blocks automatic repetition and triggers
one bounded strategy review. Automatic replanning after such a block is not yet
implemented. Successful mission history commits only after verified turn closure;
a failed controller turn poisons reuse.

Production and travel use separate clocks. Observed queued settler production does
not expire because it is slow. Sixty unproductive observed own turns trigger review
without expiring the mission. After a settler is assigned, twelve own turns without
confirmed movement expire travel. Actual movement resets that progress clock;
polls and accepted no-ops do not. Overall match deadlines remain authoritative.
These controls are conservative heuristics, not calibrated strategy forecasts.

## Verified findings

The author candidate retained 347 passing focused tests and a passing Ruff check in
`runs/growth-integration-evidence-20260906/focused-final.log` and `ruff-final.log`.
`freeze-binding.json` binds source and complete logs. Coverage includes slow queued
production, a sixteen-turn progressing escorted route, health holds, rejected and
superseded actions, observed founding, failed closure and explicit opt-in wiring.
The final integrated release gate is recorded separately.

## Follow-up probes

Observe production choices, recovery, escort travel and founding in a fresh live
match. Native catalog parsing has separate host evidence; it does not establish
successful engine settlement execution.

## Blocked checks

No external blocker is asserted by this repository candidate. Live validation is
pending final integration checks and independent review.

## Evidence gaps

No live 100-round completion, optimality, full-game victory, automatic recovery of
blocked missions, or four-seat engine behavior is established by these tests.
