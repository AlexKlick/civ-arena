"""SimulatorAdapter: GameAdapter implemented natively on the deterministic sim."""

from __future__ import annotations

import copy
from typing import Any

from civ_arena.canonical import state_hash as _state_hash
from civ_arena.game.adapter import (
    SIM_CAPABILITIES,
    ActionCommand,
    ActionResult,
    AdapterCapabilities,
    MutationRecord,
    ObserveKind,
    ObserveRequest,
)
from civ_arena.game.sim.engine import run_ambient, stock_ai_turn
from civ_arena.game.sim.layouts import duel_start
from civ_arena.game.sim.rules import (
    apply_action,
    available_production,
    available_research,
    check_action,
)
from civ_arena.game.sim.state import SimState, tile_key
from civ_arena.game.sim.visibility import ground_truth


class SimulatorAdapter:
    """In-process game engine. Journals every mutation; the referee drains it."""

    def __init__(self) -> None:
        self.state: SimState | None = None
        self._journal: list[MutationRecord] = []
        self._chaos: Any = None  # ChaosDirector | None (injected via setup cfg)
        self._broken_freeze = False
        self._stock_ai = False
        self._player_count = 2
        self._hash_cache: str | None = None

    # -- lifecycle ---------------------------------------------------------
    async def setup(self, cfg: dict[str, Any]) -> None:
        self.state = SimState.from_doc(duel_start(int(cfg["seed"])))
        self._broken_freeze = bool(cfg.get("broken_freeze", False))
        self._stock_ai = bool(cfg.get("stock_ai", False))
        self._chaos = cfg.get("chaos_director")
        self._player_count = 2

    async def teardown(self) -> None:
        self.state = None
        self._journal.clear()

    def capabilities(self) -> AdapterCapabilities:
        return SIM_CAPABILITIES

    # -- turn lifecycle ------------------------------------------------------
    async def begin_phase(self, player_id: int, turn: int) -> dict[str, Any]:
        self._require_state()
        if self.state.phase_player != -1:
            raise RuntimeError(
                f"phase already open for player {self.state.phase_player}"
            )
        if self.state.turn != turn:
            raise RuntimeError(f"turn mismatch: engine at {self.state.turn}, asked {turn}")

        self._hash_cache = None
        freeze = not self._broken_freeze
        self.state.doc["freeze_active"] = freeze
        # The built-in "AI" only gets to act when the freeze FAILED to engage —
        # this is the path the broken_freeze test exercises.
        if self._stock_ai and not freeze:
            self._journal.extend(stock_ai_turn(self.state, player_id))
        if self._chaos is not None:
            self._chaos.maybe_fire("begin_phase", self)

        manifest = run_ambient(self.state, player_id)
        self._journal.extend(manifest)
        self.state.phase_player = player_id
        return {"manifest": [m.to_doc() for m in manifest]}

    async def end_phase(self, player_id: int, turn: int) -> dict[str, Any]:
        self._require_state()
        if self.state.phase_player != player_id or self.state.turn != turn:
            raise RuntimeError("end_phase does not match the open phase")
        self._hash_cache = None
        if self._chaos is not None:
            self._chaos.maybe_fire("end_phase", self)
        self.state.phase_player = -1
        self.state.doc["freeze_active"] = False
        next_index = self.state.phase_index + 1
        if next_index >= self._player_count:
            self.state.phase_index = 0
            self.state.turn += 1
        else:
            self.state.phase_index = next_index
        return {}

    async def current_phase(self) -> dict[str, Any]:
        self._require_state()
        return {
            "turn": self.state.turn,
            "phase_player": self.state.phase_player,
            "phase_index": self.state.phase_index,
        }

    # -- observation (OMNISCIENT; the referee projects scope afterwards) ------
    async def observe(self, req: ObserveRequest) -> Any:
        self._require_state()
        state = self.state
        if req.kind is ObserveKind.OVERVIEW:
            return {
                "turn": state.turn,
                "phase_player": state.phase_player,
                "players": {k: copy.deepcopy(v) for k, v in state.players.items()},
                "cities": {k: copy.deepcopy(v) for k, v in state.cities.items()},
                "units": {k: copy.deepcopy(v) for k, v in state.units.items()},
                "tiles": {k: copy.deepcopy(v) for k, v in state.tiles.items()},
            }
        if req.kind is ObserveKind.UNITS:
            # deterministic observation order (numeric id) — dict insertion
            # order does not survive checkpoint round-trips. DEEP copies:
            # observations handed to agents must never alias live state.
            return sorted(
                # The simulator's run_ambient explicitly caps unit HP at100.
                # Observation metadata only: never alter simulator state/hash.
                ({**copy.deepcopy(u), "max_hp": 100,
                  "health_valid": type(u["hp"]) is int and 0 <= u["hp"] <= 100}
                 for u in state.units.values()),
                key=lambda u: int(u["unit_id"][1:]),
            )
        if req.kind is ObserveKind.CITIES:
            return sorted(
                (copy.deepcopy(c) for c in state.cities.values()),
                key=lambda c: int(c["city_id"][1:]),
            )
        if req.kind is ObserveKind.VISIBLE_MAP:
            return {
                "turn": state.turn,
                "tiles": {k: copy.deepcopy(v) for k, v in state.tiles.items()},
            }
        if req.kind is ObserveKind.AVAILABLE_RESEARCH:
            if req.research_building_briefing:
                raise ValueError('native research-building briefing unsupported in simulator')
            return available_research(state, req.player_id)
        if req.kind is ObserveKind.AVAILABLE_PRODUCTION:
            if req.subject_id is None:
                raise ValueError("AVAILABLE_PRODUCTION requires subject_id (city_id)")
            return available_production(state, req.player_id, req.subject_id)
        raise ValueError(f"unknown observe kind: {req.kind}")

    # -- action ----------------------------------------------------------------
    async def act(self, cmd: ActionCommand) -> ActionResult:
        self._require_state()
        self._hash_cache = None
        if self.state.phase_player != cmd.player_id:
            return ActionResult(
                status="rejected", result=None, mutations=(),
                rejection="no_lease",
                error=f"phase belongs to player {self.state.phase_player}",
            )
        reason = check_action(self.state, cmd.player_id, cmd.tool, dict(cmd.args))
        if reason is not None:
            return ActionResult(
                status="rejected", result=None, mutations=(), rejection=str(reason),
                error=f"{cmd.tool} rejected: {reason.value}",
            )
        muts = apply_action(self.state, cmd.player_id, cmd.tool, dict(cmd.args))
        self._journal.extend(muts)
        if self._chaos is not None:
            self._chaos.maybe_fire("act", self)
        return ActionResult(
            status="accepted",
            result={"tool": cmd.tool, "mutations": len(muts)},
            mutations=tuple(muts),
        )

    # -- watchdog support ---------------------------------------------------------
    def visibility_for(self, player_id: int) -> tuple[frozenset[str], frozenset[str]]:
        self._require_state()
        vis = ground_truth(self.state, player_id)
        return vis.observable, vis.remembered

    def snapshot(self) -> Any:
        self._require_state()
        return self.state.to_doc()

    def restore(self, snap: Any) -> None:
        self._hash_cache = None
        self.state = SimState.from_doc(snap)

    def drain_mutations(self) -> list[MutationRecord]:
        out = list(self._journal)
        self._journal.clear()
        return out

    def state_hash(self) -> str:
        self._require_state()
        if self._hash_cache is None:
            self._hash_cache = _state_hash(self.state.to_doc())
        return self._hash_cache

    # -- persistence ------------------------------------------------------------
    def export_state(self) -> dict[str, Any]:
        self._require_state()
        return self.state.to_doc()

    def import_state(self, doc: dict[str, Any]) -> None:
        self._hash_cache = None
        self.state = SimState.from_doc(doc)

    # -- helpers (chaos/tests) ------------------------------------------------------
    def _require_state(self) -> None:
        if self.state is None:
            raise RuntimeError("adapter not set up")

    def chaos_move_unit(self, unit_id: str, q: int, r: int) -> None:
        """Raw unauthorized teleport used by the ChaosDirector."""
        self._require_state()
        self._hash_cache = None
        unit = self.state.unit(unit_id)
        if unit is None:
            return
        old = tile_key(unit["q"], unit["r"])
        unit["q"], unit["r"] = q, r
        self._journal.append(MutationRecord(
            kind="unit.moved", entity_type="unit", entity_id=unit_id, attr="coord",
            before=old, after=tile_key(q, r), origin="chaos"))

    def chaos_set(self, entity_type: str, entity_id: str, attr: str, after: Any,
                  origin: str = "chaos") -> None:
        """Raw unauthorized attribute write used by the ChaosDirector."""
        self._require_state()
        self._hash_cache = None
        if entity_type == "city":
            entity = self.state.city(entity_id)
        elif entity_type == "player":
            entity = self.state.player(int(entity_id))
        else:
            entity = self.state.unit(entity_id)
        if entity is None:
            return
        before = entity[attr]
        entity[attr] = after
        self._journal.append(MutationRecord(
            kind=f"{entity_type}.{attr}", entity_type=entity_type, entity_id=entity_id,
            attr=attr, before=before, after=after, origin=origin))

    def chaos_spawn_unit(self, owner: int, type_: str, q: int, r: int) -> None:
        self._require_state()
        self._hash_cache = None
        _, unit = self.state.spawn_unit(owner, type_, q, r)
        self._journal.append(MutationRecord(
            kind="unit.spawned", entity_type="unit", entity_id=unit["unit_id"],
            attr="created", before=0, after=1, origin="chaos"))
