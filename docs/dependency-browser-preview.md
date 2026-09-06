# Standalone dependency browser preview

The viewer renders explicitly supplied
[observed-dependency projections](observed-dependency-projection.md) as one
self-contained HTML file. It is marked **OFFLINE · SYNTHETIC PREVIEW**, with a
visible statement that it is not current live model thinking. It does not read
the running game, connect to a model, or infer a strategic forecast.

## Use

The retained synthetic examples can be rendered from this isolated worktree:

```bash
PYTHONPATH=src /home/alexk/documents/civ-arena/.venv/bin/python -m civ_arena.catalog render \
  --projection runs/observed-dependency-projection-20260906/building_library.json \
  --projection runs/observed-dependency-projection-20260906/building_armory.json \
  --output /tmp/civ-dependency-preview.html
```

Open the resulting file in a browser. No server, package download, CDN, fetch,
public exposure or persistent process is required. `--projection` may appear up
to eight times; the selector switches only among those supplied artifacts. It
cannot load arbitrary files or silently recompute a different query/root.

The pure API is `civ_arena.catalog.viewer.render([projection, ...]) -> str`.
The extractor, catalog version 2 and projection version 1 remain unchanged.

## What the page shows

- A graph of source prerequisite and other relationship edges. Arrows point
  from subject to relation target. Node selection shows supplied observations,
  source declarations/attributes, edge provenance and unresolved references.
- Explicit legends distinguish source facts, supplied observations and unknowns.
  Observed true and observed false use the same observation color and display
  their literal boolean value. Neither color means an action is ready.
- Unverified prerequisite connectives remain explained alongside each relevant
  group. Source modifier requirement sets are inspectable but unevaluated.
  Persistent panels retain effective-rule and city/action uncertainty.
- Dashed frontier targets represent source edge endpoints outside the supplied
  depth. They receive no fabricated node attributes or observations. Selecting
  one shows only the boundary relationship evidence.
- Node/edge keyboard inspection, pointer panning, zoom buttons/wheel and a fit
  control support exploration. Small screens stack the inspector beneath the
  graph; zoom and pan are available when a fitted graph's labels are small.
- The artifact panel exposes the exact original projection and its projection,
  source-catalog and observation digests. Each embedded artifact roundtrips
  unchanged; layout and selection never change its facts or hashes.

The layout is a deterministic layered preview, not an execution schedule or
optimized causal plan. Other relations and same-layer dependency arrows can
cross. Large-graph readability and assistive-technology coverage need further
assessment; no full accessibility or device-matrix claim is made.

## Custody and input handling

Before rendering, the module checks projection version/scope, canonical digest,
required unknown labels, graph endpoint identities and bounds. Digest checks
only detect changes relative to the supplied digest; they do not authenticate
the source or caller's player-scoped observations.

Source labels and provenance are rendered with DOM `textContent`; they do not
become HTML, script, CSS, filesystem links or event-handler source. Embedded JSON
escapes `<`, `>` and `&`, preventing a label from closing its data-script block.
The executable script is fixed packaged code, authorized by its SHA-256 Content
Security Policy hash. The policy denies network connections and all other
resources by default. There are no external resources or dynamic network calls.

Only explicit projection paths and the package's fixed HTML/CSS/JS assets are
read. CLI duplicate JSON keys reject. Invalid input fails before writing the
output file. The renderer accepts at most eight artifacts, each up to 2 MiB,
with an 8 MiB canonical bundle and 12 MiB HTML limit. Each graph is limited to
512 source nodes, 2,048 edges and 1,024 total source/frontier node identities.
Oversized inputs reject without silently dropping evidence.

## Verified repository evidence

The focused catalog, projection and viewer gate passed **71 tests, zero failed
or skipped**, in 0.69 seconds. Ruff passed. The 15 viewer cases cover exact
embedded artifact custody, deterministic HTML, independent CSP hash binding,
source-label/script injection, frontier preservation, digest tampering,
malformed contracts, duplicate identities, unverified connective requirements,
bounds, multi-artifact CLI rendering and refusal preserving existing output.
An initial lint check found one long test regex; the corrected invocation passed.

## Verified browser checkpoint

System Python Playwright launched `/usr/bin/google-chrome` headlessly in a fresh
isolated context, opened only the generated local preview and injection fixture,
and closed that context/browser afterwards. It did not access the physical
browser, connected monitor, live game, tuner or provider.

**Eight interaction/security check groups passed** at 1440×1050 and 390×844:
node observation/evidence inspection; zoom/fit/pan; Armory example and keyboard
edge inspection; artifact/digest inspection; two depth-zero frontier targets;
no horizontal page overflow at 390 pixels; hostile labels remaining literal
text; and zero page/console errors with zero HTTP(S) page requests. The request
recorder observed exactly two explicit `file:` navigations. This is page-level
network evidence, not a machine-wide traffic audit.

The first two browser probe attempts failed on harness assumptions: Armory's
rendered count includes a frontier target beyond its 11 source nodes, and JSON
inspection escapes quotes inside a literal label. Those assertions were
corrected without changing viewer behavior; full failed logs remain preserved.

Evidence directory: `runs/dependency-browser-preview-r2-20260906/`.
It contains `preview.html`, `injection-fixture.html`, `frontier.json`,
`browser-summary.json`, captured commands/logs, and screenshots `desktop.png`,
`frontier.png` and `mobile.png`. The checked preview HTML SHA-256 is
`07e89ea74691b1bbefee4b2fd453b389dadf20fe6e6579289a38b01b65ec83fb`.
Five example choices cover Library, Armory, Archer, Craftsmanship and the Library
depth-zero frontier. The source projections are synthetic observations over the
retained installed base catalog. Desktop/mobile screenshots were visually
inspected in addition to the automated checks.

## Follow-up and evidence gaps

Independent exact-head review remains pending for this new viewer. A user can
open the static artifact after review; no server was left running or public URL
created. No live-model context consumption, authenticated observation transport,
active-ruleset feasibility, battle planning, action forecast, calibrated
probability or strategic improvement is established. The local full repository
release gate remains separate from these focused checks.
