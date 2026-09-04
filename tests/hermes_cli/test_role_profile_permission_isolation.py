"""Regression proof for profile-local role tool surfaces and session context.

The fixture uses two disposable profile homes and two fake MCP-like toolsets.
It exercises the production YAML loader, tool-definition resolver, Tool Search
bridge, and agent-loop name gate without starting a gateway or calling a
provider. The session tests cover the separate ContextVar inheritance window
that can otherwise leak a sibling session before the new turn binds itself.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


WIDE_TOOL = "role_permission_wide_probe"
NARROW_TOOL = "role_permission_narrow_probe"


def _tool_schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


@pytest.fixture
def role_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Two real profile config files, isolated under one disposable Hermes root."""
    root = tmp_path / "hermes"
    root.mkdir()
    (root / "config.yaml").write_text("toolsets: [hermes-cli]\n", encoding="utf-8")
    homes = {}
    for role in ("wide", "narrow"):
        home = root / "profiles" / role
        home.mkdir(parents=True)
        (home / "config.yaml").write_text(
            f"toolsets: [mcp-role-permission-{role}]\n"
            "disabled_toolsets: []\n",
            encoding="utf-8",
        )
        homes[role] = home

    calls: list[str] = []

    def fake_handler(label: str):
        def handler(args, **kwargs):
            calls.append(label)
            return json.dumps({"ok": True, "tool": label})

        return handler

    from tools.registry import registry

    registry.register(
        WIDE_TOOL,
        "mcp-role-permission-wide",
        _tool_schema(WIDE_TOOL),
        fake_handler("wide"),
    )
    registry.register(
        NARROW_TOOL,
        "mcp-role-permission-narrow",
        _tool_schema(NARROW_TOOL),
        fake_handler("narrow"),
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root, homes, calls


def _surface(home: Path):
    """Resolve one profile through the real config and tool-surface loaders."""
    from hermes_cli.config import load_config_readonly
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    import model_tools

    token = set_hermes_home_override(home)
    try:
        config = load_config_readonly()
        definitions = model_tools.get_tool_definitions(
            enabled_toolsets=config["toolsets"],
            disabled_toolsets=config["disabled_toolsets"],
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
        return config, {item["function"]["name"] for item in definitions}
    finally:
        reset_hermes_home_override(token)


def test_profile_switch_rebuilds_surface_and_blocks_prior_role(role_profiles):
    """A role switch cannot retain the previous role's tools or dispatch them."""
    _root, homes, calls = role_profiles
    wide_config, wide_names = _surface(homes["wide"])
    narrow_config, narrow_names = _surface(homes["narrow"])

    assert wide_config["toolsets"] == ["mcp-role-permission-wide"]
    assert narrow_config["toolsets"] == ["mcp-role-permission-narrow"]
    assert WIDE_TOOL in wide_names and NARROW_TOOL not in wide_names
    assert NARROW_TOOL in narrow_names and WIDE_TOOL not in narrow_names

    import model_tools

    assert WIDE_TOOL not in model_tools._last_resolved_tool_names
    assert NARROW_TOOL in model_tools._last_resolved_tool_names

    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(homes["narrow"])
    try:
        bridge_result = json.loads(
            model_tools.handle_function_call(
                "tool_call",
                {"name": WIDE_TOOL, "arguments": {}},
                enabled_toolsets=narrow_config["toolsets"],
                disabled_toolsets=narrow_config["disabled_toolsets"],
                skip_pre_tool_call_hook=True,
                skip_tool_request_middleware=True,
                skip_tool_execution_middleware=True,
            )
        )
    finally:
        reset_hermes_home_override(token)

    assert "not available in this session" in bridge_result["error"]
    assert calls == []

    class ValidationAgent:
        valid_tool_names = narrow_names
        _invalid_tool_retries = 0
        _invalid_json_retries = 0
        log_prefix = ""

        def _uniquify_tool_call_ids(self, tool_calls):
            return None

        def _repair_tool_call(self, name):
            return None

        def _buffer_vprint(self, *args, **kwargs):
            return None

        def _vprint(self, *args, **kwargs):
            return None

        def _build_assistant_message(self, message, finish_reason):
            return {"role": "assistant", "tool_calls": []}

    from agent.turn_tool_validation import validate_tool_calls

    verdict = validate_tool_calls(
        ValidationAgent(),
        SimpleNamespace(
            tool_calls=[
                SimpleNamespace(
                    id="prior-role-call",
                    function=SimpleNamespace(name=WIDE_TOOL, arguments="{}"),
                )
            ]
        ),
        "tool_calls",
        messages=[],
        conversation_history=[],
        api_call_count=0,
        effective_task_id="role-proof",
    )
    assert verdict.action == "continue"
    assert calls == []

    token = set_hermes_home_override(homes["narrow"])
    try:
        allowed = json.loads(
            model_tools.handle_function_call(
                NARROW_TOOL,
                {},
                task_id="role-proof",
                session_id="narrow",
                enabled_toolsets=narrow_config["toolsets"],
                disabled_toolsets=narrow_config["disabled_toolsets"],
                skip_pre_tool_call_hook=True,
                skip_tool_request_middleware=True,
                skip_tool_execution_middleware=True,
            )
        )
    finally:
        reset_hermes_home_override(token)
    assert allowed == {"ok": True, "tool": "narrow"}
    assert calls == ["narrow"]


@pytest.fixture(autouse=True)
def isolate_session_context():
    """Restore ContextVars and process fallbacks after each session test."""
    import gateway.session_context as session_context

    saved_context = {name: variable.get() for name, variable in session_context._VAR_MAP.items()}
    saved_async = session_context._SESSION_ASYNC_DELIVERY.get()
    saved_engaged = session_context._session_context_engaged
    saved_env = {name: os.environ.get(name) for name in session_context._VAR_MAP}
    for variable in session_context._VAR_MAP.values():
        variable.set(session_context._UNSET)
    session_context._SESSION_ASYNC_DELIVERY.set(session_context._UNSET)
    session_context._session_context_engaged = False
    try:
        yield
    finally:
        for name, variable in session_context._VAR_MAP.items():
            variable.set(saved_context[name])
        session_context._SESSION_ASYNC_DELIVERY.set(saved_async)
        session_context._session_context_engaged = saved_engaged
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _session_view() -> dict[str, str | None]:
    from tools.environments.local import _make_run_env

    env = _make_run_env({})
    return {
        name: env.get(name)
        for name in (
            "HERMES_SESSION_KEY",
            "HERMES_SESSION_CHAT_ID",
            "HERMES_SESSION_THREAD_ID",
        )
    }


async def _run_sync_in_task(function):
    function()


def test_handler_entry_reset_drops_inherited_session_context(monkeypatch):
    """A new asyncio task cannot spawn a child process with sibling A's identity."""
    import gateway.session_context as session_context
    from gateway.session_context import reset_session_vars, set_session_vars

    mine = {
        "session_key": "synthetic-mine-session",
        "platform": "discord",
        "chat_id": "synthetic-mine-chat",
        "thread_id": "synthetic-mine-thread",
        "user_id": "synthetic-mine-user",
    }
    foreign = {
        "session_key": "synthetic-foreign-session",
        "platform": "discord",
        "chat_id": "synthetic-foreign-chat",
        "thread_id": "synthetic-foreign-thread",
        "user_id": "synthetic-foreign-user",
    }
    session_context._session_context_engaged = True
    monkeypatch.setenv("HERMES_SESSION_KEY", "synthetic-process-global-foreign")
    set_session_vars(**mine)

    async def child_turn():
        captured = {}

        def body():
            reset_session_vars()
            captured["window"] = _session_view()
            set_session_vars(**foreign)
            captured["bound"] = _session_view()

        await asyncio.create_task(_run_sync_in_task(body))
        return captured

    captured = asyncio.run(child_turn())
    assert all(value is None for value in captured["window"].values())
    assert captured["bound"]["HERMES_SESSION_KEY"] == foreign["session_key"]
    assert captured["bound"]["HERMES_SESSION_CHAT_ID"] == foreign["chat_id"]


def test_handler_entry_reset_drops_inherited_async_delivery_capability():
    """A stateless sibling's async-delivery flag cannot follow the next task."""
    import gateway.session_context as session_context
    from gateway.session_context import async_delivery_supported, reset_session_vars, set_session_vars

    session_context._session_context_engaged = True
    set_session_vars(
        session_key="synthetic-stateless-sibling",
        platform="api_server",
        chat_id="synthetic-api-chat",
        async_delivery=False,
    )

    async def child_turn():
        captured = {}

        def body():
            reset_session_vars()
            captured["window"] = async_delivery_supported()

        await asyncio.create_task(_run_sync_in_task(body))
        return captured

    assert asyncio.run(child_turn())["window"] is True
