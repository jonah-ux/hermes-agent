"""Adversarial transcript authorization checks for multiplexed profiles.

These tests intentionally use the same disposable session id in two profile
stores.  They exercise the backend/session surfaces directly; Desktop owner
selection and gateway role/allowlist inheritance are separate concerns.

The pinned Hermes commit currently has no persisted-owner check on these read
paths.  ``strict=True`` makes the test an explicit regression guard: it is an
expected failure on the pinned source and must become a pass when the owning
fix lands.
"""

import json
from collections import namedtuple

import pytest


SESSION_ID = "synthetic-shared-session-2ddd"
ALPHA_MARKER = "synthetic-alpha-transcript-2ddd"
BETA_MARKER = "synthetic-beta-transcript-2ddd"


@pytest.fixture
def profile_client(tmp_path, monkeypatch, _isolate_hermes_home):
    """Create two isolated named profiles and an authenticated dashboard client."""
    from starlette.testclient import TestClient

    from hermes_cli import profiles
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app
    from hermes_constants import get_hermes_home

    default_home = get_hermes_home()
    profiles_root = default_home / "profiles"
    profiles_root.mkdir(parents=True, exist_ok=True)
    homes = {}
    for name in ("alpha", "beta"):
        home = profiles_root / name
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
        homes[name] = home

    # Keep profile resolution on the disposable root.  The production profile
    # directory and credentials are never consulted by this test process.
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: default_home)

    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", default_home / "state.db")
    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return client, homes, default_home


def _seed_session(home, *, persisted_owner: str, marker: str, user_id: str) -> None:
    from hermes_state import SessionDB

    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(
            SESSION_ID,
            source="cli",
            user_id=user_id,
            model="synthetic-test-model",
            profile_name=persisted_owner,
        )
        db.append_messages_batch(
            SESSION_ID,
            [
                {"role": "user", "content": f"{marker} user"},
                {"role": "assistant", "content": f"{marker} assistant"},
            ],
        )
        db._conn.commit()
    finally:
        db.close()


def _seed_cross_profile_same_id(homes) -> None:
    """Put each profile's synthetic transcript under the other profile's owner stamp."""
    _seed_session(
        homes["alpha"],
        persisted_owner="beta",
        marker=BETA_MARKER,
        user_id="synthetic-beta-scope",
    )
    _seed_session(
        homes["beta"],
        persisted_owner="alpha",
        marker=ALPHA_MARKER,
        user_id="synthetic-alpha-scope",
    )


def _read_messages(client, profile: str):
    return client.get(
        f"/api/sessions/{SESSION_ID}/messages",
        params={"profile": profile, "include_compacted": "true", "limit": 50},
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Pinned 2ddd has no backend persisted-owner check for profile-scoped "
        "session detail, transcript, export, or search reads"
    ),
)
def test_dashboard_reads_fail_closed_on_mismatched_persisted_profile_owner(profile_client):
    """A requested profile must not serve a row stamped for its sibling profile."""
    client, homes, _default_home = profile_client
    _seed_cross_profile_same_id(homes)

    observed = {}
    for requested_profile, foreign_marker in (("alpha", BETA_MARKER), ("beta", ALPHA_MARKER)):
        detail = client.get(
            f"/api/sessions/{SESSION_ID}", params={"profile": requested_profile}
        )
        messages = _read_messages(client, requested_profile)
        exported = client.get(
            f"/api/sessions/{SESSION_ID}/export", params={"profile": requested_profile}
        )
        search = client.get(
            "/api/sessions/search",
            params={"q": foreign_marker, "profile": requested_profile},
        )

        messages_body = messages.json() if messages.is_success else {}
        export_body = exported.json() if exported.is_success else {}
        search_body = search.json() if search.is_success else {}

        observed[requested_profile] = {
            "detail": {
                "status": detail.status_code,
                "profile_name": detail.json().get("profile_name") if detail.is_success else None,
                "user_id": detail.json().get("user_id") if detail.is_success else None,
            },
            "messages": {
                "status": messages.status_code,
                "marker_present": foreign_marker in json.dumps(messages_body),
            },
            "export": {
                "status": exported.status_code,
                "marker_present": foreign_marker in json.dumps(export_body),
            },
            "search": {
                "status": search.status_code,
                "marker_present": foreign_marker in json.dumps(search_body),
            },
        }

    # The secure contract is fail-closed for every read shape.  Accept either
    # 403 or 404 for a hidden/mismatched owner, but never a body containing the
    # sibling marker or sibling principal.
    leaks = {}
    for profile, result in observed.items():
        detail = result["detail"]
        messages = result["messages"]
        exported = result["export"]
        search = result["search"]
        if detail["status"] not in (403, 404):
            leaks.setdefault(profile, {})["detail"] = detail
        if messages["status"] not in (403, 404) or messages["marker_present"]:
            leaks.setdefault(profile, {})["messages"] = messages
        if exported["status"] not in (403, 404) or exported["marker_present"]:
            leaks.setdefault(profile, {})["export"] = exported
        if search["status"] not in (403, 404) or search["marker_present"]:
            leaks.setdefault(profile, {})["search"] = search

    assert not leaks, "mismatched profile owner was served: " + json.dumps(leaks, sort_keys=True)


