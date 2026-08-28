# Vendored from `lmwilki/civ6-mcp`

- **Upstream**: https://github.com/lmwilki/civ6-mcp (MIT — see upstream LICENSE)
- **Commit**: `dd2019056371b92ea4854e879ddf05a8cad95e8a` ("Bump version to v1.1.11")
- **Files vendored**: `src/civ_mcp/tuner_client.py` → `tuner_client.py`,
  `src/civ_mcp/connection.py` → `connection.py`
- **Local reference checkout**: `~/documents/civ6-mcp` (shallow clone at the
  commit above)

## Exact local diff

1. `connection.py`: the import block
   ```python
   from civ_mcp import tuner_client
   from civ_mcp.lua._helpers import SENTINEL
   ```
   was rewritten to
   ```python
   from . import SENTINEL, tuner_client
   ```
   plus this provenance note added to the module docstring. No other lines
   changed.
2. `tuner_client.py`: unchanged (verbatim).
3. `SENTINEL = "---END---"` (verbatim from `civ_mcp/lua/_helpers.py`) lives in
   `__init__.py`.

## Why vendor instead of depend

Upstream's package hard-requires `mcp>=1.20`, `fastapi[standard]>=0.128`,
and `anthropic>=0.84.0` — none of which are used by these two files. The
spike depends on neither the MCP stack nor a model SDK; the wire layer is
~415 lines of stdlib asyncio. When the live FireTuner leg lands, revisit:
if upstream ever splits a `civ6-wire` package (or grows optional extras),
switching the import in `game/civ6/firetuner.py` back to a real dependency
is a two-line change.
