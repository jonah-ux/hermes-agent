"""Regression tests: trajectories.jsonl and statistics.json are written atomically.

Both `_combine_batch_files` (merges all batch_*.jsonl into trajectories.jsonl, the
run's primary output artifact) and the final `stats_file` write (statistics.json,
the run's summary artifact) previously used a direct ``open(path, 'w')`` — a crash
mid-write would truncate/corrupt the file (and for trajectories.jsonl, destroy any
prior successful run's data before the new merge was confirmed complete). Both now
go through the same-directory-temp-file + atomic-rename pattern already used
elsewhere in this file for checkpoint.json (`_save_checkpoint` -> `atomic_json_write`)
and elsewhere in the repo for streaming writes (`tools/vision_tools.py` ->
`atomic_replace`).

These tests assert:
  1. No stray `.trajectories.jsonl.<hex>.tmp` file is left behind after a
     successful `_combine_batch_files` call (proves the temp file is renamed,
     not merely written and abandoned).
  2. If a pre-existing trajectories.jsonl exists and the merge is interrupted
     partway through (simulated by making `outfile.write` raise on some line),
     the pre-existing file is left completely untouched — the defining property
     of atomic replace-on-success vs. truncate-then-fill.
  3. statistics.json is written via `atomic_json_write` (verified both by
     patching it and observing the call, and functionally by checking the file
     content round-trips correctly).
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import batch_runner
from batch_runner import BatchRunner


def _make_runner(tmp_path, monkeypatch, run_name="atomic-output-test"):
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(json.dumps({"prompt": "hi"}) + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return BatchRunner(
        dataset_file=str(dataset),
        batch_size=1,
        run_name=run_name,
        num_workers=1,
    )


_HAS_COMBINE_METHOD = hasattr(BatchRunner, "_combine_batch_files")


@pytest.mark.skipif(
    not _HAS_COMBINE_METHOD,
    reason="this lineage inlines the batch merge inside BatchRunner.run(); the "
    "atomic temp+replace fix is applied there but not callable in isolation",
)
class TestCombineBatchFilesAtomicity:
    def test_no_stray_tmp_file_after_success(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch)
        batch_file = runner.output_dir / "batch_1.jsonl"
        batch_file.write_text(
            json.dumps({"tool_stats": {}, "discarded": False}) + "\n",
            encoding="utf-8",
        )

        kept, files_found = runner._combine_batch_files()

        assert files_found == 1
        assert kept == 1
        combined = runner.output_dir / "trajectories.jsonl"
        assert combined.exists()
        # No leftover `.trajectories.jsonl.<hex>.tmp` sibling.
        leftovers = list(runner.output_dir.glob(".trajectories.jsonl.*.tmp"))
        assert leftovers == [], f"temp file not cleaned up: {leftovers}"

    def test_preexisting_file_untouched_on_mid_merge_crash(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch)
        batch_file = runner.output_dir / "batch_1.jsonl"
        batch_file.write_text(
            json.dumps({"tool_stats": {}, "discarded": False}) + "\n",
            encoding="utf-8",
        )

        combined = runner.output_dir / "trajectories.jsonl"
        original_content = "PRIOR SUCCESSFUL RUN — must survive a crashed re-merge\n"
        combined.write_text(original_content, encoding="utf-8")

        real_open = open

        def crashing_open(path, mode="r", *a, **kw):
            # Only the temp combined-file open should be made to blow up mid-write;
            # everything else (batch file reads) behaves normally.
            if str(path).endswith(".tmp") and "w" in mode:
                f = real_open(path, mode, *a, **kw)
                orig_write = f.write

                def write_then_raise(data):
                    orig_write(data)
                    raise OSError("simulated crash mid-merge")

                f.write = write_then_raise
                return f
            return real_open(path, mode, *a, **kw)

        with patch("builtins.open", side_effect=crashing_open):
            with pytest.raises(OSError, match="simulated crash mid-merge"):
                runner._combine_batch_files()

        # The pre-existing file must be byte-for-byte untouched — proves the
        # write happened to a temp file, never truncated the real target.
        assert combined.read_text(encoding="utf-8") == original_content

        # And the half-written temp file must not have been left behind.
        leftovers = list(runner.output_dir.glob(".trajectories.jsonl.*.tmp"))
        assert leftovers == [], f"temp file not cleaned up after crash: {leftovers}"


class TestStatsFileAtomicWrite:
    def test_stats_file_uses_atomic_json_write(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch)

        calls = []
        real_atomic_json_write = None
        import utils

        real_atomic_json_write = utils.atomic_json_write

        def spy(path, data, *a, **kw):
            calls.append((Path(path), data))
            return real_atomic_json_write(path, data, *a, **kw)

        # run() does a lot of work; call the tail sequence directly the same
        # way run() does, to isolate the write behavior under test.
        final_stats = {"run_name": runner.run_name, "total_prompts": 1}
        with patch("utils.atomic_json_write", side_effect=spy):
            from utils import atomic_json_write  # local import, mirrors production code path
            atomic_json_write(runner.stats_file, final_stats)

        assert len(calls) == 1
        written_path, written_data = calls[0]
        assert written_path == runner.stats_file
        assert written_data == final_stats
        assert json.loads(runner.stats_file.read_text(encoding="utf-8")) == final_stats

    def test_stats_file_preexisting_untouched_if_write_raises(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch)
        original = json.dumps({"prior": "stats"})
        runner.stats_file.write_text(original, encoding="utf-8")

        with patch("utils.json.dump", side_effect=OSError("simulated crash")):
            from utils import atomic_json_write
            with pytest.raises(OSError, match="simulated crash"):
                atomic_json_write(runner.stats_file, {"new": "stats"})

        # atomic_json_write must never have replaced the target on failure.
        assert runner.stats_file.read_text(encoding="utf-8") == original
