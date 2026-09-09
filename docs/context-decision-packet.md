# Curated strategy decision packets

This isolated slice builds on the existing curator and strategic autopilot at
`b1af7086c5b5046bc46e22cf2ad5cb2b64e14491`. It does not modify the running
match, provider configuration, request limits, movement allowance, or gameplay
preferences. The model still receives only `submit_directive`; quiet turns still
make zero model requests.

## Implemented contract

The controller supplies current projected owned state, visible contacts, nearby
known terrain, and relevant available research/production options. A new bounded
`decision_packet` in the strategic request metadata supplies:

- Observed turn, projected-source label, idle owned city IDs, and whether a
  research choice is outstanding.
- Changes since the previous successful strategy request: newly/no-longer-owned
  entities, changed field names, newly/no-longer-observed contacts, changed own
  overview fields, and the number of newly known tiles. Quiet turns contribute
  through the next request's current observations. Disappearing contacts are not
  labeled destroyed; removed owned entities are not assumed killed.
- At most 16 IDs or changed entities per change category, with explicit omission
  counts. Current complete owned state remains in the existing context; historical
  values are not duplicated. The initial request has no prior comparison.

The previous request snapshot is copied before game actions and retained only
when the directive is valid. A format repair compares the same observation and
prior request; it does not advance the comparison boundary. This state remains
fresh-match-only and is not a checkpoint implementation. Packet metadata,
previous directive, movement authority, format repair metadata, and the current
context all share the existing total character budget. Required state overflow
aborts before requesting the model, rather than dropping owned state.

`option_sources` distinguishes `observed` (including genuinely empty results),
`not_requested_active_choice`, and `deferred`. Legacy cached availability reads
also distinguish unqueried choices and foreign/unobserved cities from observed
empty lists. Actual facade read errors still abort; unavailable accessors are
never relabeled empty. Production queries remain restricted to owned cities.

During scouting, the controller refreshes map before projected units/cities after
accepted or rejected action attempts. Scouting consumes only those fresh fields.
Economic overview and availability refreshes are deferred until the economic
pass, coalescing requests across scout actions. The overview remains explicitly
dirty meanwhile: rendering is refused and legacy cached reads request a refresh.
Movement, attacks, settlement and purchases invalidate economic catalogs because
native rewards or other action effects can change them. The economic pass queries
only idle queues/research or an explicitly focused owned city. Successful research
selection reads the observed active choice without re-fetching its unneeded full
catalog. Rejected research still refreshes alternatives.

This can change fallback choices when the old catalog was stale; that is a
freshness correction. Preference ordering, candidate scoring, movement rules and
action dispatch are unchanged. No probability calibration or forecast is added.

## Remaining read inventory

| Read path | Disposition |
|---|---|
| Model basic state discovery | Already eliminated by the base strategic controller; this slice adds no state tools. |
| Availability after successful research selection | Removed: the targeted fixture performs one catalog read instead of the baseline two. |
| Catalogs during sequential scout actions | Deferred; a two-action quiet-turn fixture performs zero catalogs during scouting and one research/one idle-city catalog afterwards. |
| Overview after scout effects | One fresh economic read after the batch; no added overview read per scout move. |
| Active production/research catalogs | Not queried by default; explicit source labels avoid treating omission as impossibility. |
| Map followed by entity projection | Retained: `FireTunerAdapter.observe(VISIBLE_MAP)` internally reads units/cities for derived visibility, then facade projected units/cities read again. Safely sharing these requires a separate snapshot/visibility contract and engine evidence. |
| Digest, mutation receipts, lease and closure reads | Retained under their existing authority; not model discovery calls. |

The next bounded optimization is an audited projected snapshot primitive binding
map, units and cities to one observation epoch. It must preserve map-before-entity
visibility, owning seat/lease, rejected-action invalidation, and replay semantics.
A generic cross-action cache would not establish these guarantees.

A future diplomacy inbox or commitment section can extend the bounded packet only
through an explicitly authorized seat-visible source and omission contract. This
slice adds no private messages, negotiation tools, commitments or deception rules.

## Verified findings

Repository-only focused checks cover accepted/rejected action invalidation,
deferred overview refusal, observed empty versus unqueried catalogs, foreign-city
query rejection, request-to-request delta boundaries, format repair within a
2,500-character fixture budget, unchanged quiet-turn request cadence, and existing
real-facade simulator replay and action/closure integration.

The exact commands and complete logs are retained under
`runs/context-packet-evidence-20260906/`. Final counts are recorded there in
`summary.json`; this is not a full repository release gate. Earlier development
logs are retained separately, including a mistyped test filename (no tests ran)
and two corrected Ruff findings.

## Upstream observability gap

The parent's retained opening audit for `minimax60-20260906T172847Z`
shows both seats preferring Pottery, Mining, then Animal Husbandry and building
Monument first despite different terrain. Egypt's 24 retained tiles were four
hills, 15 plains and five grassland; Sumeria's were seven desert, five hills,
three coast, three grassland and six plains. Both contexts provided only tile
coordinate, terrain, owner and city tags; research options contained IDs/costs,
and no city existed yet to supply production options at that request. The copied
processed audit is `runs/context-packet-evidence-20260906/opening-policy-audit.json`,
with the parent's event-prefix hash and sequence boundary. It is evidence about
those observed model preferences, not evidence that the openings were optimized
or that the model's internal cause is known.

The present packet cannot synthesize absent resources, yields, freshwater,
features, local prerequisites or economic rates. The next observability slice
should add only engine-supported, player-visible fields with explicit unknown
states. A later alternative comparison should record observed benefits,
assumptions, uncertainty and switch conditions against current feasible choices;
it should not present model prose or hidden reasoning as optimization proof.

## Follow-up probes

- Read-only review of this exact local commit before integration.
- Full repository pytest and Ruff on the eventual integrated head.
- A fresh controlled live run comparing request counts, availability/overview
  read counts, context sizes and completed turns. Smaller fixture read counts
  alone do not establish lower wall-clock time or better strategy.

## Blocked checks

Live/provider/desktop validation is excluded from this independent lane because
another controller owns the active match. No tuner connection, provider request,
service launch or desktop input was made for this slice. Integrate only after the
parent's current live stage ends and its run is preserved.

## Evidence gaps

No live speedup, browser rendering of the new packet, calibrated confidence,
forecast accuracy, diplomacy behavior, complete game, or consecutive acceptance
matches are established here. Packet growth can reduce optional terrain retained
within the fixed budget; required state still fails explicitly if it cannot fit.
