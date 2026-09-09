# Live turn pacing

Operator requirement, 2026-09-05: routine early turns should make useful
progress quickly. Decision speed is part of agent quality. A small action
space should not cause repeated observation and deliberation without action.

## Verified starting point

The connected-monitor attempt completed zero agent turns and sent zero model
requests. Its delay was startup and failed first-turn engagement, so it does
not measure model thinking speed or demonstrate analysis paralysis.
[Run outcome](../runs/launch-evidence-20260905T000405Z/outcome.json).

The current configuration permits 16 tool rounds per turn, 120 seconds per
provider request and a 600-second agent-turn ceiling. These are failure
ceilings, not acceptable routine-turn latency. Existing provider configuration,
request caps and operational deadlines remain unchanged by this document.

## Requirements for the next implementation

1. Fix initial-turn acquisition and the extra-major-player roster defect first.
   Faster inference cannot repair a controller that never grants a turn.
2. Measure startup, handoff/lease wait, provider latency, tool execution,
   time to first accepted action and time to verified turn completion
   separately. Report request counts and median/p95 elapsed times from actual
   completed live turns. Keep timing outside deterministic/replay state.
3. Give the policy a compact view of visible state, legal choices and the
   current plan. Batch independent observations where the adapter supports it;
   avoid repeated identical queries. Execute dependent mutations in order,
   through the existing facade, with updated legality checks.
4. Use a fast routine-turn policy: continue useful scouting/standing orders,
   maintain production and research commitments, and choose eligible research
   using visible resources and the near-term development plan. Reconsider
   after relevant events rather than rebuilding the entire strategy each turn.
5. Aim for one planning response and at most one correction response on a
   routine turn. Reserve deeper deliberation for consequential changes such
   as city placement, combat threats or a blocked strategic plan. This is a
   proposed policy budget, not a change to the existing provider safety caps.
6. When deliberation reaches its soft budget, complete legal useful actions
   and standing orders using the existing facade, then verify turn closure.
   Do not merely cancel inference and leave a lease or incomplete turn open.
   Do not count blanket idling as evidence of better play.

Initial proposed user-experience targets are a first useful accepted action
within 15 seconds and a routine completed seat turn within 30 seconds, measured
from verified engagement. These are unvalidated targets to calibrate against
real provider/engine latency, not claimed capabilities. Complex turns should
have an explicit reason for spending longer.

## Follow-up probes and blocked checks

After turn acquisition works, measure the first ten completed live seat turns
without changing providers. Determine whether repeated requests, model latency,
tool serialization, engine execution or handoffs dominate before optimizing.
Compare pace alongside useful scouting, settlement, research/production
progress, rejected actions and completion failures. Fast but ineffective turns
do not satisfy the requirement.

The later continuation `minimax-resume-20260905T004340Z` acquired the initial
turn and completed six seat turns. P0/P1 opening turns took 63.27/127.95 seconds;
turn two took 23.40/16.52 seconds; turn three took 24.89/55.94 seconds. The two
clients sent 17 and 33 requests respectively. This small, aborted sample does
not establish a latency distribution or isolate provider reasoning as the
bottleneck. [Recorded summary](../runs/minimax-resume-20260905T004340Z/summary.json).
Faster-play policy changes described here are not implemented.

## Forecast and dependency graph sequence

The browser match room first exposes the existing accepted planning notes and
recorded tool sequence. Its edges mean execution order; no causal or forecast
branches are manufactured from those logs.

The next strategy implementation should add an explicit plan record through
the existing event log, with stable node IDs, prerequisite edges, alternatives,
estimated costs/turns, current legality, and visible-state evidence. Cover
research, civics, units, buildings and map actions under the same schema.
Separate rule-derived prerequisites from model proposals. Represent uncertain
outcomes as model-reported estimates with assumptions and disconfirming events;
do not present uncalibrated confidence as measured probability.

Then retain the chosen plan across turns and execute ready legal nodes through
the facade. Revalidate immediately before each mutation, observe its result,
and invalidate only affected dependents when the world changes. Limit search
depth and alternatives on routine turns; reserve deeper planning for threats,
new information and consequential choices. Never parallelize dependent game
mutations or acquire a second tuner client.

Build graph emission, browser branch inspection, and a deterministic bounded
plan executor as separate work lanes after locking the event contract. Test
cycle rejection, stale prerequisites, invisible-information exclusion, rejected
actions and closure failure before comparing pacing and useful progress with
the unchanged MiniMax configuration. Owner-qualified unit identity is a
prerequisite for trustworthy execution and movement accounting; see the
[live diagnosis](live-resume-diagnosis-20260905.md).
