# Framed unit observations (default off)

This repository-only candidate adds request-correlated observation collection for the exact `lua_translator.units_read()` builder. `FireTunerAdapter(..., framed_units=False)` preserves the existing default. Passing the explicit boolean `True` routes `ObserveKind.UNITS` and the units component of `VISIBLE_MAP` through the new helper. No launcher or match configuration enables it. Native activation is blocked pending the stopped-game probes below.

`execute_read` selects the GameCore VM; its name does not establish read-only authority. Arbitrary `read_raw`/`write_raw`, all actions, restore/freeze, reward/ledger/digest, production verification, popup handling, startup and handoff retain their existing transport. Cities, terrain, overview and research/production catalogs also retain their existing transport. No vendored file, mod, visibility projection, simulator, provider or active-run setting changes.

## Protocol and failure contract

`FramedUnitObservations.read_units(timeout=5.0)` accepts no caller Lua or state index. It uses the existing connection's lock and already-discovered unique `GameCore_Tuner` state. Its deadline includes lock acquisition, write/drain and complete collection. It neither connects nor discovers states. It sends once, with a fresh random epoch and monotone request number, and does not use the legacy reconnect/retry wrapper or either fixed idle drain.

The exact units builder runs inside a synchronous Lua closure with lexical `local print`. Captured records use UTF-8 byte lengths, then bounded hex chunks tagged with protocol, request token, state index and chunk index. The END frame binds chunk, byte and record counts. A pre-existing global function retains its global print: unrelated hook text and its generic sentinel are unbound diagnostics, not unit rows. `_G.print` remains unchanged. Missing required Lua capabilities or a captured runtime failure produce a fixed-category correlated ERR, without partial DATA emission.

The Python reader enforces an eight-byte Nexus header, 1 MiB maximum message, final NUL, strict UTF-8 and the exact output envelope/context. It accepts separate or combined protocol lines and fragmented TCP bytes. It requires ordered unique chunks, exact bounded terminal counts, exactly one units header and final builder sentinel, valid qualified unit IDs and explicit native `is_barbarian` booleans. Historical parser compatibility remains available through the default legacy path. Active-token frames following END within the same native output message fail. Later messages bearing the completed request's old token are quarantined on the next request; the reader does not wait after completion merely to prove future silence.

The captured payload is limited to 256 KiB and 8,192 records; each hex chunk represents at most 384 bytes. Unbound output is limited to 256 lines/ACKs and 64 KiB per exchange. Only counts and the first eight SHA-256 hashes are retained for that output. Generic terminators, stale tokens and other-VM messages cannot complete the request. Active-token wrong-state output, native ERR, malformed envelopes, impossible counts, unknown active frames and excessive unsolicited output fail explicitly.

After any attempted write, timeout, cancellation, EOF or parsing failure permanently poisons the helper and requests closure of only the captured writer. No partial units, automatic replay or legacy fallback are returned. A close exception is recorded without masking the original failure. Cancellation while waiting for the lock sends nothing and leaves the current owner intact. A changed stream/state generation or replaced adapter connection is not adopted. The surrounding legacy connection still has its existing reconnect behavior for unrelated legacy operations; this candidate does not redefine cleanup or recovery. An explicit later session needs a new connection/helper and fresh discovery.

Each successful read constructs new parsed unit dictionaries; caller mutation cannot change another result. Transport nonce, timings and hashes remain outside observations and simulator/replay hashes. Returned units remain omniscient at the adapter seam; existing referee visibility projection remains responsible for player scope.

A synchronous, nonblocking optional `framed_units_audit` callback receives an independent, bounded diagnostic dictionary per attempted exchange. `last_exchange` also returns a copy. Callback exceptions set a diagnostic failure flag without changing the read result; these diagnostics are not promised durable. The existing `TapConnection._locked_execute` wire hook does not cover this direct framed path. Any future native proof must separately retain the callback and raw native-envelope evidence; it must not infer complete wire coverage from the old tap alone.

## Verified findings

The isolated fake-byte and local `texlua` tests exercise exact translated units, global native-style printing, capability/runtime/capture-bound refusal, fragmented/combined frames, stale output, strict counts/IDs/native classification, timeouts and cancellation during lock wait/send/receive, generation replacement, copy isolation and default-off adapter routing. Local Lua execution is not execution in Civ's embedded VM.

Full captures are under `runs/framed-units-implementation-20260906/`; final counts and immutable source binding are recorded in `summary.json`. The settled affected gate passed 184 tests with zero failures, errors, skips or deselections; 73 are new framed-path cases. Ruff passed for all three changed Python files. The focused and affected commands are:

```bash
PYTHONPATH=src /home/alexk/documents/civ-arena/.venv/bin/python -m pytest -q tests/test_framed_observations.py tests/test_firetuner_wire.py tests/test_live_entity_ids.py tests/test_live_map_frame.py tests/test_native_nonmajor_units.py tests/test_terrain_fidelity.py tests/test_restore_completion.py > runs/framed-units-implementation-20260906/affected-redaction-final.log 2>&1
/home/alexk/documents/civ-arena/.venv/bin/python -m ruff check src/civ_arena/game/civ6/framed_observations.py src/civ_arena/game/civ6/firetuner.py tests/test_framed_observations.py > runs/framed-units-implementation-20260906/ruff-redaction-final.log 2>&1
```

Initial new-test failures are preserved: `focused-initial.log` had 56 passed/2 failed and one warning because the fake legacy reader lacked the map timeout keyword and its cancellation fixture returned an unawaited coroutine. `focused-tightening.log` had 62 passed/2 failed because the fixture expected PLAINS for normalized GRASSLAND. These were new fixture defects, not reviewed baseline failures. `focused-final.log` then had 64 passed, before adding eight stale-output/deadline/argument regressions. The first affected gate passed 183 tests (`affected-final.log`). A source review then identified that chained parser errors could expose an invalid wire row in formatted tracebacks; error chaining was suppressed and a privacy regression added before the settled 184-test gate. `ruff-initial.log` records two new test-only style findings corrected before the final Ruff check.

## Follow-up probes

Independent exact-commit review should verify the two allowed adapter call sites, strict frame lexer, cancellation ownership, lack of replay/fallback, parser equivalence and unchanged vendored bytes. A later stopped-game validation window must prove the actual `CMD` lexer accepts the wrapper, exact raw output context/state/NUL/tag conventions, required Lua builtins and `PlayerManager.GetAlive`, native error shapes, non-ASCII output, chunk/record limits and mixed GameCore/InGame unsolicited messages using one task-owned tuner client.

Then compare framed and legacy units in a stable leased state and verify freshness after accepted/rejected movement and asynchronous production. A framed terminal proves synchronous execution of this units getter only; it does not prove completion of a previously queued mutation. Existing action/digest/production-verification paths remain legacy. Native admission must fail or remain disabled when the required behavior is unsupported; an ambiguous attempted framed request must never fall back.

## Blocked checks

No native tuner, game input, provider request, browser action or active-run change is authorized in this lane. These native lexer/envelope/freshness checks therefore remain blocked until a separately authorized stopped-game window. The option remains false by default. The full repository release suite and live activation are outside this focused candidate gate.

## Evidence gaps

No native latency reduction, game-state equivalence, match reliability improvement or mutation-completion guarantee has been established. The earlier fake transport experiment in `runs/framed-transport-design-20260906/` demonstrated removable idle-wait overhead on simulated streams only; its timings are not native measurements. Event logs remain match authority; framed diagnostics cannot replace turn/lease/allowance accounting.
