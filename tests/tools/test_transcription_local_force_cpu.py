"""Regression tests for ``tools.transcription_tools._load_local_whisper_model``'s
Apple-Silicon/Rosetta CPU-forcing branch.

``_should_force_faster_whisper_cpu()`` exists to dodge a native abort: ctranslate2's
``device="auto"`` can pick CUDA (and crash) on hosts without an NVIDIA runtime,
including Apple Silicon/Rosetta. The fix for that must only override the *device*
pick when the caller left it at the "auto" default — an explicitly pinned
``stt.local.device`` / ``stt.local.compute_type`` (#9088) must still win.

Bug fixed here: the force-cpu branch used to hardcode BOTH ``device="cpu"`` and
``compute_type="int8"`` unconditionally whenever running on Apple Silicon/Rosetta,
silently discarding a user's pinned ``compute_type`` (or ``device``) even though
compute_type has nothing to do with the native-abort risk the branch guards against.
Caught via ``tests/tools/test_transcription_tools.py::TestTranscribeLocalExtended::
test_config_device_and_compute_type_passed_to_whisper`` failing on an actual Apple
Silicon host (this machine), which forces this branch for real (not mocked).
"""

import sys
import types
from unittest.mock import MagicMock, patch

if "faster_whisper" not in sys.modules:
    # tools.transcription_tools does a late ``from faster_whisper import WhisperModel``
    # inside _load_local_whisper_model; the real package is an optional STT extra that
    # isn't installed in every dev venv, so stub it the same way
    # test_transcription_tools.py does — a real module object in sys.modules with a
    # __spec__ so both ``import faster_whisper`` and ``patch("faster_whisper.WhisperModel", ...)``
    # work without the real dependency present.
    from importlib.machinery import ModuleSpec
    faster_whisper_stub = types.ModuleType("faster_whisper")
    faster_whisper_stub.WhisperModel = MagicMock(name="WhisperModel")
    faster_whisper_stub.__spec__ = ModuleSpec("faster_whisper", loader=None)
    sys.modules["faster_whisper"] = faster_whisper_stub

import tools.transcription_tools as tl


class TestForceCpuRespectsExplicitPins:
    def test_explicit_compute_type_survives_force_cpu(self):
        mock_cls = MagicMock()
        with patch.object(tl, "_should_force_faster_whisper_cpu", return_value=True), \
             patch("faster_whisper.WhisperModel", mock_cls):
            tl._load_local_whisper_model("base", device="cpu", compute_type="float32")
        mock_cls.assert_called_once_with("base", device="cpu", compute_type="float32")

    def test_unpinned_auto_defaults_still_forced_to_cpu_int8(self):
        """No explicit pin (the "auto" defaults) — the crash workaround still applies."""
        mock_cls = MagicMock()
        with patch.object(tl, "_should_force_faster_whisper_cpu", return_value=True), \
             patch("faster_whisper.WhisperModel", mock_cls):
            tl._load_local_whisper_model("base")
        mock_cls.assert_called_once_with("base", device="cpu", compute_type="int8")

    def test_explicit_device_survives_force_cpu_with_default_compute_type(self):
        mock_cls = MagicMock()
        with patch.object(tl, "_should_force_faster_whisper_cpu", return_value=True), \
             patch("faster_whisper.WhisperModel", mock_cls):
            tl._load_local_whisper_model("base", device="cpu")
        mock_cls.assert_called_once_with("base", device="cpu", compute_type="int8")
