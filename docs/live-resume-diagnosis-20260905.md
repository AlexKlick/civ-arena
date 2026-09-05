# Live resume abort diagnosis, 2026-09-05

The run stopped honestly after six completed seat turns, but the immediate
trigger is a confirmed entity-identity mismatch in movement-allowance accounting.
The evidence does **not** establish that the reported movement belonged to a
foreign unit. This remains a failed run, not a clean three-round validation.

Examined source: `30fef52a304f473706d5fb7906079469ebd47597`, matching the run's
recorded clean source identity. No game, tuner, provider, or desktop actions
were performed for this diagnosis. Original artifacts remain unchanged.

## Confirmed evidence

- [events.jsonl](../runs/minimax-resume-20260905T004340Z/events.jsonl), sequence
  312, grants player 1 turn 3. Sequence 369 requests movement of `u196609` to
  axial `10,19`; sequence 370 accepts the request with engine detail `10,23`.
- [wire.jsonl](../runs/minimax-resume-20260905T004340Z/wire.jsonl), physical
  lines 370–372, records that request, an unchanged digest, and an empty
  commanded diff. The response proves the request was submitted; it does not
  prove that movement had completed.
- Wire line 380 still has raw engine unit `u131073`, owner 1, at engine
  coordinates `10,23`, with 2 movement. Line 385 dumps its end-path ledger:
  position `10,23` → `11,25`, movement 2 → 0. Line 386 observes the player 1
  warrior as projected ID `u196609`, axial `11,20` (engine `11,25`). The player
  0 warrior remains at engine `47,21`, projected ID `u131073`.
- The mod's [`diff_player`](../mods/PuppeteerMod/PuppeteerMod.lua) emits raw
  engine IDs (`"u" .. uid`). [`units_read`](../src/civ_arena/game/civ6/lua_translator.py)
  emits `raw_id + owner * 65536`. [`end_phase`](../src/civ_arena/game/civ6/firetuner.py)
  parses ledger IDs without translating them. The referee's
  [`_declare_own_endpath_drift`](../src/civ_arena/arena/referee.py) joins those
  ledger IDs directly to projected observation IDs.
- Consequently, player 1's ledger `u131073` resolves to player 0 in the
  observation ownership map. Event 382 admits zero movement rows, then events
  387/388 report the two violations. Player 0's earlier end-path movement
  passes this join because owner-zero raw and projected IDs coincide.
- Event 390 and [summary.json](../runs/minimax-resume-20260905T004340Z/summary.json)
  agree: `clean=false`, 3 completed rounds, 6 seat turns, 2 violations,
  cleanup completed, no active logical lease. The terminal digest is explicitly
  cached; it is not a fresh post-movement or shutdown engine observation.

An offline reproduction runs the real response parsers and the real referee
allowance method against wire lines 385/386. It reproduces zero acknowledged
rows and the incorrect ownership join. Full captures:

- `/tmp/civ-live-resume-diagnosis-probe.log`: selected event and wire evidence,
  physical wire line numbers, examined HEAD.
- `/tmp/civ-live-resume-allowance-repro-r2.log`: successful parser/referee
  reproduction. The first probe log, `/tmp/civ-live-resume-allowance-repro.log`,
  records an import-path error and provides no validation evidence.

## Limits and corrective scope

The recorded ledger and coordinates establish which owned unit moved. They do
not establish why the engine chose `11,25`, which differs from the requested
destination, or the exact timing of queued movement versus end-turn processing.
That behavior needs a separate bounded engine probe; it must not be described
as proven intended movement. Under the configured allowance, own-unit end-path
position and movement rows are nevertheless eligible for declaration.

Normalize entity identity consistently before the ownership check, with
behavioral coverage for identical raw IDs across owners and for genuine foreign
movement remaining rejected. Do not simply broaden the movement allowance.
The existing additive projection is also non-injective when raw IDs exceed
65535: `(raw=131073, owner=0)` and `(raw=65537, owner=1)` both encode `u131073`.
The run proves large raw IDs exist, but not that this particular pair coexisted.
A general repair needs an unambiguous owner-qualified identity or validated
encoding bounds across observation, command, ledger, and parser boundaries;
changing ledger formatting alone cannot establish that guarantee.

Preserve this failed run. Any repaired implementation requires local regression
checks and a fresh live attempt; no retrospective clean-run claim or
automatic resume follows from the diagnosis.
