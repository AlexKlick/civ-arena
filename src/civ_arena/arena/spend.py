"""Durable per-request spend ledger (``spend.jsonl`` in the run dir).

One fsync'd line per counted model attempt. It lives OUTSIDE the event log
on purpose: resume truncates the event log to the checkpoint prefix — the
mechanism that rolls back incomplete turns — and would rewind any spend
recorded there. Money spent is not game state; it must survive the rewind.
Counts per player id; restore takes the max with the checkpointed counter
(the file is always at least as current as the last checkpoint).
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class SpendLedger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def note(self, agent_id: str, player_id: int) -> None:
        line = json.dumps({"agent_id": agent_id, "player_id": int(player_id)},
                          sort_keys=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def counts(self) -> dict[str, int]:
        """Per-player-id attempt counts; a torn trailing line is ignored."""
        if not self.path.exists():
            return {}
        out: dict[str, int] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = str(doc.get("player_id"))
            out[key] = out.get(key, 0) + 1
        return out
