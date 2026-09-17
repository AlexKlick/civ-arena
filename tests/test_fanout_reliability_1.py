"""Fanout reliability batch 1: red-first regressions for the live driver.

Each test rehearses one wire-anomaly shape against the in-process
FakeTunerServer (no game process, no display) and asserts the run
CONTINUES — the flag_and_continue contract every live config declares.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from civ_arena.config import load_config
from civ_arena.game.civ6 import live_driver as ld
from civ_arena.game.civ6.fake_tuner_server import FakeMod, FakeTunerServer
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from civ_arena.game.civ6.vendor.connection import LuaError

PLAYER = 0
TURN = 1


class _EngineEventMod(FakeMod):
    """A benign engine-side event (a goody-hut gold bonus): the phase
    owner's OWN digest row moves mid-turn with zero allowed mutations —
    the exact shape the watchdog's flag_and_continue branch exists for.
    The bump lands after the phase's declared ambient window closed and
    before the end-turn seal, so neither the window manifest nor the
    referee's allowed list can account for it."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._leased_digest_reads = 0

    def respond(self, code):
        rows = super().respond(code)
        if self.lease is None or "Puppeteer.Digest" not in code \
                or not rows or rows[0].startswith("MOD_DIGEST|"):
            return rows
        self._leased_digest_reads += 1
        if self._leased_digest_reads > 1:  # from the post-attach digest_open on
            self.players[self.lease["player"]]["gold"] += 3
            return [self._digest()]
        return rows


def _spec():
    return load_config(ld.MOD_DEFAULT.parents[2] / "configs/live-hotseat-001.yaml")


