"""Regression test: PLUGIN-COMPAT blocks in tools/ must not contain duplicate imports.

The "PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md)" blocks re-export names
that external plugins imported from a module before the September 2026 decomposition
(see COMPAT_MANIFEST.md, scripts/check_compat_pointers.py). These blocks are hand/
generator-maintained lists of ``import x`` / ``from x import y`` lines.

A literal duplicate import line inside such a block is harmless at runtime but is a
copy-paste/generator artifact that ruff flags as F811 ("redefinition of unused name").
Two real instances were found and fixed:
  - tools/managed_tool_gateway.py: ``from urllib.parse import urlsplit`` duplicated.
  - tools/skill_usage.py: ``import tempfile`` and ``import os`` each duplicated.

This test scans every PLUGIN-COMPAT block under tools/ and asserts no top-level import
statement (by its exact source text) appears twice within the same block, so a future
regeneration of these blocks can't silently reintroduce the same class of bug.
"""

from __future__ import annotations

import pathlib

TOOLS_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "tools"

_BEGIN_MARKER = "# ---- BEGIN PLUGIN-COMPAT"
_END_MARKER = "# ---- END PLUGIN-COMPAT"


def _iter_compat_blocks():
    """Yield (path, block_start_line, lines) for every PLUGIN-COMPAT block found."""
    for path in sorted(TOOLS_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        in_block = False
        start_line = None
        block_lines: list[str] = []
        for lineno, line in enumerate(lines, start=1):
            if _BEGIN_MARKER in line:
                in_block = True
                start_line = lineno
                block_lines = []
                continue
            if _END_MARKER in line:
                if in_block:
                    yield path, start_line, block_lines
                in_block = False
                continue
            if in_block:
                block_lines.append(line)
        # Blocks without an explicit END marker (current convention: the block just
        # ends at the next blank-line-then-code) still get checked up to EOF/blank run.
        if in_block:
            yield path, start_line, block_lines


def _import_lines(block_lines):
    """Return the *module-level* (column-0, unindented) ``import ...`` /
    ``from ... import ...`` lines in a block, normalized (stripped) so trailing
    whitespace differences don't hide a real dup.

    Deliberately excludes indented imports: a PLUGIN-COMPAT block can legitimately
    contain two *different* functions that each do their own local ``import httpx``
    (or similar) in their own scope — that is not the copy-paste duplicate-line bug
    this test guards against, and flagging it would be a false positive.
    """
    return [
        line.strip()
        for line in block_lines
        if (line.startswith("import ") or line.startswith("from "))
    ]


def test_plugin_compat_blocks_exist_and_are_found():
    """Sanity check the scanner itself finds the two known blocks (catches a broken scanner)."""
    found = {path.name for path, _start, _lines in _iter_compat_blocks()}
    assert "managed_tool_gateway.py" in found
    assert "skill_usage.py" in found


def test_no_duplicate_imports_in_plugin_compat_blocks():
    offenders = []
    for path, start_line, block_lines in _iter_compat_blocks():
        seen = set()
        for line in _import_lines(block_lines):
            if line in seen:
                offenders.append(f"{path.relative_to(TOOLS_DIR.parent)} (block at line {start_line}): {line!r}")
            seen.add(line)
    assert not offenders, "Duplicate import(s) found in PLUGIN-COMPAT block(s):\n" + "\n".join(offenders)
