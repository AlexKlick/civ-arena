"""In-process Civ VI tuner stand-in speaking the real Firaxis Nexus framing.

Test infrastructure: lets the vendored wire layer, the Lua translator, and
the response parser all be exercised without a live game — the same
game-free philosophy as upstream's _StubConnection, but at the socket layer.

Two response sources, first match wins:

1. the canned ``(state_index, substring) -> lines`` list (explicit,
   per-test overrides), then
2. an optional :class:`FakeMod` — a scripted PuppeteerMod stand-in that
   rehearses mod-shaped commands game-free.

Anything else is ``ERR:FAKE unknown command``.
"""

from __future__ import annotations

import asyncio
import re

from civ_arena.game.civ6 import lua_translator
from civ_arena.game.civ6.vendor import tuner_client

APP_IDENTITY = "FakeSidMeiersCivilizationVI"

# mirrors the LSQ: handshake answers below
LUA_STATES = {0: "GameCore_Tuner", 1: "InGame"}


def _ax(x: int, y: int) -> tuple[int, int]:
    return lua_translator.xy_to_axial(x, y)


class FakeMod:
    """Scripted PuppeteerMod stand-in (mod >= 0.3 shapes) over a MINI
    ENGINE (a tiny fixed board: 2 majors, a few units, 1-2 cities).

    Answers the pipe rows the real mod prints, keeps puppet/lease/mark
    state across commands, and exposes ``Simulate.*`` FAKE-ONLY commands
    tests use to fire engine-side events the wire cannot. Faithful to D3
    (poll, never push): hook-driven prints are UNSOLICITED in the real wire
    and get drained, so ``Simulate.TurnStart`` changes state silently and
    the change surfaces only via the next Status/Digest poll.

    The act handlers parse the SAME Lua the translator emits (keyed by the
    inert ``-- arena:tool=`` marker) and mutate the mini engine the way the
    real one would — so observes, digests, and the DiffSinceLast
    reconciliation rehearse the full M14d dispatch path game-free.
    """

    # the mini engine's catalogs (doctrine vocabulary, already normalized)
    TECHS = {"MINING": 25, "POTTERY": 25, "ARCHERY": 35, "BRONZE_WORKING": 45}
    BUILDABLE = {
        "WARRIOR": (40, "unit"), "SETTLER": (80, "unit"),
        "MONUMENT": (60, "building"), "WALLS": (70, "building"),
    }
    PURCHASE_GOLD_PREMIUM = 2

    def __init__(
        self,
        version: str = "0.3.0-rehearsal",
        has_status: bool = True,
        has_digest: bool = True,
        has_command_diff: bool = True,
        supports_freeze: bool = True,
        supports_ledger: bool = True,
        auto_ambient: tuple | None = None,
        injected: bool = True,
    ) -> None:
        self.version = version
        self.has_status = has_status
        self.has_digest = has_digest
        self.has_command_diff = has_command_diff
        self.supports_freeze = supports_freeze
        self.supports_ledger = supports_ledger
        # engine effect that lands inside every ambient window:
        # (kind, entity_type, numeric_id, attr, before, after)
        self.auto_ambient = auto_ambient
        # D9 fidelity: the mod exists in the GameCore VM only after its
        # source executes there. injected=False models a fresh attach
        # (handshake answers MOD_PRESENT|false until the injection runs).
        self.injected = injected
        self.injections = 0
        self.turn_active = False
        self.puppets: dict[int, bool] = {}
        self.turn = 1
        self.lease: dict[str, int] | None = None
        self.mark: dict[str, object] | None = None  # rolling DiffSinceLast baseline
        self.ledger_rows: list[str] = []
        self.ambient_rows: list[str] = []
        self.state_nonce = 0
        self._diff_cache: str | None = None
        self._diff_cache_seq: int = -1
        self.trace: list[str] = []
        self.pending_blockers: list[str] = []
        self._restored: set[int] = set()
        self.act_log: list[tuple[str, str]] = []  # (tool, status) per command
        self.reset_board()

    # -- the mini engine ---------------------------------------------------
    def reset_board(self) -> None:
        self.players = {
            0: {"gold": 100, "researching": "", "researched": []},
            1: {"gold": 100, "researching": "", "researched": []},
        }
        self.units: dict[int, dict] = {
            # owner 0: a far settler (founds turn 1), a warrior (fortifies),
            # a near settler (marches), an archer (attack-path tests)
            1: {"owner": 0, "type": "SETTLER", "x": 10, "y": 10,
                "moves": 2, "damage": 0, "fortified": False},
            2: {"owner": 0, "type": "WARRIOR", "x": 5, "y": 5,
                "moves": 2, "damage": 0, "fortified": False},
            4: {"owner": 0, "type": "SETTLER", "x": 3, "y": 2,
                "moves": 2, "damage": 0, "fortified": False},
            5: {"owner": 0, "type": "ARCHER", "x": 6, "y": 6,
                "moves": 2, "damage": 0, "fortified": False},
            3: {"owner": 1, "type": "WARRIOR", "x": 30, "y": 30,
                "moves": 2, "damage": 0, "fortified": False},
        }
        self.cities: dict[int, dict] = {
            1: {"owner": 0, "name": "ARENA", "x": 2, "y": 2,
                "pop": 1, "queue": ""},
        }
        self.next_city_id = 2

    # -- the M17c targeted map read ----------------------------------------

    # engine-named terrains on purpose: the rehearsal must exercise the
    # parser's engine->sim terrain mapping, not confirm a copy of itself
    _FAKE_TERRAINS = ("GRASS", "GRASS_HILLS", "PLAINS", "PLAINS_HILLS",
                      "DESERT", "TUNDRA", "COAST", "OCEAN")

    def _snapshot(self, pid: int) -> dict[str, object]:
        """The mod's snapshot_player parity: units(pos/moves/damage),
        cities(population), player(gold, researching)."""
        return {
            "units": {uid: (u["x"], u["y"], u["moves"], u["damage"])
                      for uid, u in self.units.items() if u["owner"] == pid},
            "cities": {cid: c["pop"] for cid, c in self.cities.items()
                       if c["owner"] == pid},
            "player": (self.players[pid]["gold"],
                       self.players[pid]["researching"]),
        }

    def _diff(self, pid: int, before: dict[str, object]) -> list[str]:
        """The mod's diff_player parity — SAME code path shapes both the
        commanded rows (DiffSinceLast) and the release drift, which is the
        invariant that makes the watchdog's exact-key diff reconcile."""
        rows: list[str] = []
        b_units: dict[int, tuple] = before["units"]  # type: ignore[assignment]
        b_cities: dict[int, int] = before["cities"]  # type: ignore[assignment]
        b_gold, b_res = before["player"]  # type: ignore[misc]
        for uid, u in sorted(self.units.items()):
            if u["owner"] != pid:
                continue
            b = b_units.get(uid)
            if b is None:
                rows.append(f"LEDGER|unit.spawned|unit|u{uid}|exists"
                            f"|false|true")
                continue
            if (u["x"], u["y"]) != (b[0], b[1]):
                rows.append(f"LEDGER|unit.moved|unit|u{uid}|pos|{b[0]},{b[1]}"
                            f"|{u['x']},{u['y']}")
            if u["moves"] != b[2]:
                rows.append(f"LEDGER|unit.moves|unit|u{uid}|moves|{b[2]}"
                            f"|{u['moves']}")
            if u["damage"] != b[3]:
                rows.append(f"LEDGER|unit.damage|unit|u{uid}|damage|{b[3]}"
                            f"|{u['damage']}")
        for uid in sorted(set(b_units) - {u for u, x in self.units.items()
                                          if x["owner"] == pid}):
            rows.append(f"LEDGER|unit.despawned|unit|u{uid}|exists|true|false")
        for cid, c in sorted(self.cities.items()):
            if c["owner"] != pid:
                continue
            if cid not in b_cities:
                rows.append(f"LEDGER|city.founded|city|c{cid}|exists|false|true")
            elif c["pop"] != b_cities[cid]:
                rows.append(f"LEDGER|city.growth|city|c{cid}|population|"
                            f"{b_cities[cid]}|{c['pop']}")
        for cid in sorted(set(b_cities) - {c for c, x in self.cities.items()
                                           if x["owner"] == pid}):
            rows.append(f"LEDGER|city.lost|city|c{cid}|exists|true|false")
        gold = self.players[pid]["gold"]
        res = self.players[pid]["researching"]
        if gold != b_gold:
            rows.append(f"LEDGER|player.gold|player|p{pid}|gold|{b_gold}|{gold}")
        if res != b_res:
            rows.append(f"LEDGER|player.research_set|player|p{pid}"
                        f"|researching|{b_res}|{res}")
        return rows

    # digest: ENGINE STATE ONLY over the mini board (+ a nonce row for the
    # Simulate.Mutate drift fixture; unattributed, so owner-scoped hashes
    # filter it out exactly like a foreign row would be).
    def _digest(self) -> str:
        rows = []
        for uid, u in sorted(self.units.items()):
            rows.append(f"u{uid}|{u['owner']}|{u['x']}|{u['y']}"
                        f"|{u['moves']}|{u['damage']}")
        for cid, c in sorted(self.cities.items()):
            rows.append(f"c{cid}|{c['owner']}|{c['pop']}")
        for pid, p in sorted(self.players.items()):
            rows.append(f"p{pid}|{p['gold']}|{p['researching'] or -1}")
        rows.append(f"nonce{self.state_nonce}")
        return "DIGEST|" + ";".join(rows)

    def _status_rows(self) -> list[str]:
        # ONE embedded-newline row: the real mod's Status() RETURNS its
        # payload, so one print() carries all rows — the parser must split
        # them (rehearses the live wire shape, live-learned 2026-08-30).
        active = self.lease is not None
        player = self.lease["player"] if self.lease else -1
        turn = self.lease["turn"] if self.lease else -1
        # TURN_ACTIVE models the engine's IsTurnActive(local player): the
        # local turn is active from turn start until deactivated — the
        # discriminator for the dispatch targeting race (run 005)
        ta = str(self.turn_active).lower()
        return [
            f"TURN|{self.turn}\nPUPPET_ACTIVE|{str(active).lower()}"
            f"\nLEASE_PLAYER|{player}\nLEASE_TURN|{turn}"
            f"\nTURN_ACTIVE|{ta}"
        ]

    # -- act handlers: parse the translator's own Lua -----------------------
    def _act(self, tool: str, code: str) -> list[str] | None:
        def num(pat: str) -> int:
            m = re.search(pat, code)
            return int(m.group(1)) if m else -1

        def token(pat: str) -> str:
            m = re.search(pat, code)
            return m.group(1) if m else ""

        me = 0

        def dec(num: int) -> int:
            return num % 65536

        if tool == "move_unit":
            uid = num(r"UnitManager\.GetUnit\(me, (\d+)\)")
            x = num(r"PARAM_X\] = (-?\d+)")
            y = num(r"PARAM_Y\] = (-?\d+)")
            u = self.units.get(dec(uid))
            if u is None or u["owner"] != me:
                return [f"ACT|move_unit|ERR|UNKNOWN_ENTITY|u{uid}", "---END---"]
            if u["moves"] <= 0:
                return [f"ACT|move_unit|ERR|NO_MOVEMENT|u{uid}", "---END---"]
            u["x"], u["y"], u["moves"] = x, y, 0
            return [f"ACT|move_unit|OK|{x},{y}", "---END---"]
        if tool == "attack":
            uid = num(r"UnitManager\.GetUnit\(me, (\d+)\)")
            m = re.search(r"UnitManager\.GetUnit\(tOwner, (\d+)\)", code)
            tid = dec(int(m.group(1))) if m else -1
            if dec(uid) not in self.units or tid not in self.units:
                return [f"ACT|attack|ERR|UNKNOWN_ENTITY|u{tid}", "---END---"]
            if self.units[uid]["moves"] <= 0:
                return [f"ACT|attack|ERR|CANNOT_ATTACK|u{tid}", "---END---"]
            self.units[dec(uid)]["moves"] = 0
            self.units[tid]["damage"] = min(
                100, self.units[tid]["damage"] + 30)
            return ["ACT|attack|OK|hit", "---END---"]
        if tool == "fortify":
            uid = num(r"UnitManager\.GetUnit\(me, (\d+)\)")
            if dec(uid) not in self.units:
                return [f"ACT|fortify|ERR|UNKNOWN_ENTITY|u{uid}", "---END---"]
            if self.units[uid]["fortified"]:
                return ["ACT|fortify|OK|already", "---END---"]
            self.units[uid]["fortified"] = True
            return ["ACT|fortify|OK|fortified", "---END---"]
        if tool == "found_city":
            uid = dec(num(r"UnitManager\.GetUnit\(me, (\d+)\)"))
            u = self.units.get(uid)
            if u is None:
                return [f"ACT|found_city|ERR|UNKNOWN_ENTITY|u{uid}", "---END---"]
            if u["type"] != "SETTLER":
                return ["ACT|found_city|ERR|ILLEGAL_MOVE|not-a-settler",
                        "---END---"]
            cid = self.next_city_id
            self.next_city_id += 1
            self.cities[cid] = {"owner": u["owner"], "name": f"NEW{cid}",
                                "x": u["x"], "y": u["y"], "pop": 1, "queue": ""}
            del self.units[uid]
            return [f"ACT|found_city|OK|{self.cities[cid]['x']},"
                    f"{self.cities[cid]['y']}", "---END---"]
        if tool == "set_research":
            pid = num(r"Players\[(\d+)\]")
            tech = token(r"GameInfo\.Technologies\['TECH_([A-Z0-9_]+)'\]")
            p = self.players.get(pid)
            if tech not in self.TECHS:
                return [f"ACT|set_research|ERR|ARGS_INVALID|{tech}", "---END---"]
            if p is None:
                return ["ACT|set_research|ERR|UNKNOWN_ENTITY|player", "---END---"]
            if p["researching"] == tech:
                return [f"ACT|set_research|ERR|ALREADY|{tech}", "---END---"]
            p["researching"] = tech
            return [f"ACT|set_research|OK|{tech}", "---END---"]
        if tool == "set_city_production":
            cid = dec(num(r"CityManager\.GetCity\(me, (\d+)\)"))
            item = token(r"GameInfo\.Units\['UNIT_([A-Z0-9_]+)'\]") or \
                token(r"GameInfo\.Buildings\['BUILDING_([A-Z0-9_]+)'\]")
            c = self.cities.get(cid)
            if c is None or c["owner"] != me:
                return [f"ACT|set_city_production|ERR|UNKNOWN_ENTITY|c{cid}",
                        "---END---"]
            if item not in self.BUILDABLE:
                return [f"ACT|set_city_production|ERR|ARGS_INVALID|{item}",
                        "---END---"]
            c["queue"] = item
            return [f"ACT|set_city_production|OK|{item}|10", "---END---"]
        if tool == "purchase":
            cid = dec(num(r"CityManager\.GetCity\(me, (\d+)\)"))
            item = token(r"GameInfo\.Units\['UNIT_([A-Z0-9_]+)'\]") or \
                token(r"GameInfo\.Buildings\['BUILDING_([A-Z0-9_]+)'\]")
            c = self.cities.get(cid)
            if c is None or c["owner"] != me:
                return [f"ACT|purchase|ERR|UNKNOWN_ENTITY|c{cid}", "---END---"]
            if item not in self.BUILDABLE:
                return [f"ACT|purchase|ERR|ARGS_INVALID|{item}", "---END---"]
            cost = self.BUILDABLE[item][0] * self.PURCHASE_GOLD_PREMIUM
            p = self.players[me]
            if cost > p["gold"]:
                return [f"ACT|purchase|ERR|INSUFFICIENT_GOLD|{cost}gt{p['gold']}",
                        "---END---"]
            p["gold"] -= cost
            return [f"ACT|purchase|OK|{item}|{cost}", "---END---"]
        return None

    def respond(self, code: str) -> list[str] | None:
        """Rows for a command's Lua code, or None if not mod-shaped."""
        # game-level probes (the fake IS the whole game, mod included)
        if 'print("TS|1")' in code:
            return [f"TURN|{self.turn}", "LOCAL|0", "PUPPET_ACTIVE|false"]
        # -- M14d observes over the mini engine --
        if 'print("OVX|1")' in code:
            rows = [f"TURN|{self.turn}"]
            for pid, p in sorted(self.players.items()):
                rows.append(f"OVROW|{pid}|CIVILIZATION_FAKE{pid}|{p['gold']}"
                            f"|{p['researching'] or '-'}")
                if p["researched"]:
                    rows.append("OVRESEARCHED|" + str(pid) + "|"
                                + ";".join(sorted(p["researched"])))
            return rows + ["---END---"]
        if 'print("UNITS|1")' in code:
            rows = []
            for uid, u in sorted(self.units.items()):
                q, r = _ax(u["x"], u["y"])
                combat, ranged = (20, 0) if u["type"] == "WARRIOR" else \
                    (15, 15) if u["type"] == "ARCHER" else (0, 0)
                # composite id (Codex P1-11): uid + owner*65536
                rows.append(
                    f"UNITROW|{uid + u['owner'] * 65536}|{u['owner']}"
                    f"|{u['type']}|{q}|{r}"
                    f"|{100 - u['damage']}|{u['moves']}|2|{combat}|{ranged}"
                    f"|{str(u['fortified']).lower()}")
            return ["UNITS|1", *rows, "---END---"]
        if 'print("CITIES|1")' in code:
            rows = []
            for cid, c in sorted(self.cities.items()):
                q, r = _ax(c["x"], c["y"])
                rows.append(f"CITYROW|{cid + c['owner'] * 65536}"
                            f"|{c['owner']}|{c['name']}|{q}|{r}"
                            f"|{c['pop']}|{c['queue'] or '-'}")
            return ["CITIES|1", *rows, "---END---"]
        if 'print("VMAP|3")' in code:
            # the targeted read: the adapter derived the visible set and
            # asks for exactly those axial coords (offset-encoded in the
            # Lua as {q, r + floor(q / 2)}). Answer only what was asked —
            # a row for an unrequested tile would enter the belief as
            # sight it does not have.
            coords = re.findall(r"\{(-?\d+),(-?\d+)\}", code)
            rows = []
            for x_s, y_s in coords:
                q, y = int(x_s), int(y_s)
                r = y - (q - (q % 2)) // 2
                terrain = self._FAKE_TERRAINS[(q * 31 + r * 17) % 8]
                owner = -1
                city = ""
                for cid, c in self.cities.items():
                    cq, cr = _ax(c["x"], c["y"])
                    if (cq, cr) == (q, r):
                        owner = c["owner"]
                        city = f"c{cid + c['owner'] * 65536}"
                rows.append(f"TILEROW|{q}|{r}|{terrain}|true|{owner}|{city}")
            return ["VMAP|3", f"TURN|{self.turn}", *rows, "---END---"]
        if 'print("VMAP|1")' in code:
            return ["VMAP|1", f"TURN|{self.turn}", "---END---"]
        if 'print("AVRES|1")' in code:
            pid = int(re.search(r"Players\[(\d+)\]", code).group(1))
            p = self.players.get(pid, {"researched": []})
            rows = [f"TECHROW|{t}|{cost}" for t, cost in sorted(self.TECHS.items())
                    if t not in p["researched"]]
            return ["AVRES|1", *rows, "---END---"]
        if 'print("AVPROD|1")' in code:
            rows = [f"ITEMROW|{kind}|{item}|{cost}|10"
                    for item, (cost, kind) in sorted(self.BUILDABLE.items())]
            return ["AVPROD|1", *rows, "---END---"]
        # -- M14d acts (the translator's inert marker identifies the tool) --
        m = re.search(r"-- arena:tool=(\w+)", code)
        if m is not None:
            out = self._act(m.group(1), code)
            if out is not None:
                self.act_log.append((m.group(1), out[0].split("|")[2]))
                return out
        if "PUPPET_PLAYERS = {}" in code:
            # the injected mod source (D9): execution succeeds silently, and
            # the fake ADOPTS the file's declared version — the adapter's
            # version gate must rehearse against whatever file was injected
            m = re.search(r'Puppeteer\.version\s*=\s*"([^"]+)"', code)
            if m is not None:
                self.version = m.group(1)
            self.injected = True
            self.injections += 1
            return [f"MOD_LOADED|{self.version}"]
        if "Puppeteer.Handshake" in code:
            if not self.injected:
                return ["MOD_PRESENT|false", "---END---"]
            return [
                "MOD_PRESENT|true",
                f"MOD_VERSION|{self.version}",
                f"SUPPORTS_FREEZE|{str(self.supports_freeze).lower()}",
                f"SUPPORTS_LEDGER|{str(self.supports_ledger).lower()}",
                "SUPPORTS_DIGEST|true",
                f"SUPPORTS_COMMAND_DIFF|{str(self.has_command_diff).lower()}",
            ]
        if "Puppeteer.Status" in code and not self.injected:
            return ["MOD_STATUS|unavailable"]
        if "Puppeteer.Digest" in code and not self.injected:
            return ["MOD_DIGEST|unavailable"]
        m = re.search(
            r"Puppeteer\.SetPuppet\(\s*(\d+)\s*,\s*(true|false)\s*\)", code)
        if m:
            pid, enabled = int(m.group(1)), m.group(2) == "true"
            self.puppets[pid] = enabled
            if not enabled and self.lease and self.lease["player"] == pid:
                self.lease = None
            return [f"PUPPET_SET|{pid}|{str(enabled).lower()}"]
        if "Puppeteer.Status" in code:
            if not self.has_status:
                return ["MOD_STATUS|unavailable"]
            return self._status_rows()
        if "Puppeteer.Digest" in code:
            if not self.has_digest:
                return ["MOD_DIGEST|unavailable"]
            return [self._digest()]
        if "NotificationManager.GetList" in code:
            if self.pending_blockers:
                return [*self.pending_blockers, "---END---"]
            return ["NONE", "---END---"]
        if "SetProgressingCivic" in code:
            self.pending_blockers = [
                b for b in self.pending_blockers
                if not b.endswith("ENDTURN_BLOCKING_CIVIC")]
            return ["CIVIC_SET|CIVIC_FAKE", "---END---"]
        if "RequestPolicyChanges" in code:
            self.pending_blockers = [
                b for b in self.pending_blockers
                if "FILL_CIVIC_SLOT" not in b]
            return ["POLICIES_SET|1|0:POLICY_FAKE", "---END---"]
        if "Puppeteer.Trace" in code:
            # the mod v0.3.1 hook ring (minimal model: the turn-start /
            # lease / deactivate events the driver's targeting reads)
            return ["\n".join(self.trace)] if self.trace else ["---END---"]
        if "Puppeteer.DiffSinceLast" in code:
            if not self.has_command_diff:
                return ["MOD_DIFF|unavailable"]
            if self.lease is None or self.mark is None:
                return ["---END---"]
            m = re.search(
                r"Puppeteer\.DiffSinceLast\('([^']*)',\s*(-?\d+)\)", code)
            attrs = set(m.group(1).split(",")) if m and m.group(1) else None
            seq = int(m.group(2)) if m else -1
            if self._diff_cache is not None and seq == self._diff_cache_seq:
                return ([self._diff_cache] if self._diff_cache
                        else ["---END---"])  # wire retry: same rows
            keep, drop = [], []
            for row in self._diff(self.lease["player"], self.mark):
                attr = row.split("|")[4] if row.count("|") >= 5 else ""
                (keep if attrs is None or attr in attrs else drop).append(row)
            self.ledger_rows.extend(drop)  # undeclared: booked immediately
            self.mark = self._snapshot(self.lease["player"])
            self._diff_cache = "\n".join(keep)
            self._diff_cache_seq = seq
            return ([self._diff_cache] if keep else ["---END---"])
        if "Puppeteer.DumpLedger" in code:
            rows, self.ledger_rows = self.ledger_rows, []
            return rows
        if "Puppeteer.DumpAmbient" in code:
            rows, self.ambient_rows = self.ambient_rows, []
            return rows
        m = re.search(
            r"Puppeteer\.Release\(\s*(\d+)\s*,\s*(-?\d+)\s*\)", code)
        if m or "Puppeteer.Release" in code:
            # mod v0.3.1 parity: TURN-BOUND — a release for turn N must
            # never drop the next turn's freshly-engaged lease (Codex P1-8)
            want_turn = int(m.group(2)) if m else -1
            if (self.lease is not None
                    and (want_turn == -1
                         or self.lease["turn"] == want_turn)):
                if self.mark is not None:
                    self.ledger_rows.extend(
                        self._diff(self.lease["player"], self.mark))
                    self.mark = None
                self.lease = None
                self._diff_cache = None
            return ["PUPPET_ACTIVE|false"]
        if "Puppeteer.FreezeUnit" in code:
            m = re.search(r"Puppeteer\.FreezeUnit\(\s*(\d+)\s*\)", code)
            if m:
                u = self.units.get(int(m.group(1)))
                if u is not None:
                    u["moves"] = 0
            return []  # silent, like the mod (it prints FROZEN| live)
        if "Puppeteer.RestoreUnit" in code:
            m = re.search(r"Puppeteer\.RestoreUnit\(\s*(\d+)\s*\)", code)
            if m and self.lease is not None:
                uid = int(m.group(1))
                # once per unit per lease (Codex P1-1): the SECOND restore
                # for the same unit in one lease is a no-op
                if uid not in self._restored:
                    u = self.units.get(uid)
                    if u is not None:
                        u["moves"] = 2
                    self._restored.add(uid)
            return []  # silent, like the mod
        m = re.search(r"Puppeteer\.FinishAllMoves\(\s*(\d+)\s*\)", code)
        if m:
            return [f"FINISHED_MOVES|{m.group(1)}|0"]
        if "UI.RequestAction(ActionTypes.ACTION_ENDTURN)" in code:
            # D7-H1 rehearsal: the LOCAL player's end-turn via the UI bus
            # (InGame VM — no Puppeteer, no SetLocalPlayerAndObserver
            # there, live-learned). The engine honors it; the lease drops
            # (OnPlayerTurnDeactivated) and the turn advances only when
            # the driver simulates it.
            if self.lease is not None and self.mark is not None:
                self.ledger_rows.extend(
                    self._diff(self.lease["player"], self.mark))
                self.mark = None
            self.lease = None
            self._diff_cache = None
            self.turn_active = False
            return ["PUPPET_ACTIVE|false", "ENDTURN_SENT|0"]
        m = re.search(r"Puppeteer\.BeginAmbientWindow\(\s*(\d+)\s*\)", code)
        if m:
            return [f"AMBIENT_WINDOW|open|{m.group(1)}"]
        m = re.search(r"Puppeteer\.EndAmbientWindow\(\s*(\d+)\s*\)", code)
        if m:
            # engine effects land inside the window (auto_ambient fixture):
            # the window diff books them as DECLARED ambient rows
            if self.auto_ambient is not None:
                kind, etype, num, attr, before, after = self.auto_ambient
                prefix = "c" if etype == "city" else "u"
                self.ambient_rows.append(
                    f"AMBIENT|{kind}|{etype}|{prefix}{num}"
                    f"|{attr}|{before}|{after}")
            return [f"AMBIENT_WINDOW|closed|{m.group(1)}"]

        # -- Simulate.*: FAKE-ONLY (the live driver must never send these) --
        m = re.search(r"Simulate\.TurnStart\(\s*(\d+)\s*\)", code)
        if m:
            pid = int(m.group(1))
            self.turn_active = True
            self.trace.append(f"{self.turn}|HOOK_ENTER|{pid}")
            if self.puppets.get(pid):
                self.lease = {"player": pid, "turn": self.turn}
                self.mark = self._snapshot(pid)
                self._restored = set()
                self._diff_cache = None
                self.trace.append(
                    f"{self.turn}|LEASE_SET|{pid}|{self.turn}")
            return []  # hook print is unsolicited => drained: no rows
        m = re.search(r"Simulate\.TurnStartAt\(\s*(\d+)\s*,\s*(\d+)\s*\)", code)
        if m:
            # targeted variant: the engine reached this turn and the hook
            # fired for the puppet (the attach-while-parked path)
            pid, turn = int(m.group(1)), int(m.group(2))
            if turn > self.turn:
                self.turn = turn
            self.turn_active = True
            self.trace.append(f"{turn}|HOOK_ENTER|{pid}")
            if self.puppets.get(pid):
                self.lease = {"player": pid, "turn": turn}
                self.mark = self._snapshot(pid)
                self._restored = set()
                self._diff_cache = None
                self.trace.append(f"{turn}|LEASE_SET|{pid}|{turn}")
            return []
        m = re.search(r"Simulate\.TurnDeactivated\(\s*(\d+)\s*\)", code)
        if m:
            pid = int(m.group(1))
            self.turn_active = False
            self.trace.append(f"{self.turn}|HOOK_DEACT|{pid}")
            if self.lease and self.lease["player"] == pid:
                # the real hook calls Release: book remaining drift (P2-2)
                if self.mark is not None:
                    self.ledger_rows.extend(self._diff(pid, self.mark))
                    self.mark = None
                self.lease = None
                self._diff_cache = None
            return []
        if "Simulate.AdvanceTurn" in code:
            self.turn += 1
            # engine turn-end effects: per-OWNER-city income lands with the
            # advance (AFTER the adapter's pre-endturn seal — the live
            # run-004 bracket)
            for pid, p in self.players.items():
                owned = sum(1 for c in self.cities.values()
                            if c["owner"] == pid)
                p["gold"] += 5 * owned
            return []
        if "Simulate.Mutate" in code:
            self.state_nonce += 1
            return []
        m = re.search(
            r"Simulate\.Ledger\(\s*(\w+)\s*,\s*(\w+)\s*,\s*(\d+)\s*,"
            r"\s*(\w+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\)", code)
        if m:
            self.ledger_rows.append(
                f"LEDGER|{self.turn}|0|u{m.group(3)}|{m.group(1)}"
                f"|{m.group(5)}|{m.group(6)}")
            return []
        m = re.search(
            r"Simulate\.Ambient\(\s*(\w+)\s*,\s*(\w+)\s*,\s*(\d+)\s*,"
            r"\s*(\w+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\)", code)
        if m:
            self.ambient_rows.append(
                f"AMBIENT|{m.group(1)}|{m.group(2)}|{m.group(3)}"
                f"|{m.group(4)}|{m.group(5)}|{m.group(6)}")
            return []
        return None


