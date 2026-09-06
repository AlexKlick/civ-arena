"""Opt-in units-only framing on an existing tuner connection; never replay a request.

Native compatibility is unproven. This module does not connect, discover states,
change global Lua print, admit caller Lua, or route any mutation through framing.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import math
import re
import struct
import time
import uuid
from collections.abc import Callable
from typing import Any

from civ_arena.game.civ6 import lua_translator, response_parser
from civ_arena.game.civ6.vendor import tuner_client
from civ_arena.game.civ6.vendor.connection import GameConnection

PROTOCOL = 'CIVARENA_UNITS_V1'
CONTEXT = 'GameCore_Tuner'
MAX_FRAME_BYTES = 1024 * 1024
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_RECORDS = 8192
CHUNK_BYTES = 384
MAX_UNBOUND_MESSAGES = 256
MAX_UNBOUND_BYTES = 64 * 1024
_TOKEN = re.compile(r'[0-9a-f]{32}_[1-9][0-9]{0,15}\Z')
_DECIMAL = re.compile(r'(?:0|[1-9][0-9]{0,6})\Z')
_HEX = re.compile(r'(?:[0-9a-f]{2}){1,384}\Z')


class FramedObservationError(RuntimeError):
    """Fixed-category failure; partial rows and arbitrary wire error text never escape."""


def _units_lua(token: str, state: int) -> str:
    """Private structured builder. Lexical print captures only the exact units read."""
    if not _TOKEN.fullmatch(token) or type(state) is not int or not 0 <= state < 2**31:
        raise ValueError('invalid framed units identity')
    tag = f'{PROTOCOL}|{token}|{state}'
    return '''do
local emit = print
if type(emit) ~= 'function' then error('framed_units_print_unavailable') end
local tag = "''' + tag + '''"
if type(pcall) ~= 'function' or type(select) ~= 'function'
 or type(tostring) ~= 'function' or type(assert) ~= 'function'
 or type(table) ~= 'table' or type(table.concat) ~= 'function'
 or type(string) ~= 'table' or type(string.sub) ~= 'function'
 or type(string.format) ~= 'function' or type(string.byte) ~= 'function'
 or type(string.char) ~= 'function' or type(PlayerManager) ~= 'table'
 or type(PlayerManager.GetAlive) ~= 'function' then
 emit(tag..'|ERR|capability_unavailable')
 return
end
local records = {}
local total = 0
local function capture(...)
 local fields = {}
 for i=1,select('#',...) do fields[i]=tostring(select(i,...)) end
 local text=table.concat(fields,string.char(9))
 local record=tostring(#text)..':'..text
 total=total+#record
 assert(total<=262144 and #records<8192,'capture_bound')
 records[#records+1]=record
end
local ok=pcall(function()
 local print=capture
''' + lua_translator.units_read() + '''
end)
if not ok then emit(tag..'|ERR|runtime_failure') return end
local data=table.concat(records)
local chunk=0
for first=1,#data,384 do
 local part=string.sub(data,first,first+383)
 local hex={}
 for i=1,#part do hex[i]=string.format('%02x',string.byte(part,i)) end
 emit(tag..'|DATA|'..chunk..'|'..table.concat(hex))
 chunk=chunk+1
end
emit(tag..'|END|'..chunk..'|'..#data..'|'..#records)
end'''


def _number(value: str, maximum: int) -> int:
    if not _DECIMAL.fullmatch(value) or int(value) > maximum:
        raise FramedObservationError('invalid_frame_count')
    return int(value)


def _records(data: bytes, count: int) -> list[str]:
    rows = []
    while data:
        length, separator, rest = data.partition(b':')
        if not separator:
            raise FramedObservationError('invalid_record_length')
        try:
            size = _number(length.decode('ascii'), MAX_PAYLOAD_BYTES)
        except UnicodeError:
            raise FramedObservationError('invalid_record_length') from None
        if len(rest) < size or len(rows) >= MAX_RECORDS:
            raise FramedObservationError('record_bound_or_truncation')
        try:
            rows.append(rest[:size].decode('utf-8', errors='strict'))
        except UnicodeError:
            raise FramedObservationError('invalid_record_encoding') from None
        data = rest[size:]
    if len(rows) != count or not rows or rows[-1] != '---END---':
        raise FramedObservationError('missing_or_mismatched_builder_terminal')
    # units_read emits exactly this header and one final sentinel. Repeated
    # headers/sentinels must not be silently accepted by the historical parser.
    if rows[0] != 'UNITS|1' or rows.count('UNITS|1') != 1 or rows.count('---END---') != 1:
        raise FramedObservationError('invalid_units_envelope')
    return rows[:-1]


async def _receive(reader: asyncio.StreamReader) -> tuple[int, str]:
    header = await reader.readexactly(8)
    size, tag = struct.unpack('<Ii', header)
    if not 1 <= size <= MAX_FRAME_BYTES:
        raise FramedObservationError('invalid_nexus_frame_size')
    data = await reader.readexactly(size)
    if not data.endswith(b'\0'):
        raise FramedObservationError('missing_nexus_terminator')
    try:
        return tag, data[:-1].decode('utf-8', errors='strict')
    except UnicodeError:
        raise FramedObservationError('invalid_nexus_encoding') from None


class FramedUnitObservations:
    """Uses one existing connection/lock, with a fresh token and result on every read.

    Once I/O is attempted, any error permanently invalidates this helper and closes
    its captured stream. A replacement connection is never adopted implicitly.
    Diagnostics are bounded, redacted, independent copies; callback failure cannot
    turn a failed read into success or discard a successfully parsed observation.
    """

    def __init__(self, connection: GameConnection, *,
                 audit: Callable[[dict], None] | None = None) -> None:
        self._connection = connection
        self._audit = audit
        self._epoch = uuid.uuid4().hex
        self._sequence = 0
        self._generation: tuple | None = None
        self._poisoned = False
        self._last: dict | None = None

    @property
    def last_exchange(self) -> dict | None:
        return copy.deepcopy(self._last)

    def bound_to(self, connection: GameConnection) -> bool:
        return self._connection is connection

    def _identity(self) -> tuple:
        conn = self._connection
        states = [index for index, name in conn.lua_states.items() if name == CONTEXT]
        if (not conn.is_connected or conn._reader is None or conn._writer is None
                or len(states) != 1 or type(states[0]) is not int
                or not 0 <= states[0] < 2**31 or conn.gamecore_index != states[0]):
            raise FramedObservationError('connection_or_gamecore_not_admitted')
        return conn._reader, conn._writer, states[0], tuple(sorted(conn.lua_states.items()))

    def _unchanged(self, identity: tuple) -> None:
        if self._identity() != identity:
            raise FramedObservationError('connection_generation_changed')

    def _publish(self, trace: dict) -> None:
        self._last = copy.deepcopy(trace)
        if self._audit is not None:
            try:
                self._audit(copy.deepcopy(trace))
            except Exception:
                self._last['diagnostic_callback_failed'] = True

    async def read_units(self, *, timeout: float = 5.0) -> list[dict[str, Any]]:
        """No caller Lua, state argument, connect/reconnect, cache or retry surface."""
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('framed units timeout must be positive and finite')
        if self._poisoned:
            raise FramedObservationError('framed_connection_poisoned')
        start = time.monotonic()
        trace: dict = {'protocol': PROTOCOL, 'operation': 'units_read',
                       'status': 'not_sent', 'unbound_messages': 0, 'unbound_bytes': 0,
                       'unbound_sha256': [], 'frames': 0, 'payload_bytes': 0}
        started = False
        writer = None
        try:
            async with asyncio.timeout(timeout):
                async with self._connection._lock:
                    if self._poisoned:
                        raise FramedObservationError('framed_connection_poisoned')
                    identity = self._identity()
                    if self._generation is not None and identity != self._generation:
                        raise FramedObservationError('connection_generation_changed')
                    self._generation = identity
                    reader, writer, state, _ = identity
                    self._sequence += 1
                    token = f'{self._epoch}_{self._sequence}'
                    lua = _units_lua(token, state)
                    trace.update(token=token, state_index=state,
                                 source_sha256=hashlib.sha256(lua.encode()).hexdigest())
                    # Mark before write/drain: cancellation during send is ambiguous.
                    started = True
                    trace['status'] = 'sent'
                    await tuner_client.send_message(writer, tuner_client.TAG_COMMAND,
                                                    f'CMD:{state}:{lua}')
                    self._unchanged(identity)
                    rows = await self._collect(reader, identity, token, trace)
                    self._unchanged(identity)
                    try:
                        units = response_parser.parse_units(rows, qualified=True)
                        if any(type(unit.get('is_barbarian')) is not bool for unit in units):
                            raise ValueError('framed builder requires native classification')
                    except ValueError:
                        raise FramedObservationError('invalid_units_payload') from None
                    trace.update(status='completed', rows=len(rows), units=len(units))
                    return units
        except BaseException as exc:
            if started:
                self._poisoned = True
                # Close only the captured writer, never an unrelated replacement.
                if writer is not None:
                    try:
                        writer.close()
                        trace['stream_close'] = 'requested'
                    except Exception:
                        trace['stream_close'] = 'failed'
            trace.update(status='failed' if started else 'not_sent',
                         failure=type(exc).__name__, poisoned=self._poisoned)
            if isinstance(exc, FramedObservationError):
                trace['reason'] = str(exc)
            raise
        finally:
            trace['elapsed_s'] = time.monotonic() - start
            self._publish(trace)

    async def _collect(self, reader: asyncio.StreamReader, identity: tuple,
                       token: str, trace: dict) -> list[str]:
        chunks: list[bytes] = []
        state = identity[2]
        while True:
            tag, payload = await _receive(reader)
            self._unchanged(identity)
            if payload.startswith('ERR:'):
                raise FramedObservationError('uncorrelated_native_error')
            if tag != tuner_client.TAG_COMMAND:
                raise FramedObservationError('unexpected_nexus_tag')
            if payload == '':  # Native command ACK; not an observation terminator.
                self._unbound(payload, trace)
                continue
            if not payload.startswith('O\0'):
                raise FramedObservationError('malformed_output_envelope')
            context, separator, text = payload[2:].partition(': ')
            if not separator or '\0' in text:
                raise FramedObservationError('malformed_output_envelope')
            completed = None
            for line in text.splitlines():
                fields = line.split('|')
                if fields[:2] != [PROTOCOL, token]:
                    self._unbound(line, trace)
                    continue
                if completed is not None:
                    raise FramedObservationError('frame_after_terminal')
                if (len(fields) < 4 or context != CONTEXT or fields[2] != str(state)):
                    raise FramedObservationError('response_identity_mismatch')
                trace['frames'] += 1
                if fields[3] == 'ERR':
                    if len(fields) != 5 or fields[4] not in ('runtime_failure',
                                                            'capability_unavailable'):
                        raise FramedObservationError('malformed_correlated_error')
                    raise FramedObservationError(fields[4])
                if fields[3] == 'DATA':
                    if (len(fields) != 6 or _number(fields[4], MAX_RECORDS) != len(chunks)
                            or not _HEX.fullmatch(fields[5])):
                        raise FramedObservationError('invalid_or_out_of_order_chunk')
                    block = bytes.fromhex(fields[5])
                    trace['payload_bytes'] += len(block)
                    if trace['payload_bytes'] > MAX_PAYLOAD_BYTES:
                        raise FramedObservationError('payload_bound_exceeded')
                    chunks.append(block)
                elif fields[3] == 'END':
                    if (len(fields) != 7 or _number(fields[4], MAX_RECORDS) != len(chunks)
                            or _number(fields[5], MAX_PAYLOAD_BYTES) != trace['payload_bytes']):
                        raise FramedObservationError('terminal_count_mismatch')
                    data = b''.join(chunks)
                    rows = _records(data, _number(fields[6], MAX_RECORDS))
                    trace['response_sha256'] = hashlib.sha256(data).hexdigest()
                    completed = rows
                else:
                    raise FramedObservationError('unknown_response_frame')
            if completed is not None:
                return completed

    @staticmethod
    def _unbound(text: str, trace: dict) -> None:
        trace['unbound_messages'] += 1
        trace['unbound_bytes'] += len(text.encode())
        if len(trace['unbound_sha256']) < 8:
            trace['unbound_sha256'].append(hashlib.sha256(text.encode()).hexdigest())
        if (trace['unbound_messages'] > MAX_UNBOUND_MESSAGES
                or trace['unbound_bytes'] > MAX_UNBOUND_BYTES):
            raise FramedObservationError('unsolicited_output_bound_exceeded')