async def _run(tmp_path, mod, rounds=1, runtime=None, monkeypatch=None):
    if runtime is not None:
        monkeypatch.setattr(ld, "build_runtime", lambda _, **_ctx: runtime)
    server = FakeTunerServer(mod=mod)
    port = await server.start()
    adapter = FireTunerAdapter("127.0.0.1", port, simulate_hook=ld._fake_hook)  # noqa: SLF001
    try:
        code = await ld.phase_dispatch_hotseat(
            _spec(), adapter, tmp_path, rounds, "h1", ld.MOD_DEFAULT.read_text())
    finally:
        await server.stop()
    events = [json.loads(line)
              for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    summary = json.loads((tmp_path / "summary.json").read_text())
    return code, events, summary


class _InertRuntime:
    """A seat that plays no commands: its lease closes with an EMPTY
    allowed list, so any digest movement is an unexplained anomaly."""

    def bind_services(self, **_kw):
        return None

    async def take_turn(self, facade):
        # the completeness gate bounces the first end-turn (unmoved units)
        await facade.end_turn()
        await facade.end_turn()


async def test_digest_anomaly_flags_and_continues_instead_of_aborting(
        tmp_path, monkeypatch):
    mod = _EngineEventMod(hotseat=[0, 1])

    code, events, summary = await _run(
        tmp_path, mod, rounds=1, runtime=_InertRuntime(), monkeypatch=monkeypatch)

    flagged = [e for e in events if e.get("audit") == "watchdog_flag"]
    assert flagged, "the digest anomaly must be FLAGGED, not swallowed"
    assert code == 0, summary["failure_reason"]
    assert summary["clean"] is True
    assert summary["failure_reason"] is None
    # the flag row carries the turn it happened on
    assert {f["turn"] for f in flagged} == {1}


# -- finding 2: wire-sourced own-city buildings reach the sim gold loop ------

def test_own_city_wire_buildings_survive_the_ambient_rollout():
    """A wire-exact own CITYROW carries BUILDING_PALACE (every Civ6 capital)
    plus any engine building outside the sim's 3-entry catalogue. The list
    passes parse_cities -> VisibilityPolicy (own = whole row) -> belief
    (setdefault only fills an ABSENT key) -> build_state_doc verbatim into
    the rollout state, where run_ambient's per-city gold loop must not
    KeyError on a name the sim catalogue has never heard of."""
    import asyncio
    from types import SimpleNamespace

    from civ_arena.arena.visibility import VisibilityPolicy
    from civ_arena.game.civ6.response_parser import parse_cities
    from civ_arena.game.sim.engine import run_ambient
    from civ_arena.game.sim.state import BUILDINGS, SimState
    from civ_arena.planner.belief import PlannerBelief, build_state_doc
    from civ_arena.planner.entity_boundary import EntityBoundary

    lines = ["CITIES|2",
             "CITYROW|c0:7|0|ROME|36|20|1|-|true|true|100|100|0|22|0|4|4|"
             "BUILDING_PALACE;BUILDING_MONUMENT|-",
             "---END---"]
    wire = parse_cities(lines, qualified=True)
    projected = VisibilityPolicy().project(
        wire, "cities", PLAYER, observable=frozenset(), remembered=frozenset())

    async def _cities():
        return projected
    facade = SimpleNamespace(
        get_units=_cities, get_cities=_cities, get_overview=_cities,
        get_visible_map=_cities)
    boundary = EntityBoundary(facade)
    belief = PlannerBelief(player_id=PLAYER)
    belief.observe_cities(asyncio.run(boundary.get_cities()), turn=TURN)

    # the wire name reached the belief verbatim (this is the load-bearing
    # input: the planner must keep SEEING the palace, just not crash on it).
    # EntityBoundary re-encodes the qualified id to a Cantor-pair int, so
    # look up the single own city rather than by wire id.
    assert len(belief.own_cities) == 1
    assert next(iter(belief.own_cities.values()))["buildings"] == \
        ["PALACE", "MONUMENT"]

    bstate = SimState.from_doc(build_state_doc(belief, seed=24))
    run_ambient(bstate, PLAYER)  # must not raise KeyError('PALACE')
    city = next(iter(bstate.doc["cities"].values()))
    # MONUMENT is catalogued: its gold still counts. PALACE is not: no crash
    # and no invented gold.
    assert city["buildings"] == ["PALACE", "MONUMENT"]
    assert BUILDINGS["MONUMENT"]["gold"] == 2


# -- finding 4: an empty/truncated Status poll must not KeyError the driver --

def test_target_turn_reads_an_empty_status_poll_as_no_target_yet():
    """poll_status() returns {} with no exception when the Status read times
    out (parse_kv_lines([]) => {}), and a truncated payload can carry
    TURN_ACTIVE without the TURN row. _target_turn must read both shapes as
    "nothing to target yet" — the same -1 sentinel rule 1 already emits for
    a lease row without LEASE_TURN — so engage()'s probe returns None and
    the recovery loop re-polls, instead of KeyError('TURN') aborting the
    match at the first engagement."""
    from civ_arena.game.civ6.live_driver import _target_turn

    # first engagement (last_driven default -1): the .get default and the
    # missing key COLLAPSE into the same branch -> bare status["TURN"]
    assert _target_turn({}, 0, -1, None) == -1
    # mid-run empty poll (last_driven is a real turn)
    assert _target_turn({}, 0, 7, None) == -1
    # truncated payload: TURN_ACTIVE present, TURN row lost
    assert _target_turn({"TURN_ACTIVE": True}, 0, -1, None) == -1
    assert _target_turn({"TURN_ACTIVE": True}, 0, 7, None) == -1
    # partial lease rows without the TURN row are already safe (rule 1)
    assert _target_turn({"PUPPET_ACTIVE": True, "LEASE_PLAYER": 0}, 0, -1,
                        None) == -1
    # a well-formed payload is unchanged
    assert _target_turn({"TURN": 4, "PUPPET_ACTIVE": False}, 0, 7, None) == 4


# -- findings 5/6/7: lease-start production housekeeping must degrade, never
#    abort, and must never leave a provably-idle queue empty ---------------

class _HousekeepAdapter:
    """One own city, provably idle (CURPROD|0), with a scripted
    AVAILABLE_PRODUCTION read. Everything else is a test bug."""

    def __init__(self, items=(), *, city=None, options_error=None):
        self.items = list(items)
        self.city = city or {
            "city_id": "c0:65536", "owner": 0, "name": "ROME",
            "production_queue": [],
        }
        self.options_error = options_error
        self.commands = []

    async def observe(self, req):
        from civ_arena.game.adapter import ObserveKind
        if req.kind is ObserveKind.CITIES:
            return [dict(self.city)]
        if req.kind is ObserveKind.AVAILABLE_PRODUCTION:
            if self.options_error is not None:
                raise self.options_error
            return [dict(i) for i in self.items]
        raise AssertionError(f"unexpected observe {req.kind}")

    async def write_raw(self, _lua):
        return ["CURPROD|0", "---END---"]

    async def act(self, command):
        self.commands.append(command)
        return SimpleNamespace(status="accepted")


def _items(*names):
    """ITEMROW docs; the sim-era vocabulary so the preference can miss."""
    return [{"item_id": n, "kind": "unit" if n in ("BUILDER", "TRADER", "ARCHER")
             else "building", "cost": 50, "turns": 5} for n in names]


async def test_empty_queue_fills_from_a_non_preference_pick():
    """A grown/late-era city can offer nothing off _BUILD_PREFERENCE. The
    reactive fill must still pick — deterministically, the same doctrine
    _ensure_research already implements — because the production blocker
    only ever lists at turn END, when the wire can no longer resolve it
    (glm-g1 turn 12's freeze)."""
    from civ_arena.game.civ6 import live_driver as ld

    adapter = _HousekeepAdapter(_items("TRADER", "ARCHER", "BUILDER"))
    await ld._fill_empty_queues(adapter, 0, turn=15)
    assert [c.args["item_id"] for c in adapter.commands] == ["ARCHER"]


async def test_empty_queue_with_no_producible_item_is_skipped():
    """Nothing offerable at all: skip fail-closed (the P2-11 rule) rather
    than issue a command the engine cannot take."""
    from civ_arena.game.civ6 import live_driver as ld

    adapter = _HousekeepAdapter([])
    await ld._fill_empty_queues(adapter, 0, turn=15)
    assert adapter.commands == []


@pytest.mark.parametrize("error", [
    ValueError("productive options completeness marker unavailable"),
    LuaError("O: InGame: productive target result unavailable"),
])
async def test_production_options_read_failure_skips_the_city(error):
    """The options read is the one un-pcall'd fail-loud Lua block this
    proactive housekeeping runs, and its parser raises on a truncated
    payload. Both are transient wire/VM shapes at a lease start of a
    30-round match: the fill is best-effort, so the city is skipped and
    the exception must never reach _resolve_blockers' caller."""
    from civ_arena.game.civ6 import live_driver as ld

    adapter = _HousekeepAdapter(_items("MONUMENT"), options_error=error)
    await ld._fill_empty_queues(adapter, 0, turn=15)  # must not raise
    await ld._resolve_blockers(adapter, 0, turn=15)  # same: no escape
    assert adapter.commands == []
