"""Read-only consumer for Fleet's Codex access-token publication.

Fleet owns the rotating OAuth refresh chains.  Hermes may consume a fresh,
refresh-token-neutered publication on a spoke, but it must never refresh,
persist, or fall back from a malformed publication to an independent OAuth
writer.  This module is deliberately independent of ``hermes_cli.auth`` and
``agent.credential_pool`` so the trust boundary can be tested in isolation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


FLEET_CODEX_PUBLICATION_SOURCE = "fleet:custodian-publication"
FLEET_CODEX_PUBLICATION_CONTRACT = "fleet-codex-access-publication/v1"
DEFAULT_FLEET_CODEX_PUBLICATION_DIRNAME = ".hub-publish"
FLEET_CODEX_PUBLICATION_MAX_AGE_SECONDS = 30 * 60
FLEET_CODEX_PUBLICATION_MAX_FUTURE_SKEW_SECONDS = 30

_SAFE_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_POOL_SLOT = re.compile(r"^acct[1-9][0-9]*$")


class FleetCodexPublicationError(RuntimeError):
    """A metadata/schema failure at the Fleet publication trust boundary."""

    def __init__(self, reason: str, *, rate_limited: bool = False) -> None:
        self.reason = str(reason)
        self.rate_limited = bool(rate_limited)
        super().__init__(self.reason)


@dataclass(frozen=True)
class FleetCodexCredential:
    """One validated access-only Fleet publication held in process memory."""

    account_id: str
    slot: str
    label: str
    access_token: str = field(repr=False)
    expires_at: int
    publication_generated: float
    refresh_proof_observed_at: float
    bundle_sha256: str
    last_refresh: Optional[str] = None


@dataclass(frozen=True)
class FleetCodexPublication:
    """Validated publication snapshot; token values are intentionally omitted from repr."""

    configured: bool
    credentials: Tuple[FleetCodexCredential, ...] = ()
    reason: str = "not-configured"
    rate_limited: bool = False
    generated: Optional[float] = None


def default_publication_dir() -> Path:
    """Return the spoke-local mirror populated by ``codex-token-puller``."""
    # HERMES_HOME is normally ``$HOME/.hermes`` (or a profile below it). Using
    # its parent when present keeps isolated profile/test homes isolated from a
    # real user's Fleet mirror, while preserving the normal default.
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    home_root = Path(hermes_home).expanduser().parent if hermes_home else Path.home()
    return home_root / ".codex-pool" / DEFAULT_FLEET_CODEX_PUBLICATION_DIRNAME


def _reject(reason: str, *, rate_limited: bool = False) -> FleetCodexPublicationError:
    # Reasons are fixed metadata labels.  Never include paths, JSON, or token
    # material in an exception because callers may surface the message.
    return FleetCodexPublicationError(reason, rate_limited=rate_limited)


def _read_object(path: Path) -> Dict[str, Any]:
    try:
        if not path.is_file():
            raise _reject("publication-file-missing")
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FleetCodexPublicationError:
        raise
    except Exception as exc:
        raise _reject("publication-json-invalid") from exc
    if not isinstance(value, dict):
        raise _reject("publication-json-not-object")
    return value


def _fresh_timestamp(value: Any, now: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _reject("publication-timestamp-invalid")
    timestamp = float(value)
    age = now - timestamp
    if age < -FLEET_CODEX_PUBLICATION_MAX_FUTURE_SKEW_SECONDS:
        raise _reject("publication-timestamp-in-future")
    if age > FLEET_CODEX_PUBLICATION_MAX_AGE_SECONDS:
        raise _reject("publication-stale")
    return timestamp


def _jwt_exp(access_token: str) -> Optional[int]:
    try:
        parts = access_token.split(".")
        if len(parts) != 3:
            return None
        encoded = parts[1] + ("=" * (-len(parts[1]) % 4))
        claims = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
        exp = claims.get("exp") if isinstance(claims, dict) else None
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            return None
        return int(exp)
    except Exception:
        return None


def _safe_account_id(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value or not _SAFE_ACCOUNT_ID.fullmatch(value):
        return None
    return value


def _slot_is_rate_limited(slot: Dict[str, Any]) -> bool:
    return (
        slot.get("quota_probe_status") == "rate_limited"
        or slot.get("admission_reason") == "quota_rate_limited_backoff"
        or "rate" in str(slot.get("refresh_detail") or "").lower()
    )


def _validate_bundle(
    root: Path,
    account_id: str,
    metadata: Dict[str, Any],
    state_slot: Dict[str, Any],
    publication_generated: float,
    now: float,
) -> FleetCodexCredential:
    label = metadata.get("label")
    if not isinstance(label, str) or not _POOL_SLOT.fullmatch(label):
        raise _reject("publication-slot-invalid")

    observed_at = state_slot.get("observed_at")
    proof_at = state_slot.get("refresh_proof_observed_at")
    _fresh_timestamp(observed_at, now)
    proof_timestamp = _fresh_timestamp(proof_at, now)
    if state_slot.get("model_access_status") != "viable":
        raise _reject("publication-model-access-not-viable")
    if state_slot.get("refresh_status") != "delegated":
        raise _reject("publication-refresh-not-delegated")
    if state_slot.get("refresh_proof_source") != "codex-token-custodian-publish":
        raise _reject("publication-refresh-proof-invalid")
    if state_slot.get("new_work_eligible") is not True:
        raise _reject("publication-not-new-work-eligible")
    repair = state_slot.get("repair")
    if isinstance(repair, dict) and repair.get("terminal_reason"):
        raise _reject("publication-terminal-repair-latch")

    bundle = _read_object(root / f"{account_id}.json")
    # The current Fleet publisher includes this key for legacy Codex spokes.
    # Hermes never consumes it; a non-empty value makes the publication
    # ineligible rather than allowing an API key to cross this boundary.
    api_key_marker = bundle.get("OPENAI_API_KEY")
    if api_key_marker is not None and api_key_marker is not False and api_key_marker != "":
        raise _reject("publication-contains-api-key")
    bundle_digest = hashlib.sha256(
        json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    published_digest = metadata.get("bundle_sha256")
    if (
        not isinstance(published_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", published_digest)
        or published_digest != bundle_digest
    ):
        raise _reject("publication-bundle-digest-mismatch")

    tokens = bundle.get("tokens")
    if not isinstance(tokens, dict):
        raise _reject("publication-token-shape-invalid")
    bundle_account_id = _safe_account_id(tokens.get("account_id"))
    if bundle_account_id != account_id:
        raise _reject("publication-account-mismatch")
    refresh_token = tokens.get("refresh_token")
    if refresh_token not in (None, ""):
        raise _reject("publication-refresh-token-present")
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise _reject("publication-access-token-missing")
    access_token = access_token.strip()

    published_exp = metadata.get("exp")
    if isinstance(published_exp, bool) or not isinstance(published_exp, (int, float)):
        raise _reject("publication-expiry-invalid")
    published_exp = int(published_exp)
    if _jwt_exp(access_token) != published_exp or published_exp <= int(now):
        raise _reject("publication-access-token-expired-or-mismatched")

    return FleetCodexCredential(
        account_id=account_id,
        slot=label,
        label=f"fleet/{label}",
        access_token=access_token,
        expires_at=published_exp,
        publication_generated=publication_generated,
        refresh_proof_observed_at=proof_timestamp,
        bundle_sha256=published_digest,
        last_refresh=(
            metadata.get("last_refresh")
            if isinstance(metadata.get("last_refresh"), str)
            else None
        ),
    )


def read_fleet_codex_publication(
    publication_dir: Optional[Path] = None,
    *,
    now: Optional[float] = None,
) -> FleetCodexPublication:
    """Read and validate the spoke publication without performing network I/O.

    Both the custodian manifest and the watchdog admission snapshot are
    required.  If either artifact is present but invalid, a typed exception is
    raised so callers cannot silently fall back to Hermes-owned refresh state.
    """
    root = Path(publication_dir) if publication_dir is not None else default_publication_dir()
    manifest_path = root / "manifest.json"
    state_path = root / ".pool-watchdog-state.json"
    if not manifest_path.is_file() and not state_path.is_file():
        return FleetCodexPublication(configured=False)
    if not manifest_path.is_file() or not state_path.is_file():
        raise _reject("publication-paired-artifacts-missing")

    current_time = time.time() if now is None else float(now)
    manifest = _read_object(manifest_path)
    state = _read_object(state_path)
    if (
        manifest.get("schema_version") != 1
        or manifest.get("source") != "codex-token-custodian"
        or manifest.get("contract") != FLEET_CODEX_PUBLICATION_CONTRACT
    ):
        raise _reject("publication-contract-invalid")
    publication_generated = _fresh_timestamp(manifest.get("generated"), current_time)
    state_generated = _fresh_timestamp(state.get("generated_at"), current_time)
    if state.get("schema_version") != 2 or state.get("source") != "pool-auth-watchdog":
        raise _reject("publication-health-contract-invalid")
    slots = state.get("slots")
    accounts = manifest.get("accounts")
    if not isinstance(slots, dict) or not isinstance(accounts, dict):
        raise _reject("publication-index-shape-invalid")

    credentials = []
    rate_limited = False
    for account_id_raw, metadata in accounts.items():
        account_id = _safe_account_id(account_id_raw)
        if account_id is None or not isinstance(metadata, dict):
            raise _reject("publication-account-index-invalid")
        label = metadata.get("label")
        if not isinstance(label, str) or not _POOL_SLOT.fullmatch(label):
            raise _reject("publication-slot-invalid")
        state_slot = slots.get(f"codex:{label}")
        if not isinstance(state_slot, dict):
            raise _reject("publication-health-slot-missing")
        rate_limited = rate_limited or _slot_is_rate_limited(state_slot)
        if state_slot.get("new_work_eligible") is not True:
            continue
        credentials.append(
            _validate_bundle(
                root,
                account_id,
                metadata,
                state_slot,
                publication_generated,
                current_time,
            )
        )

    credentials.sort(key=lambda item: int(item.slot[4:]))
    return FleetCodexPublication(
        configured=True,
        credentials=tuple(credentials),
        reason="eligible" if credentials else "no-eligible-publications",
        rate_limited=rate_limited,
        generated=max(publication_generated, state_generated),
    )


def fleet_credential_to_pool_payload(
    credential: FleetCodexCredential,
    *,
    priority: int = 0,
) -> Dict[str, Any]:
    """Create an in-memory pool row; persistence strips its access token."""
    return {
        "id": f"fleet-{credential.account_id}",
        "label": credential.label,
        "auth_type": "oauth",
        "priority": priority,
        "source": FLEET_CODEX_PUBLICATION_SOURCE,
        "access_token": credential.access_token,
        "refresh_token": None,
        "last_refresh": credential.last_refresh,
        "fleet_account_id": credential.account_id,
        "fleet_slot": credential.slot,
        "fleet_bundle_sha256": credential.bundle_sha256,
        "fleet_publication_generated": credential.publication_generated,
        "fleet_refresh_proof_observed_at": credential.refresh_proof_observed_at,
    }
