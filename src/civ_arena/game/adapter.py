"""The GameAdapter seam: the only interface the arena core ever sees.

The arena core (referee, coordinator, sessions) talks to games exclusively
through :class:`GameAdapter`. Lua never enters the arena core; adapter
implementations translate between this semantic surface and their engine
(simulator natively, FireTuner via the vendored wire layer + pure
translator/parser modules).
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class ObserveKind(enum.Enum):
    """The six observation kinds of the agent-visible tool surface."""

    OVERVIEW = "overview"
    UNITS = "units"
    CITIES = "cities"
    VISIBLE_MAP = "visible_map"
    AVAILABLE_RESEARCH = "available_research"
    AVAILABLE_PRODUCTION = "available_production"


@dataclass(frozen=True)
class ObserveRequest:
    kind: ObserveKind
    player_id: int  # supplied by PlayerSession (bound) or Referee (referee scope)
    subject_id: str | None = None  # city_id for AVAILABLE_PRODUCTION


@dataclass(frozen=True)
class ActionCommand:
    tool: str
    args: Mapping[str, Any]
    player_id: int  # injected by the referee from the bound session — never agent-supplied
    idempotency_key: str
    lease_id: str


@dataclass(frozen=True)
class MutationRecord:
    """A single state change that actually happened.

    ``origin`` ("command" | "ambient" | "stock_ai" | "chaos" ...) is
    DIAGNOSTIC ONLY. Authorization is decided by diffing against the
    referee's own ledger of requested effects — never by trusting this tag
    (a chaos mutation that lies about being ambient is still caught).
    """

    kind: str  # e.g. "unit.moved", "city.production_set", "player.research_set"
    entity_type: str  # "unit" | "city" | "player" | "tile" | "revealed"
    entity_id: str
    attr: str
    before: Any
    after: Any
    origin: str = "command"

    def to_doc(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "attr": self.attr,
            "before": self.before,
            "after": self.after,
            "origin": self.origin,
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> MutationRecord:
        return cls(
            kind=doc["kind"],
            entity_type=doc["entity_type"],
            entity_id=doc["entity_id"],
            attr=doc["attr"],
            before=doc["before"],
            after=doc["after"],
            origin=doc.get("origin", "command"),
        )


class RejectionReason(enum.StrEnum):
    NO_LEASE = "no_lease"
    LEASE_EXPIRED = "lease_expired"
    LEASE_FOREIGN = "lease_foreign"
    NOT_YOUR_UNIT = "not_your_unit"
    NOT_YOUR_CITY = "not_your_city"
    UNKNOWN_ENTITY = "unknown_entity"
    NO_MOVEMENT = "no_movement"
    ILLEGAL_DEST = "illegal_dest"
    ILLEGAL_MOVE = "illegal_move"
    OUT_OF_RANGE = "out_of_range"
    CANNOT_ATTACK = "cannot_attack"
    PREREQ_UNMET = "prereq_unmet"
    INSUFFICIENT_GOLD = "insufficient_gold"
    OCCUPIED = "occupied"
    ALREADY = "already"
    ARGS_INVALID = "args_invalid"
    TOOL_UNKNOWN = "tool_unknown"
    NOT_IMPLEMENTED = "not_implemented"


@dataclass(frozen=True)
class ActionResult:
    status: str  # "accepted" | "rejected" | "duplicate"
    result: dict[str, Any] | None
    mutations: tuple[MutationRecord, ...] = ()
    rejection: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class AdapterCapabilities:
    rollback: bool
    save_load: bool
    acts: bool  # False for the FireTuner skeleton
    state_hash: bool
    turn_events: bool


EMPTY_CAPABILITIES = AdapterCapabilities(
    rollback=False, save_load=False, acts=False, state_hash=False, turn_events=False
)


@runtime_checkable
class GameAdapter(Protocol):
    """The seam. Arena core depends on this and nothing else game-side."""

    # lifecycle
    async def setup(self, cfg: Mapping[str, Any]) -> None: ...
    async def teardown(self) -> None: ...
    def capabilities(self) -> AdapterCapabilities: ...

    # turn lifecycle — the referee drives phases explicitly
    async def begin_phase(self, player_id: int, turn: int) -> dict[str, Any]: ...
    async def end_phase(self, player_id: int, turn: int) -> dict[str, Any]: ...
    async def current_phase(self) -> dict[str, Any]: ...

    # observation (referee applies visibility scope AFTER this returns)
    async def observe(self, req: ObserveRequest) -> Any: ...

    # action
    async def act(self, cmd: ActionCommand) -> ActionResult: ...

    # watchdog support
    def snapshot(self) -> Any: ...
    def restore(self, snap: Any) -> None: ...
    def drain_mutations(self) -> list[MutationRecord]: ...
    def state_hash(self) -> str: ...

    # persistence
    def export_state(self) -> dict[str, Any]: ...
    def import_state(self, doc: dict[str, Any]) -> None: ...


# Capabilities advertised by the simulator adapter.
SIM_CAPABILITIES = AdapterCapabilities(
    rollback=True, save_load=True, acts=True, state_hash=True, turn_events=True
)


@dataclass(frozen=True)
class AmbientManifest:
    """What the referee REQUESTED at a phase boundary (the authorization source)."""

    player_id: int
    turn: int
    mutations: tuple[MutationRecord, ...] = field(default=())

    def to_doc(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "turn": self.turn,
            "mutations": [m.to_doc() for m in self.mutations],
        }
