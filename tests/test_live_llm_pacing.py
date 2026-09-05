"""The live driver enables paced clients without changing provider limits."""
import json
from dataclasses import replace

import pytest

from civ_arena.agents.llm.runtime import LLMAgentRuntime
from civ_arena.config import load_config
from civ_arena.game.civ6 import live_driver as ld
from civ_arena.game.civ6.fake_tuner_server import FakeMod, FakeTunerServer
from civ_arena.game.civ6.firetuner import FireTunerAdapter
from fakes import FakeModel, use


def test_sixty_round_configuration_keeps_provider_and_safety_limits():
    root = ld.MOD_DEFAULT.parents[2] / "configs"
    baseline = load_config(root / "live-hotseat-llm-minimax2-001.yaml")
    sixty = load_config(root / "live-hotseat-llm-minimax2-060.yaml")
    assert sixty.max_turns == 60
    assert replace(sixty, max_turns=baseline.max_turns,
                   match_id=baseline.match_id) == baseline


@pytest.mark.parametrize("paced", [True, False])
async def test_driver_records_effective_pacing_before_first_request(tmp_path, monkeypatch, paced):
    spec = load_config(ld.MOD_DEFAULT.parents[2] / "configs/live-hotseat-llm-minimax2-060.yaml")
    models = []

    def build(profile, **context):
        assert context["opening_units_frozen"] is False
        model = FakeModel([[use("end_turn")]])
        models.append(model)
        return LLMAgentRuntime.build(profile, client=model)

    monkeypatch.setattr(ld, "build_runtime", build)
    server = FakeTunerServer(mod=FakeMod(hotseat=[0, 1]))
    port = await server.start()
    adapter = FireTunerAdapter("127.0.0.1", port, simulate_hook=ld._fake_hook)
    try:
        result = await ld.phase_dispatch_hotseat(
            spec, adapter, tmp_path, 1, "h1", ld.MOD_DEFAULT.read_text(),
            pace_llm_turns=paced)
    finally:
        await server.stop()
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert result == 0, summary
    assert summary["completed_rounds"] == 1
    assert all(row["enabled"] is paced for row in summary["turn_pacing"].values())
    assert [(row["turn"], row["player"]) for row in summary["per_turn"]] == [(1, 0), (1, 1)]
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    audits = [event for event in events if event.get("audit") == "turn_pacing"]
    assert len(audits) == (2 if paced else 0)
    assert all(event["recall_available"] is False for event in audits)
    if audits:
        assert max(event["seq"] for event in audits) < min(
            event["seq"] for event in events if event.get("audit") == "provider_request")
    for model in models:
        assert model.closed
        tools = {tool["name"] for tool in model.requests[0]["tools"]}
        assert ("recall_lessons" not in tools) == paced
        opening = str(model.requests[0]["messages"])
        assert ("Controller context" in opening) == paced
