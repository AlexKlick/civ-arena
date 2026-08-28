"""Checkpoints: sim + RNG + coordinator state, verified against the log prefix."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from civ_arena.arena.events import EventLog
from civ_arena.canonical import log_prefix_hash

CHECKPOINT_SCHEMA = 1


@dataclass(frozen=True)
class CheckpointState:
    match_id: str
    game_instance_id_of_origin: str
    turn: int
    seq: int  # log length at save time
    sim_doc: dict[str, Any]
    rng_states: dict[str, list[Any]]
    coordinator_state: dict[str, Any]
    log_prefix_sha256: str

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema": CHECKPOINT_SCHEMA,
            "match_id": self.match_id,
            "game_instance_id_of_origin": self.game_instance_id_of_origin,
            "turn": self.turn,
            "seq": self.seq,
            "sim_doc": self.sim_doc,
            "rng_states": self.rng_states,
            "coordinator_state": self.coordinator_state,
            "log_prefix_sha256": self.log_prefix_sha256,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> CheckpointState:
        if doc.get("schema") != CHECKPOINT_SCHEMA:
            raise ValueError(f"checkpoint schema {doc.get('schema')} != "
                             f"{CHECKPOINT_SCHEMA}")
        return cls(
            match_id=doc["match_id"],
            game_instance_id_of_origin=doc["game_instance_id_of_origin"],
            turn=doc["turn"], seq=doc["seq"], sim_doc=doc["sim_doc"],
            rng_states=doc["rng_states"],
            coordinator_state=doc["coordinator_state"],
            log_prefix_sha256=doc["log_prefix_sha256"],
        )


class CheckpointManager:
    def __init__(self, directory: Path, every_n_turns: int) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.every_n_turns = every_n_turns

    def maybe_save(self, turn: int, state: CheckpointState) -> Path | None:
        if turn % self.every_n_turns != 0:
            return None
        path = self.dir / f"ckpt-turn-{turn:04d}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state.to_doc(), sort_keys=True))
        os.replace(tmp, path)
        return path

    def latest(self) -> CheckpointState | None:
        best: tuple[int, Path] | None = None
        for path in self.dir.glob("ckpt-turn-*.json"):
            turn = int(path.stem.split("-")[-1])
            if best is None or turn > best[0]:
                best = (turn, path)
        if best is None:
            return None
        return CheckpointState.from_doc(json.loads(best[1].read_text()))

    @staticmethod
    def verify_log_prefix(log: EventLog, ckpt: CheckpointState) -> bool:
        records = log.records()
        if len(records) < ckpt.seq:
            return False
        prefix = records[: ckpt.seq]
        return log_prefix_hash(prefix) == ckpt.log_prefix_sha256
