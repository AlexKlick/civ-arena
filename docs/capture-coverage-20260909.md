# Capture coverage matrix — lane B (2026-09-09)

NEXT-05 deliverable: turn a run's `spectator_world` events into the
field-family coverage table the sealed-recording qualification needs
([dataset-evidence-card §6](dataset-evidence-card.md#6-qualification-procedure-for-a-sealed-recording--not-yet-run)).
Producer: `scripts/capture_coverage.py <run_dir> [-o out.json] [--md out.md]`;
pins: `tests/test_capture_coverage.py`, `tests/test_qual_configs.py`.

## Field families

One row per (entity class × family); `accessor` names the read that
produces the family (wire headers from
`src/civ_arena/game/civ6/world_capture.py` / `lua_translator.py`):

| family | accessor | world payload | admitted keys counted for `unsupported_or_null` |
|---|---|---|---|
| roster_identity | `SPECW\|1 roster_read` | `world["roster"]` | player_id, civ_name, leader, kind, suzerain, is_major, is_barbarian, alive, **level** |
| economy_players | `OVX\|2 overview_read` | `world["players"]` | Amendment-2 player set (gold, gold_per_turn, science, culture, faith, upkeep, era, researching, researched, civics) |
| cities | `CITIES\|2 cities_read` | `world["cities"]` | Amendment-2 city set (population, is_capital, is_major, hp, max_hp, production_queue, buildings, districts, …) |
| owned_tiles | `SPECW\|1 owned_tiles_read` | `world["owned_tiles_columns"]` | q, r, terrain, feature, resource, improvement, district, river, city |
| palette | `SPECW\|1 palette_read` | `world["palette"]` | primary, secondary + **`palette_confirmed: false`** (+1 per event) |
| game_era | `OVX\|2 overview_read` | `world["game_era"]` | presence gate (absent ⇒ zero contributing events) |
| grid | `SPECW\|1 owned_tiles_read` (GRID row) | `world["grid"]` | w, h |
| fog_audit | `DRIVER fog_audit_for` (driver-side, not a wire read) | `world["fog_audit"]` | requested, engine_visible, engine_not_visible, unavailable, disagree_coords |

`unsupported_or_null` counts an admitted key the payload does not
support: **absent OR explicitly `null`** (the producer drops None-valued
optional keys — `world_capture.package`, so a null on the wire is a value
nobody read). A real `0`, `false`, `""` or `[]` is supported data and is
never counted.

Row fields: `accessor`, `context` (that event's `world["contexts"]`
value; `unrecorded` where the world doc records no transport for the
family), `observed_vs_derived`, `sampling_time` {first_ts, last_ts},
`source_cursor` {seq_min, seq_max, events}, `read_ms` {min, median,
max}, `unsupported_or_null`, `truncated` (carried from the LATEST
contributing event), `verifying_probe` (pinning test + live read-check
script, both asserted to exist by the tests).

## How entity classes are NAMED

Classes come ONLY from `world["roster"][i]["kind"]` — the DERIVED
PLAYERROW field (`IsBarbarian` → barbarian, else `IsMajor` → major, else
city_state; `world_capture.roster_read`). There is no other naming
source: economy rows, cities, territory columns and palette entries are
attributed to a class by resolving their pid through the SAME event's
roster, and a pid that event's roster does not name is attributed to no
class. `free_city` is emitted for symmetry with the arena class model
but no producer can ever name it (the strict parser rejects the kind), so
in any **wire-conforming** recording it is a permanent GAP row — present,
zero, never inferred. It is not zero by construction: a hand-edited or
corrupt log that names `free_city` is counted as observed AND reported in
`errors` (below), so the discrepancy is visible rather than silently
absorbed. A roster row that DOES name a kind outside the wire-admitted set
(`major`/`city_state`/`barbarian` — `world_capture.parse_roster`) is
recorded in the matrix's top-level `errors` list and in the markdown's
`## Errors` section: an explicit coverage error, never a silent
no-class attribution. Board-global
families (game_era, grid, fog_audit) carry no class dimension: one
aggregation, reported for every OBSERVED class; unobserved classes keep
zero rows everywhere.

## Rehearsal evidence (offline, `--fake`)

Config: `configs/live-hotseat-spectator-003-rehearsal.yaml` — the
identical `match:` block of `configs/live-hotseat-spectator-003.yaml`
(same seed 271183, `spectator_capture: true`) over the
planner/turtler pair from `live-hotseat-001.yaml`, because the fake
tuner fakes the TUNER, not the provider.

- Log: `runs/qual-laneB-rehearsal.log`; run dir:
  `runs/qual-laneB-rehearsal/live-hotseat-spectator-003-rehearsal/`
  (both git-ignored).
- Command: `HOME=/home/alexk PYTHONPATH=src .venv/bin/python -m
  civ_arena.game.civ6.live_driver
  configs/live-hotseat-spectator-003-rehearsal.yaml --phase
  dispatch-hotseat --fake --turns 3 --runs-root runs/qual-laneB-rehearsal`
- Result: `summary.clean == true`; `python -m
  civ_arena.game.civ6.validate_run <run-dir> --rounds 3 --allow-fake`
  → **PASS (exit 0)**; `after_seat` sequence of the 7 `spectator_world`
  audits: **[-1, 0, 1, 0, 1, 0, 1]** (baseline + one per completed seat
  turn; shape pinned by `tests/test_live_hotseat.py:128-166`).
- Coverage over the rehearsal dir: all 8 families present, classes
  named by the fake roster.

## Standing gaps

1. **Unobserved entity classes** — whatever the run's roster never
   named (in the rehearsal: `city_state`, `barbarian`, `free_city`)
   stays a zero-count GAP row; the matrix never infers a class from
   economy/city/territory/palette pids.
2. **`palette_confirmed: false`** — the world doc ships it false by
   design (Amendment 3 item 9); the viewer trusts palette ints only
   when the `live_capture_check` packing follow-up flips it.
3. **Spectate-phase world carrier UNVERIFIED** — this matrix covers the
   hotseat `spectator_world` audit only. The spectate snapshot's world
   block (`world_capture.spectate_world`: read transport only, no
   palette, no extended cities) has no live proof; per-family `context`
   values for that carrier are unrecorded.
4. **Ruleset** — `base_source_catalog — effective ruleset unverified`:
   the publisher emits provenance from the run's own `summary.json`
   identity (empty when absent) and never asserts which ruleset the
   engine actually ran.