def test_profile_scoped_reads_are_disposable_and_distinct_when_owner_matches(profile_client):
    """Control case: same id can be read from each explicitly selected owner store."""
    client, homes, _default_home = profile_client
    _seed_session(
        homes["alpha"],
        persisted_owner="alpha",
        marker=ALPHA_MARKER,
        user_id="synthetic-alpha-scope",
    )
    _seed_session(
        homes["beta"],
        persisted_owner="beta",
        marker=BETA_MARKER,
        user_id="synthetic-beta-scope",
    )

    for profile, own_marker, foreign_marker in (
        ("alpha", ALPHA_MARKER, BETA_MARKER),
        ("beta", BETA_MARKER, ALPHA_MARKER),
    ):
        detail = client.get(f"/api/sessions/{SESSION_ID}", params={"profile": profile})
        messages = _read_messages(client, profile)
        exported = client.get(
            f"/api/sessions/{SESSION_ID}/export", params={"profile": profile}
        )
        search = client.get(
            "/api/sessions/search", params={"q": own_marker, "profile": profile}
        )

        assert detail.status_code == 200
        assert detail.json()["profile_name"] == profile
        assert detail.json()["user_id"] == f"synthetic-{profile}-scope"
        assert messages.status_code == 200
        message_text = json.dumps(messages.json())
        assert own_marker in message_text
        assert foreign_marker not in message_text
        assert exported.status_code == 200
        export_text = json.dumps(exported.json())
        assert own_marker in export_text
        assert foreign_marker not in export_text
        assert search.status_code == 200
        assert own_marker in json.dumps(search.json())
        assert foreign_marker not in json.dumps(search.json())


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Pinned 2ddd session_search falls back across every profile even when "
        "the caller supplied an explicit profile scope"
    ),
)
def test_session_search_does_not_fallback_outside_explicit_profile_scope(profile_client, monkeypatch):
    """An explicit profile miss must not scan and return a sibling transcript."""
    _client, homes, default_home = profile_client
    from hermes_state import SessionDB
    from hermes_cli import profiles
    from tools.session_search_tool import session_search

    # Alpha exists but does not contain the id. Beta owns the only copy.
    alpha_db = SessionDB(db_path=homes["alpha"] / "state.db")
    alpha_db.close()
    _seed_session(
        homes["beta"],
        persisted_owner="beta",
        marker=BETA_MARKER,
        user_id="synthetic-beta-scope",
    )
    default_db = SessionDB(db_path=default_home / "state.db")
    try:
        Info = namedtuple("Info", "name path")
        monkeypatch.setattr(
            profiles,
            "list_profiles",
            lambda: [Info("alpha", homes["alpha"]), Info("beta", homes["beta"])],
        )
        result = json.loads(
            session_search(db=default_db, session_id=SESSION_ID, profile="alpha")
        )
    finally:
        default_db.close()

    assert result.get("success") is False, "explicit alpha scope returned beta data: " + json.dumps(result)
