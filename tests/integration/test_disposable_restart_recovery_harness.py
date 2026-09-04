"""Hermetic Hermes restart/crash-recovery probes.

These tests exercise the existing persistence contracts from
``docs/session-lifecycle.md`` and ``docs/state-db-recovery.md`` without
starting a gateway, resolving a provider, sending a message, or opening the
live ``state.db``.  Every durable path is either a temporary JSON snapshot or
an explicitly named temporary SQLite file.  The child-process probes reuse
the conformance harness so an interrupted writer is a real OS process with a
bounded wait and explicit reap.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_state
from gateway.config import GatewayConfig
from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore
from gateway.session import SessionEntry, SessionStore
from hermes_state import SessionDB, SessionTurnLeaseLostError
from tests.conformance.persistence._harness import (
    kill9_and_reap,
    spawn_child,
    wait_for,
)


def _routing_entry() -> SessionEntry:
    """Return a small valid routing entry for the fake authoritative store."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return SessionEntry(
        session_key="opaque-routing-key",
        session_id="opaque-session-id",
        created_at=now,
        updated_at=now,
    )


class _FakeRoutingDB:
    """Minimal authoritative routing seam; it never opens SQLite."""

    def __init__(self, entry: SessionEntry):
        self._entry_json = json.dumps(entry.to_dict())

    def load_gateway_routing_entries(self, *, scope: str) -> dict[str, str]:
        del scope
        return {"opaque-routing-key": self._entry_json}

    def get_session(self, session_id: str):
        del session_id
        # A routing-only fake has no transcript rows to prune.
        return None


