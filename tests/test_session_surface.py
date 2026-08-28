"""Session surface: the tool API structurally excludes player identity."""

from __future__ import annotations

import inspect

from civ_arena.session.player_session import PlayerSession
from civ_arena.session.tools import TOOL_REGISTRY, ToolFacade

FORBIDDEN_PARAM_NAMES = {"player_id", "owner_id", "player", "owner", "civilization"}


def test_no_tool_parameter_is_player_id():
    assert len(TOOL_REGISTRY) == 14
    for name, fn in TOOL_REGISTRY.items():
        sig = inspect.signature(fn)
        for param_name in sig.parameters:
            assert param_name not in FORBIDDEN_PARAM_NAMES, (
                f"tool {name} exposes identity parameter {param_name!r}"
            )
        assert list(sig.parameters)[0] == "ctx", (
            f"tool {name} must take the opaque ctx first"
        )


def test_no_tool_name_leaks_identity_or_scope():
    for name in TOOL_REGISTRY:
        low = name.lower()
        assert not any(bad in low for bad in ("player", "owner", "referee", "scope")), (
            f"tool name {name!r} leaks identity/scope vocabulary"
        )


def test_facade_exposes_only_tools():
    class _Ref:
        pass

    facade = ToolFacade.__new__(ToolFacade)
    facade.__dict__["_ctx"] = None
    facade.__dict__["_bound"] = {n: n for n in TOOL_REGISTRY}
    assert set(facade.names()) == set(TOOL_REGISTRY)
    try:
        facade.player_id  # noqa: B018
        raise AssertionError("facade must not expose player_id")
    except AttributeError:
        pass
    try:
        facade.referee  # noqa: B018
        raise AssertionError("facade must not expose the referee")
    except AttributeError:
        pass


def test_session_hides_adapter_and_player_id():
    session = PlayerSession(referee=object(), player_id=3, agent_id="roman")
    assert session.agent_id == "roman"
    assert not hasattr(session, "adapter")
    assert not hasattr(session, "player_id")
    assert not hasattr(session, "policy")
    public = [k for k in vars(session) if not k.startswith("_")]
    assert public == ["agent_id"], f"unexpected public attrs: {public}"
