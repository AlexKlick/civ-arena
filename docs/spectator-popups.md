# Watching hotseat matches in Moonlight

2026-09-04. The hotseat driver now owns an informational-popup watcher as part
of its match lifecycle. During active agent turns it checks every five seconds,
including while the model is answering. During stalled handoffs it participates
in the existing recovery sweeps and deadlines. It uses the same tuner connection
and UI lock as the driver; it does not start another keyboard/click loop.

| Observed context | Normal callback |
|---|---|
| TechCivicCompletedPopup | OnClose |
| BoostUnlockedPopup | OnClose |
| EraCompletePopup | OnClose |
| NaturalWonderPopup | OnClose |
| WonderBuiltPopup | OnClose |
| GreatWorkShowcase | HideScreen |

The first five live under `/InGame/WorldPopups`; the showcase lives under
`/InGame/Screens`. These paths and handlers were read from the installed
`steamassets/base/assets/ui/ingame.xml`, popup Lua files, `greatworkshowcase.lua`,
and the expansion era-popup replacement. The generic tech-popup `Close()`
clears its queue, so the watcher uses `OnClose()` to advance one notice at a
time. Wonder/era handlers also perform their normal popup-manager unlocks.

Each close rechecks the exact context ID and visibility inside that context's
Lua state. Missing or ambiguous state, incomplete observations and missing
callbacks fail explicitly. All-six-contexts-missing is a failure, not evidence
of an unobstructed map. Great-person recruitment, diplomacy, world congress,
government/research choices, generic dialogs and historic-moment screens are
outside this allowlist. Existing handoff keyboard/banner recovery remains in
place; the new watcher itself sends no desktop keys or clicks.

## Verified findings

The restored task-owned headless session has an active 2944×1840 output and
Steam's current startup has a successful logged-on observation. Sunshine
reported an active streaming client. These are host observations, not proof
of which frames the viewer saw:
[host preflight](../runs/spectator-popup-20260904T230258Z/host-preflight.json).

Source and executable-Lua fixture checks cover the exact paths, normal
callbacks, queue handling and duplicate-token protection. A close is sent once
through the connection's locked transport; transport failures are not retried.
A bounded context-local token receipt remains defensive protection against a
duplicated request. Fake games skip both wire and desktop input.

A full scan/one-close operation is bounded to ten seconds. A still-visible
same-context queue is bounded to eight closes and 180 seconds. A recovery close
also belongs to the existing eight-sweep/180-second transition budget and is
followed by an engine poll before further input. Visible-after-close can mean
another queued notice; only observed hidden state counts as a dismissal.

`HEARTBEAT` events with `audit=popup_check` retain observed outcomes. The terminal
summary includes `informational_popups` counters. A failed watcher cancels the
active turn through the driver's terminal path. Successful completion drains
an in-flight check, then stops the watcher; abort cleanup starts no new checks.
All clocks stay outside deterministic simulator hashes.

## Follow-up probes

After the repository checks, launch a fresh Architecture-1 deterministic
three-round rehearsal. Keep the current young X session when possible so the
Moonlight stream is not interrupted. Then advance through fresh MiniMax smoke
and acceptance A/B only if the preceding stage passes. Retain real popup and
handoff observations from the single driver connection. Installed-source and
Lua-fixture checks alone do not establish live callback behavior.

The launcher and stop/preserve commands remain in [live validation](live-validation.md).
Use new run IDs and preserve startup save backups. Provider configuration,
request caps, mod bytes and the documented movement allowance are unchanged.

## Blocked checks

At implementation start, no Civ6 process or tuner existed yet. The two
30-round acceptance runs remain unproven. Source and host readiness do not
replace the fresh rehearsal, smoke and acceptance gates.

## Evidence gaps

There is not yet a live captured tech/civic popup dismissal from this revision.
Sunshine connection evidence is host stream evidence; viewing through the
user's Moonlight client is outside the repository gate. Later run outcomes
must record these boundaries explicitly.
