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

Repository verification at source/test commit
`51cd3bcefedd78901b08d7d3cec512ef546dd066`: **640 passed, 0 failed, 1 skipped**
in 512.09 seconds. The full suite ran once; Ruff passed. Only documentation
changed during and after this gate. No flaky/rerun plugin was requested.

- [Complete pytest log](../runs/spectator-popup-20260904T230258Z/pytest-release.log)
- [Complete Ruff log](../runs/spectator-popup-20260904T230258Z/ruff-release.log)
- [33 popup-helper checks, including executable Lua fixtures](../runs/spectator-popup-20260904T230258Z/popup-focused.log)
- [45 focused driver/UI checks before the final success-path quiesce adjustment](../runs/spectator-popup-20260904T230258Z/integrated-focused.log)

The final quiesce adjustment is included in the full gate. A separate read-only
review found no confirmed blocker in the controller/driver lifecycle; this is
repository proof and does not waive the live preflight below.

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

Live stage status: **BLOCKED** at provider tool preflight. Two bounded attempts
used the unchanged MiniMax client, configuration and strict echo predicate.
The first made one POST and failed its required tool-call predicate. Its probe
retained only the exception type, so its response model, content and usage are
unavailable. The original script and log remain preserved:
[first probe](../runs/spectator-popup-20260904T230258Z/provider-preflight.log).

A separately retained diagnostic attempt made one additional POST. Its client
reported `MiniMax-M3`, 24 input tokens and 8 output tokens, `end_turn`, one text
block and zero tool calls. It failed the same predicate:
[diagnostic](../runs/spectator-popup-20260904T230258Z/provider-diagnostic.log).
This proves that the diagnostic response omitted the requested call; it does
not establish an authentication failure, provider outage, or the first
response's contents. No further requests or live launches followed.

Total provider requests in this follow-up: two. No fresh game episode was
opened. The restored headless session and streaming connection were preserved;
the staged rehearsal, MiniMax smoke and both 30-round acceptance runs remain
unproven. Source and host readiness do not replace those gates.

## Evidence gaps

There is not yet a live captured tech/civic popup dismissal from this revision.
Sunshine connection evidence is host stream evidence; viewing through the
user's Moonlight client is outside the repository gate. Later run outcomes
must record these boundaries explicitly.

The failed first provider response cannot be reconstructed from its exception
type. No successful tool-result acknowledgment was reached by either attempt.
The [source/config/mod and installed-UI inventory](../runs/spectator-popup-20260904T230258Z/custody-final.json)
binds eight repository files and nine installed UI sources; these byte hashes
are source custody, not a live callback execution receipt.
