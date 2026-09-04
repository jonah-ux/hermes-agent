"""Contract and pool-integration tests for the Fleet Codex bridge."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path

import pytest

import hermes_cli.auth as auth
from agent.credential_pool import (
    FLEET_CODEX_PUBLICATION_SOURCE,
    STATUS_DEAD,
    STATUS_EXHAUSTED,
    load_pool,
)
from agent.fleet_codex_publication import (
    FleetCodexPublicationError,
    read_fleet_codex_publication,
)
from hermes_cli.auth import AuthError, CODEX_RATE_LIMITED_CODE, resolve_codex_runtime_credentials


def _jwt(exp: int, marker: str) -> str:
    def part(payload: dict) -> str:
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
        return encoded.rstrip(b"=").decode("ascii")

    return f"{part({'alg': 'none', 'typ': 'JWT'})}.{part({'exp': exp, 'sub': marker})}.sig"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def _write_publication(
    root: Path,
    *,
    now: int | None = None,
    accounts: tuple[tuple[str, str, str], ...] = (("account-1", "acct1", "one"),),
    eligible: dict[str, bool] | None = None,
    rate_limited: set[str] | None = None,
) -> dict[str, str]:
    now = int(time.time()) if now is None else now
    eligible = eligible or {slot: True for _, slot, _ in accounts}
    rate_limited = rate_limited or set()
    manifest = {
        "schema_version": 1,
        "source": "codex-token-custodian",
        "contract": "fleet-codex-access-publication/v1",
        "generated": now - 3,
        "accounts": {},
    }
    slots: dict[str, dict] = {}
    access_by_account: dict[str, str] = {}
    for index, (account_id, slot, marker) in enumerate(accounts, start=1):
        access = _jwt(now + 86400 + index, marker)
        bundle = {
            "auth_mode": "chatgpt",
            "tokens": {
                "account_id": account_id,
                "access_token": access,
                "refresh_token": "",
            },
            "last_refresh": "2026-09-04T00:00:00Z",
            # Fleet emits an explicit false sentinel when no API-key provider
            # is configured; Hermes accepts that marker but never a value.
            "OPENAI_API_KEY": False,
        }
        digest = hashlib.sha256(
            json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        _write_json(root / f"{account_id}.json", bundle)
        manifest["accounts"][account_id] = {
            "label": slot,
            "exp": now + 86400 + index,
            "bundle_sha256": digest,
            "last_refresh": bundle["last_refresh"],
        }
        slots[f"codex:{slot}"] = {
            "observed_at": now - 10,
            "model_access_status": "viable" if eligible.get(slot) else "unknown",
            "refresh_status": "delegated",
            "refresh_detail": "fresh-custodian-publish",
            "refresh_proof_source": "codex-token-custodian-publish",
            "refresh_proof_observed_at": now - 8,
            "quota_probe_status": "rate_limited" if slot in rate_limited else "available",
            "new_work_eligible": bool(eligible.get(slot)),
            "admission_reason": (
                "quota_rate_limited_backoff" if slot in rate_limited else "model_access_viable_refresh_delegated"
            ),
        }
        access_by_account[account_id] = access
    _write_json(
        root / "manifest.json",
        manifest,
    )
    _write_json(
        root / ".pool-watchdog-state.json",
        {
            "schema_version": 2,
            "generated_at": now - 2,
            "source": "pool-auth-watchdog",
            "slots": slots,
        },
    )
    return access_by_account


def _write_hermes_auth(home: Path) -> None:
    _write_json(
        home / "auth.json",
        {
            "version": 1,
            "active_provider": "openai-codex",
            "providers": {
                "openai-codex": {
                    "tokens": {
                        "access_token": "hermes-owned-access",
                        "refresh_token": "hermes-owned-refresh",
                    },
                    "auth_mode": "chatgpt",
                }
            },
        },
    )


def test_valid_publication_is_preferred_without_refresh_or_auth_store_write(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    publication = tmp_path / ".codex-pool" / ".hub-publish"
    _write_hermes_auth(hermes_home)
    access_by_account = _write_publication(publication)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(
        auth,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: pytest.fail("Fleet source attempted OAuth refresh"),
    )

    resolved = resolve_codex_runtime_credentials(force_refresh=True)

    assert resolved["source"] == FLEET_CODEX_PUBLICATION_SOURCE
    assert resolved["api_key"] == next(iter(access_by_account.values()))
    assert resolved["fleet_slot"] == "acct1"
    stored = (hermes_home / "auth.json").read_text()
    assert "hermes-owned-refresh" in stored
    assert resolved["api_key"] not in stored


def test_present_but_malformed_publication_fails_closed_before_hermes_refresh(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    publication = tmp_path / ".codex-pool" / ".hub-publish"
    _write_hermes_auth(hermes_home)
    _write_publication(publication)
    manifest = json.loads((publication / "manifest.json").read_text())
    manifest["contract"] = "wrong-contract"
    _write_json(publication / "manifest.json", manifest)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(auth, "_read_codex_tokens", lambda: pytest.fail("fallback was used"))

    with pytest.raises(AuthError) as exc:
        resolve_codex_runtime_credentials()

    assert exc.value.code == "codex_fleet_publication_invalid"
    assert exc.value.relogin_required is False


def test_rate_limited_fleet_publication_does_not_prompt_for_reauth(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    publication = tmp_path / ".codex-pool" / ".hub-publish"
    _write_hermes_auth(hermes_home)
    _write_publication(
        publication,
        accounts=(("account-1", "acct1", "one"),),
        eligible={"acct1": False},
        rate_limited={"acct1"},
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(
        auth,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: pytest.fail("legacy Hermes row was selected"),
    )

    with pytest.raises(AuthError) as exc:
        resolve_codex_runtime_credentials()

    assert exc.value.code == CODEX_RATE_LIMITED_CODE
    assert exc.value.relogin_required is False
    pool = load_pool("openai-codex")
    assert pool.select() is None


def test_pool_uses_only_fleet_rows_and_neuters_persisted_reference(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    publication = tmp_path / ".codex-pool" / ".hub-publish"
    _write_hermes_auth(hermes_home)
    access_by_account = _write_publication(
        publication,
        accounts=(("account-1", "acct1", "one"), ("account-2", "acct2", "two")),
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(
        auth,
        "refresh_codex_oauth_pure",
        lambda *_args, **_kwargs: pytest.fail("Fleet pool row attempted OAuth refresh"),
    )

    pool = load_pool("openai-codex")
    selected = pool.select()
    assert selected is not None
    assert selected.source == FLEET_CODEX_PUBLICATION_SOURCE
    assert selected.refresh_token is None
    assert selected.access_token in access_by_account.values()
    assert pool.try_refresh_current() is not None

    stored = json.loads((hermes_home / "auth.json").read_text())
    fleet_rows = [
        row
        for row in stored.get("credential_pool", {}).get("openai-codex", [])
        if row.get("source") == FLEET_CODEX_PUBLICATION_SOURCE
    ]
    assert fleet_rows
    assert all("access_token" not in row and "refresh_token" not in row for row in fleet_rows)
    assert all("fleet_account_id" in row for row in fleet_rows)
    assert not any(value in (hermes_home / "auth.json").read_text() for value in access_by_account.values())


def test_401_and_429_rotate_only_across_fleet_rows(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    publication = tmp_path / ".codex-pool" / ".hub-publish"
    _write_hermes_auth(hermes_home)
    _write_publication(
        publication,
        accounts=(("account-1", "acct1", "one"), ("account-2", "acct2", "two")),
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    pool = load_pool("openai-codex")
    first = pool.select()
    assert first is not None
    second = pool.mark_exhausted_and_rotate(
        status_code=401,
        credential_id=first.id,
        error_context={"reason": "token_invalidated"},
    )
    assert second is not None
    assert second.source == FLEET_CODEX_PUBLICATION_SOURCE
    assert next(row for row in pool.entries() if row.id == first.id).last_status == STATUS_DEAD

    third = pool.mark_exhausted_and_rotate(
        status_code=429,
        credential_id=second.id,
        error_context={"reason": "rate_limit"},
    )
    assert third is None
    assert next(row for row in pool.entries() if row.id == second.id).last_status == STATUS_EXHAUSTED


def test_publication_reader_rejects_refresh_material_without_exposing_it(tmp_path):
    publication = tmp_path / "publication"
    _write_publication(publication)
    bundle_path = publication / "account-1.json"
    bundle = json.loads(bundle_path.read_text())
    bundle["tokens"]["refresh_token"] = "must-not-cross"
    _write_json(bundle_path, bundle)
    manifest_path = publication / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["accounts"]["account-1"]["bundle_sha256"] = hashlib.sha256(
        json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    _write_json(manifest_path, manifest)

    with pytest.raises(FleetCodexPublicationError) as exc:
        read_fleet_codex_publication(publication)

    assert exc.value.reason == "publication-refresh-token-present"
    assert "must-not-cross" not in str(exc.value)


def test_publication_reader_rejects_api_key_value(tmp_path):
    publication = tmp_path / "publication"
    _write_publication(publication)
    bundle_path = publication / "account-1.json"
    bundle = json.loads(bundle_path.read_text())
    bundle["OPENAI_API_KEY"] = "synthetic-api-key"
    _write_json(bundle_path, bundle)
    manifest_path = publication / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["accounts"]["account-1"]["bundle_sha256"] = hashlib.sha256(
        json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    _write_json(manifest_path, manifest)

    with pytest.raises(FleetCodexPublicationError) as exc:
        read_fleet_codex_publication(publication)

    assert exc.value.reason == "publication-contains-api-key"
    assert "synthetic-api-key" not in str(exc.value)
