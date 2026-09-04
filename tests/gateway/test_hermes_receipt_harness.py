"""Adversarial relay-receipt fixtures for candidate2ddd.

These tests exercise the existing Desktop-to-gateway relay boundary with
synthetic metadata only.  They never open media, contact a peer, or start a
real Hermes process.  The strict ``xfail`` cases are deliberate: they are
owner-facing regression tests for the identity fields that the current
implementation drops or omits from its delivery fingerprint.  When the
owner fixes the binding, an unexpected pass turns this file red until the
``xfail`` marker is removed.

The target-profile case is a normal passing test.  It records the important
negative finding from the investigation: swapping the explicit target
profile is already rejected, so this lane must not claim a profile defect.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import tui_gateway.server as srv
from tools import bot_relay


MESSAGE = "inspect the synthetic relay artifact"
IDEMPOTENCY_KEY = "candidate2ddd:relay:message-1"
CONTENT_DIGEST_A = "sha256:" + ("a" * 64)
CONTENT_DIGEST_B = "sha256:" + ("b" * 64)
ATTACHMENT_METADATA_A = {
    "name": "plan-a.txt",
    "kind": "file",
    "size": 11,
    "sha256": "a" * 64,
}
ATTACHMENT_METADATA_B = {
    "name": "plan-b.txt",
    "kind": "file",
    "size": 17,
    "sha256": "b" * 64,
}
ATTACHMENTS_A = [
    {"name": "plan-a.txt", "kind": "file", "sha256": "a" * 64}
]
ATTACHMENTS_B = [
    {"name": "plan-b.txt", "kind": "file", "sha256": "b" * 64}
]


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give each case an isolated synthetic Hermes home and session table."""

    hermes_home = tmp_path / ".hermes"
    for profile in ("ops", "other"):
        (hermes_home / "profiles" / profile).mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    srv._sessions.clear()
    yield hermes_home
    srv._sessions.clear()


def _relay_params(
    *,
    profile: str = "ops",
    target_connection: str = "connection-a",
    message_id: str = "a" * 32,
    attachment_metadata: dict = ATTACHMENT_METADATA_A,
    attachments: list[dict] = ATTACHMENTS_A,
    content_digest: str = CONTENT_DIGEST_A,
) -> dict:
    """Build one valid v2 envelope with optional identity mutations."""

    envelope = {
        "schema": "asm-hermes-a2a-envelope/v2",
        "message_id": message_id,
        "idempotency_key": IDEMPOTENCY_KEY,
        "type": "REQUEST",
        "from_agent": "sender",
        "to_agent": profile,
        "target_profile": profile,
        "target_connection": target_connection,
        "message": MESSAGE,
        "scope": {"mutation": "none", "production": "none"},
        "expires_at": time.time() + 600,
        "authority_effect": "none",
        "attachment_metadata": attachment_metadata,
        "attachments": attachments,
        "content_digest": content_digest,
    }
    return {
        "profile": profile,
        "message": MESSAGE,
        "message_id": message_id,
        "idempotency_key": IDEMPOTENCY_KEY,
        "envelope_schema": envelope["schema"],
        "envelope": envelope,
    }


def _deliver_twice(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_params: dict,
    second_params: dict,
) -> tuple[dict, list[tuple[tuple, dict]], dict]:
    """Complete one synthetic turn, then attempt a mutated replay."""

    calls: list[tuple[tuple, dict]] = []

    class Process:
        returncode = 0
        stdout = "synthetic relay response"
        stderr = ""

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    # The gateway handler imports subprocess itself; patching the module-level
    # function keeps the test hermetic while preserving the argv/receipt path.
    monkeypatch.setattr("subprocess.run", fake_run)
    first = srv._methods["bot_relay.deliver"](1, first_params)
    assert "error" not in first, first

    receipt_path = bot_relay.delivery_receipt_path(home, IDEMPOTENCY_KEY)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    second = srv._methods["bot_relay.deliver"](2, second_params)
    return second, calls, receipt


def _assert_idempotency_conflict(response: dict) -> None:
    assert response["error"]["data"]["reason"] == "idempotency_conflict", response


@pytest.mark.xfail(
    strict=True,
    reason="relay enqueue currently drops attachment metadata, attachments, and content_digest",
)
def test_enqueue_preserves_content_identity_metadata(home: Path) -> None:
    target = {
        "profile": "ops",
        "handle": "ops",
        "connection_id": "connection-a",
    }
    expected = {
        "attachment_metadata": ATTACHMENT_METADATA_A,
        "attachments": ATTACHMENTS_A,
        "content_digest": CONTENT_DIGEST_A,
    }
    envelope = bot_relay.enqueue_envelope(
        home,
        target=target,
        message=MESSAGE,
        sender_profile="sender",
        sender_handle="sender",
        metadata=expected,
    )
    outbox_path = bot_relay.relay_root(home) / bot_relay.OUTBOX_DIR / f"{envelope['id']}.json"
    serialized = json.loads(outbox_path.read_text(encoding="utf-8"))
    for field, value in expected.items():
        assert serialized[field] == value


@pytest.mark.xfail(
    strict=True,
    reason="completed relay receipts omit structured target/content identity fields",
)
def test_completed_receipt_records_target_and_content_identity(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, receipt = _deliver_twice(
        home,
        monkeypatch,
        _relay_params(),
        _relay_params(),
    )
    assert receipt["target_connection"] == "connection-a"
    assert receipt["target_profile"] == "ops"
    assert receipt["content_digest"] == CONTENT_DIGEST_A
    assert receipt["attachment_metadata"] == ATTACHMENT_METADATA_A


@pytest.mark.xfail(
    strict=True,
    reason="delivery fingerprint omits attachment metadata and attachment identity",
)
def test_swapped_attachment_metadata_cannot_replay_target_receipt(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second, calls, _ = _deliver_twice(
        home,
        monkeypatch,
        _relay_params(),
        _relay_params(
            attachment_metadata=ATTACHMENT_METADATA_B,
            attachments=ATTACHMENTS_B,
        ),
    )
    _assert_idempotency_conflict(second)
    assert len(calls) == 1


@pytest.mark.xfail(
    strict=True,
    reason="delivery fingerprint omits relayed content_digest",
)
def test_swapped_content_digest_cannot_replay_target_receipt(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second, calls, _ = _deliver_twice(
        home,
        monkeypatch,
        _relay_params(),
        _relay_params(content_digest=CONTENT_DIGEST_B),
    )
    _assert_idempotency_conflict(second)
    assert len(calls) == 1


def test_swapped_message_id_cannot_replay_target_receipt(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second, calls, _ = _deliver_twice(
        home,
        monkeypatch,
        _relay_params(),
        _relay_params(message_id="b" * 32),
    )
    _assert_idempotency_conflict(second)
    assert len(calls) == 1


def test_swapped_target_connection_cannot_replay_target_receipt(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second, calls, _ = _deliver_twice(
        home,
        monkeypatch,
        _relay_params(),
        _relay_params(target_connection="connection-b"),
    )
    _assert_idempotency_conflict(second)
    assert len(calls) == 1


def test_swapped_target_profile_is_already_rejected(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second, calls, _ = _deliver_twice(
        home,
        monkeypatch,
        _relay_params(),
        _relay_params(profile="other", target_connection="connection-a"),
    )
    _assert_idempotency_conflict(second)
    assert len(calls) == 1
