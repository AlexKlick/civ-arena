"""Standalone recovery keeps full city IDs and never operates on another owner."""

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

SPEC = importlib.util.spec_from_file_location(
    'live_resolve_production',
    Path(__file__).resolve().parents[1] / 'scripts/live_resolve_production.py')
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)


@pytest.mark.parametrize("readback_available", [True, False])
async def test_resolver_passes_full_owned_city_id_to_guarded_builders(
        monkeypatch, readback_available):
    class Connection:
        connect = AsyncMock()
        disconnect = AsyncMock()

        def __init__(self):
            self.reads = iter([
                ['PUPPET_ACTIVE|false', '---END---'],
                ['LOCAL|1', '---END---'],
            ])
            self.writes = []

        async def execute_read(self, lua):
            return next(self.reads)

        async def execute_write(self, lua):
            self.writes.append(lua)
            if len(self.writes) == 1:
                assert 'CITYROW|' in lua
                return ['CITYROW|c0:131073|0|Foreign|1|2|1|-',
                        'CITYROW|c1:131073|1|Owned|3|4|1|-', '---END---']
            assert 'if me ~= 1 then' in lua
            assert 'CityManager.GetCity(me, 131073)' in lua
            assert 'pCity:GetID() ~= 131073' in lua
            if len(self.writes) == 2:
                assert 'AVPROD|1' in lua
                return ['ITEMROW|building|MONUMENT|60|5', '---END---']
            if len(self.writes) == 3:
                return ['ACT|set_city_production|OK|submitted',
                        'PRODUCTION_REQUEST|-99', '---END---']
            assert 'CURPROD|' in lua
            if not readback_available:
                return ['CURPROD|-1', '---END---']
            return ['CURPROD|' + ('0' if len(self.writes) == 4 else '-99'), '---END---']

    conn = Connection()
    monkeypatch.setattr(resolver, 'GameConnection', lambda *_: conn)
    monkeypatch.setattr('sys.argv', ['live_resolve_production.py'])
    if readback_available:
        assert await resolver.main() == 0
        assert len(conn.writes) == 5
    else:
        with pytest.raises(RuntimeError, match="unavailable"):
            await resolver.main()
        assert len(conn.writes) == 4
    conn.disconnect.assert_awaited_once()
