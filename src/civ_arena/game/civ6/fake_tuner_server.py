"""In-process Civ VI tuner stand-in speaking the real Firaxis Nexus framing.

Test infrastructure: lets the vendored wire layer, the Lua translator, and
the response parser all be exercised without a live game — the same
game-free philosophy as upstream's _StubConnection, but at the socket layer.

Two response sources, first match wins:

1. the canned ``(state_index, substring) -> lines`` list (explicit,
   per-test overrides), then
2. an optional :class:`FakeMod` — a scripted PuppeteerMod state machine
   that rehearses mod-shaped commands game-free.

Anything else is ``ERR:FAKE unknown command``.
"""

from __future__ import annotations

import asyncio
import re

from civ_arena.game.civ6.vendor import tuner_client

APP_IDENTITY = "FakeSidMeiersCivilizationVI"

# mirrors the LSQ: handshake answers below
LUA_STATES = {0: "GameCore_Tuner", 1: "InGame"}


class FakeMod:
    """Scripted PuppeteerMod stand-in (mod >= 0.2 shapes).

    Answers the pipe rows the real mod prints, keeps puppet/lease state
    across commands, and exposes ``Simulate.*`` FAKE-ONLY commands tests
    use to fire engine-side events the wire cannot (turn-start hooks,
    engine drift, ledger/ambient rows). Faithful to D3 (poll, never
    push): hook-driven prints are UNSOLICITED in the real wire and get
    drained, so ``Simulate.TurnStart`` changes state silently and the
    change surfaces only via the next Status/Digest poll — exactly the
    discipline the live driver must use.
    """

    def __init__(
        self,
        version: str = "0.2.0-rehearsal",
        has_status: bool = True,
        has_digest: bool = True,
        supports_freeze: bool = True,
        supports_ledger: bool = True,
    ) -> None:
        self.version = version
        self.has_status = has_status
        self.has_digest = has_digest
        self.supports_freeze = supports_freeze
        self.supports_ledger = supports_ledger
        self.puppets: dict[int, bool] = {}
        self.turn = 1
        self.lease: dict[str, int] | None = None
        self.ledger_rows: list[str] = []
        self.ambient_rows: list[str] = []
        self.state_nonce = 0

    # -- digest: a pure function of state (zero-drift rehearses as equal) --
    def _digest(self) -> str:
        puppets = ",".join(str(p) for p in sorted(self.puppets)) or "none"
        lease = ("none" if self.lease is None
                 else f"p{self.lease['player']}t{self.lease['turn']}")
        return (f"DIGEST|rehearsal|turn={self.turn}|puppets={puppets}"
                f"|lease={lease}|nonce={self.state_nonce}")

    def _status_rows(self) -> list[str]:
        active = self.lease is not None
        player = self.lease["player"] if self.lease else -1
        turn = self.lease["turn"] if self.lease else -1
        return [
            f"TURN|{self.turn}",
            f"PUPPET_ACTIVE|{str(active).lower()}",
            f"LEASE_PLAYER|{player}",
            f"LEASE_TURN|{turn}",
        ]

    def respond(self, code: str) -> list[str] | None:
        """Rows for a command's Lua code, or None if not mod-shaped."""
        if "Puppeteer.Handshake" in code:
            return [
                "MOD_PRESENT|true",
                f"MOD_VERSION|{self.version}",
                f"SUPPORTS_FREEZE|{str(self.supports_freeze).lower()}",
                f"SUPPORTS_LEDGER|{str(self.supports_ledger).lower()}",
                "SUPPORTS_DIGEST|true",
            ]
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
        if "Puppeteer.DumpLedger" in code:
            rows, self.ledger_rows = self.ledger_rows, []
            return rows
        if "Puppeteer.DumpAmbient" in code:
            rows, self.ambient_rows = self.ambient_rows, []
            return rows
        if "Puppeteer.Release" in code:
            self.lease = None
            return ["PUPPET_ACTIVE|false"]
        if "Puppeteer.RestoreUnit" in code:
            return []  # silent, like the mod
        m = re.search(r"Puppeteer\.BeginAmbientWindow\(\s*(\d+)\s*\)", code)
        if m:
            return [f"AMBIENT_WINDOW|open|{m.group(1)}"]
        m = re.search(r"Puppeteer\.EndAmbientWindow\(\s*(\d+)\s*\)", code)
        if m:
            return [f"AMBIENT_WINDOW|closed|{m.group(1)}"]

        # -- Simulate.*: FAKE-ONLY (the live driver must never send these) --
        m = re.search(r"Simulate\.TurnStart\(\s*(\d+)\s*\)", code)
        if m:
            pid = int(m.group(1))
            if self.puppets.get(pid):
                self.lease = {"player": pid, "turn": self.turn}
            return []  # hook print is unsolicited => drained: no rows
        m = re.search(r"Simulate\.TurnDeactivated\(\s*(\d+)\s*\)", code)
        if m:
            pid = int(m.group(1))
            if self.lease and self.lease["player"] == pid:
                self.lease = None
            return []
        if "Simulate.AdvanceTurn" in code:
            self.turn += 1
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
