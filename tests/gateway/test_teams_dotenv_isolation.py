"""Canaries for Teams SDK import-time dotenv isolation (#62935 / #62947)."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest


CANARY_KEY = "HERMES_TEAMS_DOTENV_CANARY"


def _plant_cwd_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plant a cwd ``.env`` that would leak if Teams SDK dotenv ran unguarded."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"{CANARY_KEY}=leaked-from-cwd\n", encoding="utf-8")
    monkeypatch.delenv(CANARY_KEY, raising=False)


def _install_fake_teams_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal microsoft_teams package tree for adapter imports."""
    microsoft_teams = types.ModuleType("microsoft_teams")
    microsoft_teams.__path__ = []  # type: ignore[attr-defined]

    apps = types.ModuleType("microsoft_teams.apps")
    apps.__path__ = []  # type: ignore[attr-defined]
    apps.App = type("App", (), {})
    apps.ActivityContext = type("ActivityContext", (), {})

    common = types.ModuleType("microsoft_teams.common")
    common.__path__ = []  # type: ignore[attr-defined]
    common_http = types.ModuleType("microsoft_teams.common.http")
    common_http.__path__ = []  # type: ignore[attr-defined]
    common_http_client = types.ModuleType("microsoft_teams.common.http.client")
    common_http_client.ClientOptions = type("ClientOptions", (), {})

    api = types.ModuleType("microsoft_teams.api")
    api.__path__ = []  # type: ignore[attr-defined]
    api.MessageActivity = type("MessageActivity", (), {})
    api.ConversationReference = type("ConversationReference", (), {})

    api_activities = types.ModuleType("microsoft_teams.api.activities")
    api_activities.__path__ = []  # type: ignore[attr-defined]
    api_typing = types.ModuleType("microsoft_teams.api.activities.typing")
    api_typing.TypingActivityInput = type("TypingActivityInput", (), {})
    api_invoke = types.ModuleType("microsoft_teams.api.activities.invoke")
    api_invoke.__path__ = []  # type: ignore[attr-defined]
    api_invoke_card = types.ModuleType(
        "microsoft_teams.api.activities.invoke.adaptive_card"
    )
    api_invoke_card.AdaptiveCardInvokeActivity = type(
        "AdaptiveCardInvokeActivity", (), {}
    )

    api_models = types.ModuleType("microsoft_teams.api.models")
    api_models.__path__ = []  # type: ignore[attr-defined]
    api_models_card = types.ModuleType("microsoft_teams.api.models.adaptive_card")
    api_models_card.AdaptiveCardActionCardResponse = type(
        "AdaptiveCardActionCardResponse", (), {}
    )
    api_models_card.AdaptiveCardActionMessageResponse = type(
        "AdaptiveCardActionMessageResponse", (), {}
    )
    api_models_invoke = types.ModuleType(
        "microsoft_teams.api.models.invoke_response"
    )
    api_models_invoke.InvokeResponse = type("InvokeResponse", (), {})
    api_models_invoke.AdaptiveCardInvokeResponse = type(
        "AdaptiveCardInvokeResponse", (), {}
    )

    apps_http = types.ModuleType("microsoft_teams.apps.http")
    apps_http.__path__ = []  # type: ignore[attr-defined]
    apps_http_adapter = types.ModuleType("microsoft_teams.apps.http.adapter")
    apps_http_adapter.HttpMethod = str
    apps_http_adapter.HttpRequest = type("HttpRequest", (), {})
    apps_http_adapter.HttpResponse = type("HttpResponse", (), {})
    apps_http_adapter.HttpRouteHandler = type("HttpRouteHandler", (), {})

    cards = types.ModuleType("microsoft_teams.cards")
    cards.AdaptiveCard = type("AdaptiveCard", (), {})
    cards.ExecuteAction = type("ExecuteAction", (), {})
    cards.TextBlock = type("TextBlock", (), {})

    for name, mod in {
        "microsoft_teams": microsoft_teams,
        "microsoft_teams.apps": apps,
        "microsoft_teams.common": common,
        "microsoft_teams.common.http": common_http,
        "microsoft_teams.common.http.client": common_http_client,
        "microsoft_teams.api": api,
        "microsoft_teams.api.activities": api_activities,
        "microsoft_teams.api.activities.typing": api_typing,
        "microsoft_teams.api.activities.invoke": api_invoke,
        "microsoft_teams.api.activities.invoke.adaptive_card": api_invoke_card,
        "microsoft_teams.api.models": api_models,
        "microsoft_teams.api.models.adaptive_card": api_models_card,
        "microsoft_teams.api.models.invoke_response": api_models_invoke,
        "microsoft_teams.apps.http": apps_http,
        "microsoft_teams.apps.http.adapter": apps_http_adapter,
        "microsoft_teams.cards": cards,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)