class FakeTunerServer:
    """Scripted responses: list of (state_index, substring) -> output lines."""

    def __init__(
        self,
        responses: list[tuple[int, str, list[str]]] | None = None,
        app_identity: str = APP_IDENTITY,
        mod: FakeMod | None = None,
    ) -> None:
        self.responses = responses or []
        self.app_identity = app_identity
        self.mod = mod
        self.received_commands: list[str] = []
        self._server: asyncio.AbstractServer | None = None
        self._peers: set[asyncio.StreamWriter] = set()
        self.port: int | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is None:
            return
        # Close lingering client connections first: wait_closed() blocks until
        # every handler returns, and a handler parks on recv until its client
        # disconnects. A test that fails before adapter.teardown() must not
        # hang the suite.
        for writer in list(self._peers):
            writer.close()
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._peers.add(writer)
        try:
            while True:
                try:
                    msg = await tuner_client.recv_message(reader)
                except (asyncio.IncompleteReadError, ConnectionResetError):
                    return
                if msg.tag == tuner_client.TAG_HANDSHAKE:
                    await self._handle_handshake(msg, writer)
                elif msg.tag == tuner_client.TAG_COMMAND:
                    await self._handle_command(msg, writer)
        finally:
            self._peers.discard(writer)
            writer.close()

    async def _handle_handshake(
        self, msg: tuner_client.Message, writer: asyncio.StreamWriter
    ) -> None:
        if msg.payload == "APP:":
            await tuner_client.send_message(
                writer, tuner_client.TAG_HANDSHAKE, self.app_identity)
        elif msg.payload == "LSQ:":
            # newline-separated alternating [index, name] pairs
            payload = "0\nGameCore_Tuner\n1\nInGame"
            await tuner_client.send_message(
                writer, tuner_client.TAG_HANDSHAKE, payload)

    async def _handle_command(
        self, msg: tuner_client.Message, writer: asyncio.StreamWriter
    ) -> None:
        self.received_commands.append(msg.payload)
        # payload shape: "CMD:{state_index}:{code}"
        parts = msg.payload.split(":", 2)
        try:
            state_index = int(parts[1])
        except (IndexError, ValueError):
            state_index = -1
        code = parts[2] if len(parts) == 3 else ""

        lines: list[str] | None = None
        for resp_state, substring, resp_lines in self.responses:
            if resp_state == state_index and substring in msg.payload:
                lines = resp_lines
                break
        if lines is None and self.mod is not None:
            lines = self.mod.respond(code)
        if lines is None:
            lines = ["ERR:FAKE unknown command"]

        context = LUA_STATES.get(state_index, "GameCore_Tuner")
        for line in lines:
            if line.startswith("ERR:"):
                # raw error payloads make the vendored connection raise LuaError
                await tuner_client.send_message(
                    writer, tuner_client.TAG_COMMAND, line)
                return
            await tuner_client.send_message(
                writer, tuner_client.TAG_COMMAND, f"O\x00{context}: {line}")
        # always terminate with the sentinel
        await tuner_client.send_message(
            writer, tuner_client.TAG_COMMAND, f"O\x00{context}: ---END---")
