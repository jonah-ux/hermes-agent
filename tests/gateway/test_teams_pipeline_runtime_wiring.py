"""Tests for Teams pipeline runtime wiring into the gateway."""

from __future__ import annotations

import sys
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import MagicMock

from gateway.config import Platform, PlatformConfig
from gateway.run import GatewayRunner
from plugins.teams_pipeline.runtime import (
    bind_gateway_runtime,
    build_pipeline_runtime,
    build_pipeline_runtime_config,
)


def test_gateway_runner_skips_wiring_without_msgraph_adapter(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: MagicMock()}
    runner._teams_pipeline_runtime_error = None

    called = False

    def _bind(_gateway_runner):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr("plugins.teams_pipeline.runtime.bind_gateway_runtime", _bind)
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"plugins": {"enabled": ["teams_pipeline"]}},
    )

    GatewayRunner._wire_teams_pipeline_runtime(runner)

    assert called is False


def test_build_pipeline_runtime_skips_sender_when_adapter_layer_is_unavailable(monkeypatch):
    gateway = SimpleNamespace(
        config=SimpleNamespace(
            platforms={
                Platform("teams"): PlatformConfig(
                    enabled=True,
                    extra={
                        "delivery_mode": "graph",
                        "team_id": "team-1",
                        "channel_id": "channel-1",
                    },
                ),
            }
        )
    )

    monkeypatch.setattr(
        "plugins.teams_pipeline.runtime.build_graph_client",
        lambda: object(),
    )
    monkeypatch.setattr(
        "plugins.teams_pipeline.runtime.resolve_teams_pipeline_store_path",
        lambda: "/tmp/teams-pipeline-store.json",
    )
    monkeypatch.setattr(
        "plugins.teams_pipeline.runtime.TeamsPipelineStore",
        lambda path: {"path": path},
    )
    # Force a fresh import of the plugins.platforms.teams package tree
    # before stubbing out .adapter below. plugins/platforms/teams/__init__.py
    # does `from .adapter import register`, so build_pipeline_runtime's
    # `from plugins.platforms.teams.summary_writer import TeamsSummaryWriter`
    # only fails with ImportError (the behavior this test exercises) when
    # importing the *package* re-executes __init__.py against the stubbed,
    # empty adapter module below. If any earlier test in this pytest process
    # already imported the real plugins.platforms.teams tree (e.g.
    # test_load_gateway_config_honors_explicit_api_server_disable in
    # test_teams_dotenv_isolation.py, which loads the real gateway config/
    # platform registry with no adapter stub), summary_writer is already
    # cached in sys.modules and this import silently short-circuits to the
    # real, fully-functional TeamsSummaryWriter instead of raising --
    # regardless of what .adapter is stubbed to here. Purge the whole
    # plugins.platforms.teams.* subtree first (via monkeypatch.delitem, so
    # pytest restores every entry at teardown) to make this test's outcome
    # independent of prior import-cache state / test execution order.
    for name in list(sys.modules):
        if name == "plugins.platforms.teams" or name.startswith("plugins.platforms.teams."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    monkeypatch.setitem(
        sys.modules,
        "plugins.platforms.teams.adapter",
        ModuleType("plugins.platforms.teams.adapter"),
    )

    runtime = build_pipeline_runtime(gateway)

    assert runtime.teams_sender is None