def _purge_teams_adapter_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a fresh Teams adapter/summary_writer import for this test.

    Uses ``monkeypatch.delitem`` (not a raw ``del``) so pytest's teardown
    restores every purged ``sys.modules`` entry once this test ends,
    instead of permanently leaving the freshly-imported, fake-SDK-backed
    module cached for the rest of the pytest process. A bare ``del`` here
    previously caused
    test_build_pipeline_runtime_skips_sender_when_adapter_layer_is_unavailable
    (tests/gateway/test_teams_pipeline_runtime_wiring.py) to observe a
    real, already-imported TeamsSummaryWriter (baked in by whichever of
    these tests ran first) instead of hitting the ImportError path it
    exercises, when both files run in the same pytest session.

    Callers MUST invoke this a second time immediately after performing
    the fresh import (see call sites below). ``monkeypatch.delitem`` only
    schedules a restore for keys that already exist in ``sys.modules`` at
    the moment it is called -- a module that gets newly created *during*
    the test (i.e. the fresh ``plugins.platforms.teams.adapter`` /
    ``...summary_writer`` objects this helper's callers import right
    after purging) is invisible to the first call and was never
    monkeypatched at all, so it silently survived teardown and leaked
    into later tests/files even after switching to ``delitem``. Calling
    this again post-import schedules THOSE new entries too; pytest undoes
    monkeypatch calls in LIFO order, so the two delitem calls on the same
    key correctly collapse back to "remove the key" (or restore whatever
    legitimately pre-existed) once the test ends. Already-bound local
    variables (e.g. ``teams_adapter``) keep working after this second
    purge because removing a name from ``sys.modules`` does not affect an
    object a test already holds a direct reference to.
    """
    for name in list(sys.modules):
        if name == "plugins.platforms.teams" or name.startswith(
            "plugins.platforms.teams."
        ):
            monkeypatch.delitem(sys.modules, name, raising=False)


class TestTeamsAdapterImportDoesNotLeakDotenv:
    def test_adapter_import_without_sdk_does_not_raise(self, monkeypatch):
        """Plugin import must not crash when microsoft_teams is absent.

        ``find_spec("microsoft_teams.apps")`` raises ModuleNotFoundError on
        3.11+ if the parent namespace is missing — that unregistered Teams.
        """
        for name in list(sys.modules):
            if name == "microsoft_teams" or name.startswith("microsoft_teams."):
                monkeypatch.delitem(sys.modules, name, raising=False)
        _purge_teams_adapter_modules(monkeypatch)

        import plugins.platforms.teams.adapter as teams_adapter

        _purge_teams_adapter_modules(monkeypatch)

        # Import must succeed even when the parent namespace is absent
        # (CI: no microsoft-teams-apps). Symbols stay unbound until connect.
        assert teams_adapter.App is None

    def test_namespace_without_apps_is_not_sdk_available(self, monkeypatch):
        """A sibling microsoft_teams package must not count as the Teams SDK."""
        # tests/gateway/test_teams.py installs a full fake microsoft_teams.*
        # module tree (including microsoft_teams.apps) directly into the
        # real sys.modules via a bare `sys.modules.setdefault(...)` at
        # *module import/collection time* (not inside a test function, so
        # monkeypatch never sees it and can never undo it). Once pytest has
        # collected that file in this process, "microsoft_teams.apps" stays
        # cached in sys.modules for the rest of the run.
        #
        # importlib.util.find_spec("microsoft_teams.apps") short-circuits to
        # that cached module's __spec__ whenever the dotted name is already
        # a sys.modules key -- it never re-consults the (here, deliberately
        # empty) __path__ of the fresh `ns` package below. Without purging
        # that stray submodule first, _probe_teams_sdk_available() would see
        # a leaked "apps" submodule that has nothing to do with this test's
        # namespace-only package and incorrectly report the SDK as
        # available. Purge any stray microsoft_teams.* submodules (but not
        # the bare "microsoft_teams" key itself, which is set immediately
        # below) so this test's outcome does not depend on whether
        # tests/gateway/test_teams.py has already been collected.
        for name in list(sys.modules):
            if name.startswith("microsoft_teams."):
                monkeypatch.delitem(sys.modules, name, raising=False)

        ns = types.ModuleType("microsoft_teams")
        ns.__path__ = []  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "microsoft_teams", ns)
        _purge_teams_adapter_modules(monkeypatch)

        import plugins.platforms.teams.adapter as teams_adapter

        _purge_teams_adapter_modules(monkeypatch)

        assert teams_adapter.App is None
        assert teams_adapter.TEAMS_SDK_AVAILABLE is False

    def test_adapter_import_does_not_load_cwd_dotenv(self, tmp_path, monkeypatch):
        _plant_cwd_dotenv(tmp_path, monkeypatch)
        _install_fake_teams_sdk(monkeypatch)
        _purge_teams_adapter_modules(monkeypatch)

        import plugins.platforms.teams.adapter as teams_adapter

        _purge_teams_adapter_modules(monkeypatch)

        assert CANARY_KEY not in os.environ
        assert teams_adapter.App is None  # SDK symbols deferred

    def test_teams_summary_writer_import_does_not_load_cwd_dotenv(
        self, tmp_path, monkeypatch
    ):
        """teams_pipeline/runtime.py imports TeamsSummaryWriter directly."""
        _plant_cwd_dotenv(tmp_path, monkeypatch)
        _install_fake_teams_sdk(monkeypatch)
        _purge_teams_adapter_modules(monkeypatch)

        from plugins.platforms.teams.adapter import TeamsSummaryWriter

        _purge_teams_adapter_modules(monkeypatch)

        assert CANARY_KEY not in os.environ
        assert TeamsSummaryWriter is not None

    def test_sdk_import_path_suppresses_dotenv(self, tmp_path, monkeypatch):
        """Guarded SDK bind must no-op dotenv.load_dotenv (SDK import side effect)."""
        _plant_cwd_dotenv(tmp_path, monkeypatch)
        _install_fake_teams_sdk(monkeypatch)
        _purge_teams_adapter_modules(monkeypatch)

        import dotenv

        import plugins.platforms.teams.adapter as teams_adapter

        _purge_teams_adapter_modules(monkeypatch)

        def _marking_load_dotenv(*args, **kwargs):
            os.environ[CANARY_KEY] = "leaked-from-sdk-import"
            return True

        monkeypatch.setattr(dotenv, "load_dotenv", _marking_load_dotenv)

        # Unguarded call would leak — establish the canary contract.
        dotenv.load_dotenv()
        assert os.environ.get(CANARY_KEY) == "leaked-from-sdk-import"
        monkeypatch.delenv(CANARY_KEY, raising=False)

        with teams_adapter._suppress_third_party_dotenv():
            dotenv.load_dotenv(dotenv.find_dotenv(usecwd=True))
            # Also exercise the real bind importer under the same suppress.
            from microsoft_teams.apps import App  # noqa: F401

        assert CANARY_KEY not in os.environ

        # Active requirements path must also stay clean.
        monkeypatch.setattr(teams_adapter, "App", None)
        monkeypatch.setattr(teams_adapter, "AIOHTTP_AVAILABLE", True)

        def _fake_ensure_and_bind(feature, importer, target_globals, **kwargs):
            assert feature == "platform.teams"
            # Call dotenv the way microsoft_teams.apps.app does, but from inside
            # the importer which wraps SDK imports in _suppress_third_party_dotenv.
            # We inject the dotenv call by wrapping the importer.
            original_importer = importer

            def _importer_with_sdk_dotenv():
                with teams_adapter._suppress_third_party_dotenv():
                    dotenv.load_dotenv(dotenv.find_dotenv(usecwd=True))
                return original_importer()

            bound = _importer_with_sdk_dotenv()
            target_globals.update(bound)
            return True

        monkeypatch.setattr(
            "tools.lazy_deps.ensure_and_bind", _fake_ensure_and_bind
        )
        assert teams_adapter.check_teams_requirements() is True
        assert CANARY_KEY not in os.environ
        assert teams_adapter.App is not None


class TestLoadGatewayConfigApiServerExplicitDisable:
    def test_load_gateway_config_honors_explicit_api_server_disable(
        self, tmp_path, monkeypatch
    ):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "platforms:\n"
            "  api_server:\n"
            "    enabled: false\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("API_SERVER_ENABLED", "true")
        # Must satisfy _has_usable_api_server_key (min_length=16) — a weaker
        # key never enters the env-override branch on current main, which
        # would vacuously pass the enabled=False assertion below.
        monkeypatch.setenv("API_SERVER_KEY", "test-key-0123456789abcdef")
        monkeypatch.chdir(tmp_path)

        from gateway.config import Platform, load_gateway_config

        config = load_gateway_config()
        api_cfg = config.platforms.get(Platform.API_SERVER)
        assert api_cfg is not None
        assert api_cfg.enabled is False
        assert api_cfg.extra.get("key") == "test-key-0123456789abcdef"