class _DisposableHarness:
    """Own one child and one temporary root, with deterministic teardown."""

    def __init__(self):
        self._temporary_directory = None
        self.root: Path | None = None
        self.child = None

    def __enter__(self) -> "_DisposableHarness":
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="hermes-restart-recovery-"
        )
        self.root = Path(self._temporary_directory.name)
        ready = self.root / "child-ready"
        script = (
            "import time\n"
            "from pathlib import Path\n"
            f"Path({str(ready)!r}).write_text('ready', encoding='utf-8')\n"
            "time.sleep(300)\n"
        )
        self.child = spawn_child(
            script,
            env={"HERMES_HOME": str(self.root / "hermes-home")},
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if self.child is not None and self.child.poll() is None:
                kill9_and_reap(self.child)
            elif self.child is not None:
                self.child.wait(timeout=5)
        finally:
            if self.child is not None:
                if self.child.stdout is not None:
                    self.child.stdout.close()
                if self.child.stderr is not None:
                    self.child.stderr.close()
            if self._temporary_directory is not None:
                self._temporary_directory.cleanup()
        return False


def test_interrupted_atomic_snapshot_keeps_last_complete_snapshot(tmp_path):
    """Killing a writer before replace cannot truncate the prior snapshot."""
    sessions_dir = tmp_path / "routing-snapshot"
    sessions_dir.mkdir()
    snapshot = sessions_dir / "sessions.json"
    old_data = {"opaque-routing-key": {"session_id": "old"}}
    snapshot.write_text(json.dumps(old_data), encoding="utf-8")
    ready = sessions_dir / "replace-ready"

    script = (
        "import time\n"
        "from pathlib import Path\n"
        "import gateway.session as session_module\n"
        "from gateway.session import SessionStore\n"
        "class BareStore:\n"
        f"    sessions_dir = Path({str(sessions_dir)!r})\n"
        "def blocked_atomic_replace(*args):\n"
        f"    Path({str(ready)!r}).write_text('ready', encoding='utf-8')\n"
        "    while True:\n"
        "        time.sleep(0.05)\n"
        "session_module.atomic_replace = blocked_atomic_replace\n"
        "SessionStore._save_sessions_json(BareStore(), "
        "{'opaque-routing-key': {'session_id': 'new'}})\n"
    )
    child = spawn_child(
        script,
        env={"HERMES_HOME": str(tmp_path / "hermes-home")},
    )
    try:
        wait_for(ready.exists, what="atomic snapshot replacement barrier", child=child)
        assert child.poll() is None
        assert json.loads(snapshot.read_text(encoding="utf-8")) == old_data
        assert list(sessions_dir.glob(".sessions_*.tmp")), (
            "the interrupted writer should remain staged, not partially replace "
            "the committed snapshot"
        )
    finally:
        kill9_and_reap(child)
        if child.stdout is not None:
            child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()


def test_malformed_legacy_snapshot_does_not_erase_authoritative_routing(tmp_path):
    """A malformed legacy mirror cannot replace a valid routing source."""
    sessions_dir = tmp_path / "routing-snapshot"
    sessions_dir.mkdir()
    (sessions_dir / "sessions.json").write_text("{not-json", encoding="utf-8")

    store = SessionStore(sessions_dir, GatewayConfig())
    store._db = _FakeRoutingDB(_routing_entry())
    store._ensure_loaded()

    assert store._entries["opaque-routing-key"].session_id == "opaque-session-id"
    assert store._loaded is True


def test_duplicate_completion_is_reused_after_fresh_store_restart(tmp_path):
    """A completed idempotency reservation admits one fake completion."""
    db_path = tmp_path / "runs.sqlite3"
    completion_calls: list[str] = []
    scope = "hermetic-restart"
    key = "opaque-completion-key"
    fingerprint = "opaque-request-fingerprint"

    first = RunIdempotencyStore(str(db_path))
    try:
        outcome, record = first.reserve(
            scope,
            key,
            fingerprint,
            "opaque-run-1",
            {"status": "running"},
        )
        assert outcome == "created"
        assert record["run_id"] == "opaque-run-1"
        if outcome == "created":
            completion_calls.append("opaque-completion-key")
        first.update_status(
            "opaque-run-1",
            {"status": "completed", "result": "opaque-result"},
        )
    finally:
        first.close()

    restarted = RunIdempotencyStore(str(db_path))
    try:
        outcome, record = restarted.reserve(
            scope,
            key,
            fingerprint,
            "opaque-run-2",
            {"status": "running"},
        )
        assert outcome == "reused"
        assert record["run_id"] == "opaque-run-1"
        assert record["status"] == {
            "result": "opaque-result",
            "status": "completed",
        }
        if outcome == "created":
            completion_calls.append("opaque-completion-key")
    finally:
        restarted.close()

    assert completion_calls == ["opaque-completion-key"]


def test_stale_turn_lease_is_reclaimed_and_fences_late_flush(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """A fresh process-shaped holder reclaims a dead lease before writing."""
    db = SessionDB(tmp_path / "lease.sqlite3")
    try:
        db.create_session("opaque-session", source="test")
        stale_holder = "pid=424242:turn=stale"
        fresh_holder = "pid=424243:turn=fresh"
        assert db.try_acquire_session_turn_lease(
            "opaque-session", stale_holder, ttl_seconds=300
        )

        monkeypatch.setattr(
            hermes_state,
            "psutil",
            SimpleNamespace(pid_exists=lambda pid: False),
        )
        assert db.try_acquire_session_turn_lease(
            "opaque-session", fresh_holder, ttl_seconds=300
        )

        with pytest.raises(SessionTurnLeaseLostError):
            db.append_message(
                "opaque-session",
                "assistant",
                "late stale flush",
                turn_lease_holder=stale_holder,
            )
        db.append_message(
            "opaque-session",
            "assistant",
            "fresh flush",
            turn_lease_holder=fresh_holder,
        )
        assert [
            row["content"]
            for row in db.get_messages("opaque-session", include_inactive=True)
        ] == ["fresh flush"]
    finally:
        db.close()


def test_harness_teardown_reaps_child_and_removes_owned_root():
    """The harness owns and fully tears down its child and temporary files."""
    with _DisposableHarness() as harness:
        assert harness.root is not None
        ready = harness.root / "child-ready"
        wait_for(ready.exists, what="disposable harness child", child=harness.child)
        assert harness.child.poll() is None
        assert ready.read_text(encoding="utf-8") == "ready"

    assert harness.child.poll() is not None
    assert harness.root is not None
    assert not harness.root.exists()
