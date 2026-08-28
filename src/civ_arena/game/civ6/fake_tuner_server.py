"""In-process Civ VI tuner stand-in speaking the real Firaxis Nexus framing.

Test infrastructure: lets the vendored wire layer, the Lua translator, and
the response parser all be exercised without a live game — the same
game-free philosophy as upstream's _StubConnection, but at the socket layer.
"""

from __future__ import annotations

import asyncio

from civ_arena.game.civ6.vendor import tuner_client

APP_IDENTITY = "FakeSidMeiersCivilizationVI"


class FakeTunerServer:
    """Scripted responses: list of (state_index, substring) -> output lines."""

    def __init__(
        self,
        responses: list[tuple[int, str, list[str]]] | None = None,
        app_identity: str = APP_IDENTITY,
    ) -> None:
        self.responses = responses or []
        self.app_identity = app_identity
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
        try:
            state_index = int(msg.payload.split(":", 2)[1])
        except (IndexError, ValueError):
            state_index = -1

        lines: list[str] | None = None
        for resp_state, substring, resp_lines in self.responses:
            if resp_state == state_index and substring in msg.payload:
                lines = resp_lines
                break
        if lines is None:
            lines = ["ERR:FAKE unknown command"]

        for line in lines:
            if line.startswith("ERR:"):
                # raw error payloads make the vendored connection raise LuaError
                await tuner_client.send_message(
                    writer, tuner_client.TAG_COMMAND, line)
                return
            await tuner_client.send_message(
                writer, tuner_client.TAG_COMMAND, f"O\x00GameCore_Tuner: {line}")
        # always terminate with the sentinel
        await tuner_client.send_message(
            writer, tuner_client.TAG_COMMAND, "O\x00GameCore_Tuner: ---END---")
