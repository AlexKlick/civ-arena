# Public development branches — 2026-09-08

This public repository preserves the local Civ VI Arena development branches.
`master` remains the earlier integrated control-plane base; newer experiments
are published separately and are not implicitly merged by publication.

| Branch | Latest work |
| --- | --- |
| `m4v/observer-20260908` | Spectator-world viewer consumption |
| `feat/s3-economic-forecast-20260907` | Economic forecast and round-four regression hardening |
| `feat/cap-integration-20260907` | Capture, validation/provenance, and decision telemetry integration |
| `feat/observer-readability-20260907` | Observer integration before the M4 split |
| `wip/civ-arena-m4-capture-20260908-20260908` | Snapshot of uncommitted M4 capture work |
| `wip/civ-arena-20260908` | Snapshot of uncommitted changes in the original master checkout |
| `wip/civ-arena-cap-integration-20260907-20260908` | Snapshot of uncommitted review findings |

All other local development branches are retained. WIP snapshots preserve work
without asserting that it passes integration or native-game validation. Source
worktrees and indexes were not changed by snapshotting.

The vendored FireTuner wire code carries its upstream MIT license at
`src/civ_arena/game/civ6/vendor/LICENSE`. The repository contains arena/mod
source, not a copy of Civilization VI or its game assets; running native
matches requires your own game installation and configured local services.

Historical documents contain operator-specific paths, local service endpoints,
and references to run artifacts. Configure your own environment. Ignored live
run directories, virtual environments, credentials, and local model data are
not included. Local tests are the validation authority; publishing source is
not proof of native gameplay, model quality, or browser behavior.
