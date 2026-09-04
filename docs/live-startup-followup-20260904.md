# Live startup follow-up — 2026-09-04

The current gaming display is a verified startup blocker. This follow-up
does not establish the sole cause of the earlier failed launch, successful
Steam authentication, or any completed live seat turn. No fresh game was
launched; the deterministic rehearsal, MiniMax smoke, and both 30-round
acceptance runs remain pending.

Work began from `5a8611ead13a71f50838fc530b6afb259bd3181e` on local branch
`fix/hotseat-reliability-20260904` in the isolated reliability worktree.
The previous failed run `rehearsal-20260904T214144Z` and its save backups
remain preserved. No source, save, or run in the original checkout was changed.
All 22 prior save backups still match the current saves by SHA-256:
[preservation inventory](../runs/live-startup-followup-20260904/preserved-saves.json).

## Verified host observations

- The task-owned `headless-gaming.service` runs as `alexk`, with xinit PID
  1126271 and Steam PID 1128240. Its X server uses
  `/etc/X11/xorg-local-gaming.conf` on display `:1`.
- `/run/gaming-session-mode` is a root-owned file containing `local`.
  The session launcher gives that file precedence over display detection;
  it offers no environment or command-line mode override.
- XRandR reports a 640 by 480 root screen, every output disconnected, and
  zero active monitors. Steam's bundled SDL3 successfully initializes X11
  but enumerates zero displays.
- Steam's current startup log records failure to create its login window:
  `Could not find display info`. No successful logged-on observation appears
  after that restart. Network connectivity checks did succeed.
- App 289070 is installed with manifest `StateFlags=4`; this is installation
  metadata, not launch or gameplay proof. There is no current Civ6 process,
  visible Civ6 window, or listener at `127.0.0.1:4318`.

[Captured host diagnosis](../runs/live-startup-followup-20260904/host-diagnosis.json)
contains commands, return codes, timestamps, and selected diagnostics without
credential values. [SDL probe](../runs/live-startup-followup-20260904/sdl-readonly.log)
used the same bundled SDL3 library that Steam provides.

A reversible, unprivileged `xrandr --setmonitor` probe advertised a temporary
monitor backed by no output. XRandR then listed one monitor, but SDL3 still
enumerated zero displays. The probe monitor was removed successfully, restoring
zero monitors. It is not a viable demonstrated repair:
[complete probe and cleanup log](../runs/live-startup-followup-20260904/virtual-monitor-probe.log).
No key, mouse, tuner, or game-launch action was sent by this investigation.

## Repository change and checks

The launcher now requires an active XRandR output before restarting X or
touching the game, then checks again after `--fresh-x`. It accepts physical,
headless, and virtual outputs with active geometry; it does not require a
physical monitor. A root-screen size or a named monitor without an active
output does not establish readiness.

Each Architecture-1 display check retains unique JSON diagnostics in the
startup folder. A failed display check passes through the existing terminal
record path before game kill/launch. The helper is bounded to 10 seconds;
missing xrandr, nonzero exit, and timeout fail explicitly. No host mode or
permission is changed automatically.

- Focused regression suite: **17 passed, 0 failed, 0 skipped** in 0.11s:
  [full log](../runs/live-startup-followup-20260904/launcher-tests-final.log).
- Ruff on the two changed Python files: **passed**:
  [full log](../runs/live-startup-followup-20260904/ruff-final.log).
- Read-only invocation of the new guard on the current host: **failed as
  expected**, with the unavailable-display reason and no subsequent action:
  [full log](../runs/live-startup-followup-20260904/live-display-preflight.log).

These checks prove the early failure behavior, not a successful live startup.
The combined release gate is recorded separately by the coordinating workstream.

## Required host preparation and next stage

The installed supported helper reprobes the connectors, selects local or
headless mode, writes the runtime mode file, and restarts the gaming service:

```bash
/home/alexk/.local/bin/gaming-mode
```

That helper invokes sudo for connector reprobe, the root-owned runtime mode
file, and service restart. This agent's noninteractive sudo probe failed with
`a password is required`; no password was requested, read, or supplied. The
helper must be run in a terminal with the necessary host privileges. Restoring
the display does not guarantee Steam authentication or a successful Civ6 launch.

After host preparation, verify active XRandR output, Steam readiness, task
ownership, and absence of another tuner client. Then use a unique run ID and
the existing staged command in [live validation](live-validation.md#follow-up-probes):
three deterministic rounds, fresh three-round MiniMax smoke, then separate
fresh 30-round acceptance A and B. Stop at the first failed stage and preserve
its artifacts. The startup/play/agent/recovery/cleanup limits and provider
configuration, request caps, mod bytes, and movement-drift allowance are
unchanged. No provider request was made in this follow-up.
