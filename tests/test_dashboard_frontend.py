"""Execute production JavaScript rendering; no provider, tuner, or desktop."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize('seats', [2, 3, 4])
def test_every_configured_seat_has_cards_graph_scouting_and_inspectable_calls(seats):
    node = shutil.which('node')
    if node is None:
        pytest.skip('node unavailable for executable frontend regression')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / 'tests/dashboard_dom_probe.cjs'),
                             str(root / 'src/civ_arena/dashboard_static/app.js'), str(seats)],
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
