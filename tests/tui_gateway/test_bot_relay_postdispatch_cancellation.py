"""Offline regressions for Bot Relay cancellation after dispatch.

Source under test is the immutable public NousResearch/hermes-agent commit
``2ddd96aff552b7aa8c48a5543b1687a41cf26c02``.  The fixtures use only temporary
homes, synthetic roster/receipt state, and injected transports; they never open
a gateway, send a message, use credentials, or edit the relay owner.

The sender-side tripwire is intentionally an expected failure on this source:
``delivery_committed=True`` means the envelope is already queued, so a
post-dispatch cancellation must return ``sent_unwatched`` and tell the caller
not to resend.  ``asyncio.CancelledError`` currently bypasses the
``except Exception`` branch and escapes instead.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import bot_mode_dm, bot_relay


SOURCE_COMMIT = "2ddd96aff552b7aa8c48a5543b1687a41cf26c02"


def _structured_params(*, key: str, message_id: str, message: str) -> dict:
    envelope = {
        "schema": bot_relay.ENVELOPE_SCHEMA,
        "message_id": message_id,
        "idempotency_key": key,
        "type": "REQUEST",
        "from_agent": "sender",
        "to_agent": "ops",
        "target_connection": "synthetic-gateway",
        "target_profile": "ops",
        "target_handle": "ops",
        "message": message,
        "scope": {"mutation": "none", "production": "none"},
        "expires_at": time.time() + 60,
        "authority_effect": "none",
    }
    return {
        "profile": "ops",
        "message": message,
        "message_id": message_id,
        "idempotency_key": key,
        "envelope_schema": bot_relay.ENVELOPE_SCHEMA,
        "envelope": envelope,
    }


class TestPostDispatchCancellation(unittest.TestCase):
    @unittest.expectedFailure
    def test_sender_cancellation_after_queue_dispatch_returns_unwatched_ack(self) -> None:
        """A committed queue must not become a resend invitation on cancellation."""
        with tempfile.TemporaryDirectory(prefix="hermes-relay-cancel-") as raw:
            home = Path(raw) / ".hermes"
            target = {
                "profile": "ops",
                "handle": "ops",
                "connection_id": "synthetic-gateway",
                "connection_label": "synthetic gateway",
                "online": True,
            }
            bot_relay.write_remote_roster(home, [target])
            seen: list[dict] = []

            def cancel_after_dispatch(
                _command: str,
                _label: str,
                *,
                task_id,
                agent,
                delivery_committed: bool = False,
            ) -> str:
                seen.append({
                    "task_id": task_id,
                    "agent": agent,
                    "delivery_committed": delivery_committed,
                })
                raise asyncio.CancelledError("synthetic cancellation after waiter dispatch")

            metadata = {
                "type": "REQUEST",
                "idempotency_key": "parent192035:postdispatch-cancel",
                "ttl_seconds": 60,
            }
            result = None
            escaped = None
            with patch.object(bot_mode_dm, "_spawn_delivery", side_effect=cancel_after_dispatch):
                try:
                    result = bot_mode_dm._try_relay_delivery(
                        home,
                        "ops@synthetic-gateway",
                        "synthetic relay payload",
                        "default",
                        task_id="192035",
                        agent=object(),
                        metadata=metadata,
                    )
                except BaseException as exc:  # capture the defect for the assertion below
                    escaped = exc

            self.assertIsNone(
                escaped,
                "post-dispatch cancellation escaped instead of becoming sent_unwatched",
            )
            self.assertEqual(len(seen), 1)
            self.assertTrue(seen[0]["delivery_committed"])
            self.assertIsInstance(result, str)
            self.assertEqual(json.loads(result)["status"], "sent_unwatched")
            queued = list((home / bot_relay.RELAY_DIR_NAME / bot_relay.OUTBOX_DIR).glob("*.json"))
            self.assertEqual(len(queued), 1, "the committed envelope must remain available to the waiter")

    def test_target_cancellation_preserves_pending_receipt_and_blocks_duplicate(self) -> None:
        """Cancellation after target dispatch stays pending until canonical completion."""
        with tempfile.TemporaryDirectory(prefix="hermes-target-cancel-") as raw:
            home = Path(raw) / ".hermes"
            (home / "profiles" / "ops").mkdir(parents=True)
            params = _structured_params(
                key="parent192035:target-postdispatch-cancel",
                message_id="c" * 32,
                message="synthetic target payload",
            )
            calls: list[tuple[str, str]] = []

            def cancel_after_target_dispatch(profile: str, dm_file: str):
                calls.append((profile, dm_file))
                raise asyncio.CancelledError("synthetic cancellation after target accepted dispatch")

            import tui_gateway.server as server

            with patch.dict(server._sessions, {}, clear=True), patch.object(
                server, "_profile_home", lambda _profile: None
            ):
                with self.assertRaises(asyncio.CancelledError):
                    server._methods["bot_relay.deliver"](
                        1,
                        params,
                        _root=lambda: home,
                        _run=cancel_after_target_dispatch,
                    )

                receipt_path = bot_relay.delivery_receipt_path(home, params["idempotency_key"])
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                self.assertEqual(receipt["status"], "started")
                self.assertEqual(len(calls), 1)

                fingerprint = bot_relay.delivery_fingerprint(
                    params["envelope"],
                    target_profile=params["profile"],
                    message=params["message"],
                    structured=True,
                )
                self.assertEqual(
                    bot_relay.read_idempotent_delivery(
                        home,
                        params["idempotency_key"],
                        message_id=params["message_id"],
                        delivery_fingerprint=fingerprint,
                        target_connection=params["envelope"]["target_connection"],
                        target_profile=params["envelope"]["target_profile"],
                        target_handle=params["envelope"]["target_handle"],
                    ),
                    {"disposition": "pending", "reason": "target_receipt_pending"},
                )

                retry = server._methods["bot_relay.deliver"](
                    2,
                    params,
                    _root=lambda: home,
                    _run=cancel_after_target_dispatch,
                )
                self.assertEqual(retry["error"]["data"]["reason"], "duplicate_ambiguous")
                self.assertEqual(len(calls), 1, "an ambiguous receipt must not run a duplicate turn")

                readback = server._methods["bot_relay.receipt.read"](
                    3, params, _root=lambda: home
                )
                self.assertEqual(readback["error"]["data"]["reason"], "target_receipt_pending")


if __name__ == "__main__":
    unittest.main()
