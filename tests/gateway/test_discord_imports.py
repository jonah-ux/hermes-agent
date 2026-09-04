"""Import-safety tests for the Discord gateway adapter."""

import builtins
import importlib.util


class TestDiscordImportSafety:
    def test_module_imports_even_when_discord_dependency_is_missing(self, monkeypatch):
        original_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "discord" or name.startswith("discord."):
                raise ImportError("discord unavailable for test")
            return original_import(name, globals, locals, fromlist, level)

        # Resolve the adapter's source file up front (before faking the
        # ImportError) and execute a PRIVATE, throwaway copy of it under an
        # isolated module name instead of reusing
        # importlib.import_module("plugins.platforms.discord.adapter").
        #
        # Purging + re-importing the real dotted name corrupts shared
        # global state: Python's import machinery unconditionally re-stamps
        # a `.adapter` attribute onto whatever "plugins.platforms.discord"
        # package object is live at that moment (regardless of whether that
        # parent object itself needed reimporting), and
        # monkeypatch.delitem's teardown only restores the sys.modules DICT
        # entries — it has no way to know about, or undo, that separate
        # attribute stamped on the parent module object. The result is a
        # module with `discord = None` wired permanently into the parent's
        # attribute chain, which every later
        # `import plugins.platforms.discord.adapter as X` in other test
        # files (e.g. tests/gateway/test_discord_send.py) resolves through
        # attribute access rather than a sys.modules lookup — so they see
        # the simulated-missing state for the rest of the pytest process
        # even though sys.modules itself looks correctly restored.
        #
        # Loading a private copy of the same source file under its own
        # module name touches none of that shared cache/attribute tree.
        real_spec = importlib.util.find_spec("plugins.platforms.discord.adapter")
        assert real_spec is not None and real_spec.origin

        monkeypatch.setattr(builtins, "__import__", fake_import)

        probe_spec = importlib.util.spec_from_file_location(
            "plugins.platforms.discord._import_safety_probe", real_spec.origin
        )
        module = importlib.util.module_from_spec(probe_spec)
        probe_spec.loader.exec_module(module)

        assert module.DISCORD_AVAILABLE is False
        assert module.discord is None
