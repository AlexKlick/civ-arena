"""Framed unit observations over fake Nexus bytes and local real Lua; no live calls."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import struct
import subprocess
import traceback
from unittest.mock import AsyncMock

import pytest

from civ_arena.game.adapter import ObserveKind, ObserveRequest
from civ_arena.game.civ6 import lua_translator
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from civ_arena.game.civ6.framed_observations import (
    CONTEXT,
    MAX_FRAME_BYTES,
    MAX_PAYLOAD_BYTES,
    PROTOCOL,
    FramedObservationError,
    FramedUnitObservations,
    _units_lua,
)
from civ_arena.game.civ6.vendor.connection import GameConnection

TOKEN = 'a' * 32 + '_1'
ROW = 'UNITROW|u0:131073|0|WARRIOR|0|0|100|2|2|20|0|false|false'
TAG = re.compile(r'local tag = "CIVARENA_UNITS_V1\|([^|]+)\|([0-9]+)"')


def records(rows):
    return b''.join(str(len(row.encode())).encode() + b':' + row.encode() for row in rows)


def frames(token=TOKEN, state=2, rows=None):
    rows = ['UNITS|1', ROW, '---END---'] if rows is None else rows
    payload = records(rows)
    chunks = [payload[i:i + 384] for i in range(0, len(payload), 384)]
    base = f'{PROTOCOL}|{token}|{state}'
    return [f'{base}|DATA|{i}|{chunk.hex()}' for i, chunk in enumerate(chunks)] + [
        f'{base}|END|{len(chunks)}|{len(payload)}|{len(rows)}']


def packet(text, *, context=CONTEXT, tag=3, raw=False):
    payload = text if raw else 'O\0' + context + ': ' + text
    encoded = payload.encode() + b'\0'
    return struct.pack('<Ii', len(encoded), tag) + encoded


class Writer:
    def __init__(self, reader, plan):
        self.reader = reader
        self.plan = plan
        self.sent = []
        self.closed = False
        self.drain = AsyncMock()

    def write(self, data):
        payload = data[8:-1].decode()
        self.sent.append(payload)
        token, state = TAG.search(payload).groups()
        for delay, part in self.plan(token, int(state)):
            asyncio.get_running_loop().call_later(delay, self.feed, part)

    def feed(self, part):
        if not self.closed:
            if part is None:
                self.reader.feed_eof()
            else:
                self.reader.feed_data(part)

    def close(self):
        self.closed = True

    def is_closing(self):
        return self.closed


def connection(plan=None, *, audit=None):
    if plan is None:
        def plan(token, state):
            return [(0, packet(line)) for line in frames(token, state)]
    conn = GameConnection()
    reader = asyncio.StreamReader()
    writer = Writer(reader, plan)
    conn._reader, conn._writer = reader, writer
    conn.lua_states = {2: CONTEXT, 9: 'InGame'}
    conn.gamecore_index = 2
    conn.ingame_index = 9
    conn.connect = AsyncMock(side_effect=AssertionError('must not connect'))
    conn.reconnect = AsyncMock(side_effect=AssertionError('must not replay'))
    return conn, writer, FramedUnitObservations(conn, audit=audit)


@pytest.mark.parametrize('combined,fragmented', [(False, False), (True, False), (True, True)])
async def test_fresh_exact_units_from_separate_combined_and_fragmented_nexus(combined, fragmented):
    def plan(token, state):
        output = frames(token, state)
        wire = ([packet('\n'.join(output))] if combined else [packet(x) for x in output])
        if fragmented:
            blob = b''.join(wire)
            wire = [blob[:3], blob[3:9], blob[9:23], blob[23:]]
        return [(0, piece) for piece in wire]
    audits = []
    conn, writer, client = connection(plan, audit=audits.append)
    units = await client.read_units()
    assert units[0]['unit_id'] == 'u0:131073' and units[0]['is_barbarian'] is False
    assert len(writer.sent) == 1 and writer.sent[0].startswith('CMD:2:')
    assert lua_translator.units_read() in writer.sent[0]
    assert 'RequestOperation' not in writer.sent[0]
    assert audits[0]['status'] == 'completed' and audits[0]['units'] == 1
    units[0]['q'] = 99
    snapshot = client.last_exchange
    snapshot['status'] = 'tampered'
    again = await client.read_units()
    assert again[0]['q'] == 0 and client.last_exchange['status'] == 'completed'
    assert audits[0]['token'] != audits[1]['token']
    conn.connect.assert_not_called()
    conn.reconnect.assert_not_called()


async def test_stale_generic_old_nonce_and_other_vm_do_not_complete_or_contaminate():
    secret = 'private stale diagnostic'
    def plan(token, state):
        stale = [packet('---END---'), packet(secret, context='InGame')]
        stale += [packet(row, context='InGame') for row in frames('b' * 32 + '_1', 9)]
        return [(0, part) for part in stale] + [(0.003, packet(row))
                                               for row in frames(token, state)]
    _, writer, client = connection(plan)
    assert len(await client.read_units()) == 1
    assert client.last_exchange['unbound_messages'] == 4
    assert secret not in json.dumps(client.last_exchange)
    assert not writer.closed


@pytest.mark.parametrize('mode', [
    'native_error', 'runtime_error', 'capability_error', 'wrong_context', 'wrong_state',
    'duplicate_chunk', 'bad_chunk_number', 'odd_hex', 'hex_whitespace', 'wrong_bytes',
    'wrong_rows', 'missing_header', 'duplicate_header', 'missing_builder_end',
    'duplicate_builder_end', 'duplicate_unit', 'foreign_owner_mismatch',
    'nonunit_row', 'unsupported_tag', 'bad_output_envelope', 'embedded_null',
    'zero_frame_length', 'oversize_header', 'invalid_utf8', 'missing_nexus_null',
    'unknown_frame', 'noncanonical_count', 'record_length_mismatch', 'unbound_storm',
])
async def test_corruption_fails_without_partial_units_or_replay(mode):
    def plan(token, state):
        output = frames(token, state)
        first = output[0].split('|')
        end = output[-1].split('|')
        raw = None
        context, tag = CONTEXT, 3
        if mode == 'native_error':
            raw = packet('ERR:private native failure', raw=True)
        elif mode in ('runtime_error', 'capability_error'):
            reason = 'runtime_failure' if mode == 'runtime_error' else 'capability_unavailable'
            output = [f'{PROTOCOL}|{token}|{state}|ERR|{reason}']
        elif mode == 'wrong_context':
            context = 'InGame'
        elif mode == 'wrong_state':
            output = frames(token, state + 1)
        elif mode == 'duplicate_chunk':
            output.insert(1, output[0])
        elif mode == 'bad_chunk_number':
            first[4] = '1'
            output[0] = '|'.join(first)
        elif mode in ('odd_hex', 'hex_whitespace'):
            first[5] += 'f' if mode == 'odd_hex' else ' '
            output[0] = '|'.join(first)
        elif mode in ('wrong_bytes', 'wrong_rows', 'noncanonical_count'):
            end[5 if mode == 'wrong_bytes' else 6] = '99' if mode != 'noncanonical_count' else '03'
            output[-1] = '|'.join(end)
        elif mode in ('missing_header', 'duplicate_header', 'missing_builder_end',
                      'duplicate_builder_end', 'duplicate_unit', 'foreign_owner_mismatch',
                      'nonunit_row'):
            rows = ['UNITS|1', ROW, '---END---']
            if mode == 'missing_header':
                rows.pop(0)
            elif mode == 'duplicate_header':
                rows.insert(0, 'UNITS|1')
            elif mode == 'missing_builder_end':
                rows.pop()
            elif mode == 'duplicate_builder_end':
                rows.append('---END---')
            elif mode == 'duplicate_unit':
                rows.insert(1, ROW)
            elif mode == 'foreign_owner_mismatch':
                rows[1] = ROW.replace('|0|WARRIOR', '|1|WARRIOR')
            else:
                rows[1] = 'CITYROW|private invalid data'
            output = frames(token, state, rows)
        elif mode == 'unsupported_tag':
            tag = 4
        elif mode == 'bad_output_envelope':
            raw = packet('Omissing separator', raw=True)
        elif mode == 'embedded_null':
            raw = packet('bad\0output')
        elif mode == 'zero_frame_length':
            raw = struct.pack('<Ii', 0, 3)
        elif mode == 'oversize_header':
            raw = struct.pack('<Ii', MAX_FRAME_BYTES + 1, 3)
        elif mode == 'invalid_utf8':
            raw = struct.pack('<Ii', 2, 3) + b'\xff\0'
        elif mode == 'missing_nexus_null':
            raw = struct.pack('<Ii', 2, 3) + b'xx'
        elif mode == 'unknown_frame':
            output = [f'{PROTOCOL}|{token}|{state}|UNKNOWN']
        elif mode == 'record_length_mismatch':
            raw_data = b'999:short'
            output = [f'{PROTOCOL}|{token}|{state}|DATA|0|{raw_data.hex()}',
                      f'{PROTOCOL}|{token}|{state}|END|1|{len(raw_data)}|1']
        else:
            output = ['stale'] * 257
        return [(0, raw)] if raw is not None else [(0, packet(row, context=context, tag=tag))
                                                  for row in output]
    conn, writer, client = connection(plan)
    with pytest.raises(FramedObservationError):
        await client.read_units(timeout=.2)
    assert writer.closed and len(writer.sent) == 1
    assert client.last_exchange['status'] == 'failed'
    assert 'private' not in json.dumps(client.last_exchange)
    with pytest.raises(FramedObservationError, match='poisoned'):
        await client.read_units()
    assert len(writer.sent) == 1
    conn.reconnect.assert_not_called()


@pytest.mark.parametrize('mode', ['timeout', 'cancel', 'partial_header', 'eof', 'send_cancel'])
async def test_ambiguous_io_poison_is_permanent_and_never_resends(mode):
    def plan(_token, _state):
        return ([(0, b'\x01\x00\x00\x00')] if mode == 'partial_header' else
                [(0, None)] if mode == 'eof' else [])
    conn, writer, client = connection(plan)
    if mode == 'send_cancel':
        async def delayed_drain():
            await asyncio.sleep(1)
        writer.drain = AsyncMock(side_effect=delayed_drain)
    task = asyncio.create_task(client.read_units(timeout=.015))
    if mode in ('cancel', 'send_cancel'):
        await asyncio.sleep(.001)
        task.cancel()
    with pytest.raises((TimeoutError, asyncio.CancelledError, asyncio.IncompleteReadError)):
        await task
    assert writer.closed and len(writer.sent) == 1
    replacement = Writer(asyncio.StreamReader(), lambda *_: [])
    conn._writer = replacement
    with pytest.raises(FramedObservationError, match='poisoned'):
        await client.read_units()
    assert not replacement.closed and not replacement.sent
    conn.reconnect.assert_not_called()


@pytest.mark.parametrize('cancel', [False, True])
async def test_lock_wait_failure_neither_sends_nor_disturbs_owner(cancel):
    _, writer, client = connection(lambda t, s: [(.02, packet(row)) for row in frames(t, s)])
    owner = asyncio.create_task(client.read_units(timeout=.1))
    await asyncio.sleep(.001)
    waiter = asyncio.create_task(client.read_units(timeout=.003))
    if cancel:
        await asyncio.sleep(.001)
        waiter.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await waiter
    assert len(await owner) == 1 and not writer.closed and len(writer.sent) == 1
    assert len(await client.read_units()) == 1


@pytest.mark.parametrize('when', ['before', 'during'])
async def test_connection_generation_change_never_adopts_or_closes_replacement(when):
    conn, writer, client = connection()
    if when == 'before':
        await client.read_units()
    else:
        writer.plan = lambda t, s: [(.01, packet(row)) for row in frames(t, s)]
        task = asyncio.create_task(client.read_units())
        await asyncio.sleep(.001)
    replacement_reader = asyncio.StreamReader()
    replacement = Writer(replacement_reader, lambda *_: [])
    conn._reader, conn._writer = replacement_reader, replacement
    with pytest.raises(FramedObservationError, match='generation_changed'):
        if when == 'before':
            await client.read_units()
        else:
            await task
    assert not replacement.closed and not replacement.sent
    assert len(writer.sent) == 1 and writer.closed is (when == 'during')


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'wrong_index', 'disconnected'])
async def test_unsupported_connection_or_vm_refuses_before_send(change):
    conn, writer, client = connection()
    if change == 'missing':
        conn.lua_states = {9: 'InGame'}
    elif change == 'duplicate':
        conn.lua_states[3] = CONTEXT
    elif change == 'wrong_index':
        conn.gamecore_index = 3
    else:
        writer.close()
    with pytest.raises(FramedObservationError, match='not_admitted'):
        await client.read_units()
    assert not writer.sent and client.last_exchange['status'] == 'not_sent'
    conn.connect.assert_not_called()


async def test_no_arbitrary_code_state_or_mutation_surface():
    _, writer, client = connection()
    with pytest.raises(TypeError):
        await client.read_units(lua='UnitManager.RequestOperation()')
    with pytest.raises(TypeError):
        await client.read_units(state=9)
    assert not writer.sent


async def test_diagnostic_failure_does_not_lose_observation_or_expose_mutable_internal_state():
    def audit(trace):
        trace['status'] = 'tampered'
        raise OSError('private path')
    _, _, client = connection(audit=audit)
    assert len(await client.read_units()) == 1
    assert client.last_exchange['status'] == 'completed'
    assert client.last_exchange['diagnostic_callback_failed']


@pytest.fixture
def lua_runner(tmp_path):
    executable = shutil.which('texlua')
    if executable is None:
        pytest.skip('texlua unavailable for native-print lexical fixture')
    def run(source):
        path = tmp_path / 'framed-units.lua'
        path.write_text(source)
        result = subprocess.run([executable, str(path)], capture_output=True, text=True,
                                check=True, timeout=5)
        return result.stdout.splitlines()
    return run


LUA_UNITS = '''
function native_function() print('GLOBAL|private'); print('---END---') end
local unit={GetID=function() return 131073 end,GetX=function() return 1 end,
 GetY=function() return 0 end,GetType=function() return 1 end}
local player={GetID=function() return 63 end,IsBarbarian=function() return true end,
 GetUnits=function() return {Members=function() return ipairs({unit}) end} end}
PlayerManager={GetAlive=function() native_function(); return {player} end}
GameInfo={Units={[1]={UnitType='UNIT_WARRIOR'}}}
'''


async def test_real_translated_units_capture_ignores_global_print_without_replacing_it(lua_runner):
    def plan(token, state):
        lines = lua_runner(LUA_UNITS + _units_lua(token, state)
                           + "\nprint('GLOBAL_STILL_WORKS')")
        assert lines[:2] == ['GLOBAL|private', '---END---']
        assert lines[-1] == 'GLOBAL_STILL_WORKS'
        return [(0, packet('\n'.join(lines[:-1])))]
    _, _, client = connection(plan)
    units = await client.read_units()
    assert units[0]['owner'] == 63 and units[0]['is_barbarian'] is True
    assert client.last_exchange['unbound_messages'] == 2
    assert 'private' not in json.dumps(client.last_exchange)


@pytest.mark.parametrize('change,reason', [
    ('PlayerManager.GetAlive=nil', 'capability_unavailable'),
    ("PlayerManager.GetAlive=function() error('private') end", 'runtime_failure'),
    (f"GameInfo.Units[1].UnitType=string.rep('X',{MAX_PAYLOAD_BYTES + 1})", 'runtime_failure'),
])
def test_real_lua_missing_capability_runtime_error_or_capture_bound_fails(
        lua_runner, change, reason):
    rows = lua_runner(LUA_UNITS + change + '\n' + _units_lua(TOKEN, 2))
    assert rows[-1] == f'{PROTOCOL}|{TOKEN}|2|ERR|{reason}'
    assert not any('|DATA|' in row or '|END|' in row for row in rows)


@pytest.mark.parametrize('enabled', [False, True])
async def test_adapter_units_map_and_raw_routes_preserve_opt_in_boundary(enabled):
    conn, writer, _ = connection()
    async def read(lua, **kwargs):
        if 'print("UNITS|1")' in lua:
            return ['UNITS|1', ROW, '---END---']
        if 'print("VMAP|3")' in lua:
            return ['VMAP|3', 'TURN|1', 'TILEROW|0|0|TERRAIN_GRASS|true|-1|', '---END---']
        return ['LEGACY']
    conn.execute_read = AsyncMock(side_effect=read)
    conn.execute_write = AsyncMock(return_value=['CITIES|1', '---END---'])
    adapter = FireTunerAdapter(conn=conn, framed_units=enabled)
    units = await adapter.observe(ObserveRequest(kind=ObserveKind.UNITS, player_id=0))
    assert units[0]['unit_id'] == 'u0:131073'
    visible = await adapter.observe(ObserveRequest(kind=ObserveKind.VISIBLE_MAP, player_id=0))
    assert visible['tiles']['0,0']['terrain'] == 'GRASSLAND'
    assert len(writer.sent) == (2 if enabled else 0)
    raw = lua_translator.restore_unit('u0:131073')
    assert await adapter.read_raw(raw) == ['LEGACY']
    conn.execute_read.assert_called_with(raw)
    assert len(writer.sent) == (2 if enabled else 0)
    assert sum('print("UNITS|1")' in call.args[0]
               for call in conn.execute_read.call_args_list) == (0 if enabled else 2)


async def test_adapter_never_falls_back_after_ambiguous_framed_read():
    conn, writer, _ = connection(lambda *_: [(0, packet('ERR:failed', raw=True))])
    conn.execute_read = AsyncMock(return_value=['UNITS|1', ROW, '---END---'])
    adapter = FireTunerAdapter(conn=conn, framed_units=True)
    with pytest.raises(FramedObservationError):
        await adapter.observe(ObserveRequest(kind=ObserveKind.UNITS, player_id=0))
    conn.execute_read.assert_not_called()
    assert writer.closed


@pytest.mark.parametrize('flag', [1, None, 'true'])
def test_adapter_opt_in_requires_boolean(flag):
    with pytest.raises(ValueError, match='explicit boolean'):
        FireTunerAdapter(framed_units=flag)


@pytest.mark.parametrize('trailing', ['END', 'DATA', 'ERR'])
async def test_correlated_frames_after_terminal_in_same_message_are_rejected(trailing):
    def plan(token, state):
        output = frames(token, state)
        extra = (output[-1] if trailing == 'END' else output[0] if trailing == 'DATA'
                 else f'{PROTOCOL}|{token}|{state}|ERR|runtime_failure')
        return [(0, packet('\n'.join(output + [extra])))]
    _, writer, client = connection(plan)
    with pytest.raises(FramedObservationError, match='frame_after_terminal'):
        await client.read_units()
    assert writer.closed


async def test_missing_native_classification_is_not_accepted_as_legacy_unit_payload():
    legacy = ROW.rsplit('|', 1)[0]
    _, writer, client = connection(
        lambda t, s: [(0, packet(row)) for row in frames(t, s, ['UNITS|1', legacy, '---END---'])])
    with pytest.raises(FramedObservationError, match='invalid_units_payload'):
        await client.read_units()
    assert writer.closed


async def test_close_failure_preserves_original_error_and_poison():
    _, writer, client = connection(lambda *_: [(0, packet('ERR:private', raw=True))])
    def close():
        raise OSError('private close failure')
    writer.close = close
    with pytest.raises(FramedObservationError, match='uncorrelated_native_error'):
        await client.read_units()
    assert client.last_exchange['stream_close'] == 'failed'
    with pytest.raises(FramedObservationError, match='poisoned'):
        await client.read_units()
    assert len(writer.sent) == 1


async def test_adapter_does_not_read_from_previous_connection_after_replacement():
    original, original_writer, _ = connection()
    replacement, replacement_writer, _ = connection()
    adapter = FireTunerAdapter(conn=original, framed_units=True)
    adapter._conn = replacement
    with pytest.raises(FramedObservationError, match='adapter_connection_replaced'):
        await adapter.observe(ObserveRequest(kind=ObserveKind.UNITS, player_id=0))
    assert not original_writer.sent and not replacement_writer.sent


async def test_delayed_prior_response_during_next_read_is_quarantined():
    tokens = []
    def plan(token, state):
        tokens.append(token)
        stale = [] if len(tokens) == 1 else [(0, packet(row))
                                           for row in frames(tokens[0], state)]
        return stale + [(.002, packet(row)) for row in frames(token, state)]
    _, writer, client = connection(plan)
    assert len(await client.read_units()) == 1
    assert len(await client.read_units()) == 1
    assert len(writer.sent) == 2 and not writer.closed
    assert client.last_exchange['unbound_messages'] == 2
    assert tokens[0] != tokens[1]


async def test_continuing_stale_output_does_not_restart_total_deadline():
    def plan(*_):
        return [(index * .001, packet('---END---')) for index in range(100)]
    _, writer, client = connection(plan)
    with pytest.raises(TimeoutError):
        await client.read_units(timeout=.015)
    assert writer.closed and client.last_exchange['elapsed_s'] < .1
    assert 0 < client.last_exchange['unbound_messages'] < 100


@pytest.mark.parametrize('value', [True, 0, -1, float('nan'), float('inf'), '5'])
async def test_invalid_timeout_is_rejected_before_io(value):
    _, writer, client = connection()
    with pytest.raises(ValueError, match='positive and finite'):
        await client.read_units(timeout=value)
    assert not writer.sent


async def test_parser_failure_traceback_does_not_disclose_raw_wire_row():
    secret = 'PRIVATE_UNBOUND_NATIVE_TEXT'
    _, _, client = connection(lambda t, s: [(0, packet(row))
                            for row in frames(t, s, ['UNITS|1', secret, '---END---'])])
    with pytest.raises(FramedObservationError) as caught:
        await client.read_units()
    assert secret not in ''.join(traceback.format_exception(caught.value))
    assert secret not in json.dumps(client.last_exchange)
