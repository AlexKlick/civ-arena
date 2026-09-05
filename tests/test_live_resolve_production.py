"""Standalone recovery keeps full city IDs and never operates on another owner."""

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

SPEC = importlib.util.spec_from_file_location(
    'live_resolve_production',
    Path(__file__).resolve().parents[1] / 'scripts/live_resolve_production.py')
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)


async def test_resolver_passes_full_owned_city_id_to_guarded_builders(monkeypatch):
    class Connection:
        connect = AsyncMock()
        disconnect = AsyncMock()

        def __init__(self):
            self.reads = iter([
                ['PUPPET_ACTIVE|false', '---END---'],
                ['LOCAL|1', '---END---'],
                ['CITYROW|c0:131073|0|Foreign|1|2|1|-',
                 'CITYROW|c1:131073|1|Owned|3|4|1|-', '---END---'],
            ])
            self.writes = []

        async def execute_read(self, lua):
            return next(self.reads)

        async def execute_write(self, lua):
            self.writes.append(lua)
            assert 'if me ~= 1 then' in lua
            assert 'CityManager.GetCity(me, 131073)' in lua
            assert 'pCity:GetID() ~= 131073' in lua
            if len(self.writes) == 1:
                assert 'AVPROD|1' in lua
                return ['ITEMROW|building|MONUMENT|60|5', '---END---']
            assert len(self.writes) == 2
            return ['ACT|set_city_production|OK|building', '---END---']

    conn = Connection()
    monkeypatch.setattr(resolver, 'GameConnection', lambda *_: conn)
    monkeypatch.setattr('sys.argv', ['live_resolve_production.py'])
    assert await resolver.main() == 0
    assert len(conn.writes) == 2
    conn.disconnect.assert_awaited_once()
