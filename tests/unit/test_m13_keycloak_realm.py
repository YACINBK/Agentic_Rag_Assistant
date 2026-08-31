"""Unit tests for M13 — Keycloak realm import (closes D39).

Contract: contracts/m13_keycloak_realm_import.md, test cases 1–7.

**Scope, stated plainly.** These tests prove the realm file is shaped correctly and
stays consistent with `app/core/settings.py` and `docker-compose.yml`. They do not
prove that a login works — no Keycloak runs here, no token is issued, no callback
is exercised. The manual checklist in the contract is the only thing that proves
that, and §6 of the summary records it as unverified.

What they buy instead is the class of failure that is expensive to diagnose live:
a client secret that drifted from `.env`, a redirect URI aimed at Keycloak's own
port, a role that crept back into the realm, a seeded user who would be stopped by
an "update your password" screen, or a bind mount whose source path does not exist
(docker silently creates an empty *directory* for a missing source, and the import
then finds nothing to import).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

from app.core.settings import settings

REPO_ROOT = Path(__file__).resolve().parents[2]
REALM_PATH = REPO_ROOT / "deploy" / "keycloak" / "whitecape-realm.json"
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"

# Where Keycloak's `--import-realm` looks. Anything mounted outside this directory
# is invisible to the importer no matter how correct the file is.
IMPORT_DIR = "/opt/keycloak/data/import/"

APP_ORIGIN = "http://localhost:8000"
CALLBACK_PATH = "/auth/callback"
# RP-initiated logout sends the browser back here. Keycloak validates a
# post_logout_redirect_uri against the SAME redirectUris list as the callback, so
# this path has to be registered too — see the logout assertion below.
LOGIN_PATH = "/auth/login"
KEYCLOAK_PORT = 8080
EXPECTED_USER_COUNT = 4


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def realm() -> dict:
    assert REALM_PATH.is_file(), f"realm file missing at {REALM_PATH}"
    return json.loads(REALM_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def app_client(realm: dict) -> dict:
    """The one client the application authenticates as."""
    clients = realm.get("clients", [])
    matching = [c for c in clients if c.get("clientId") == settings.KEYCLOAK_CLIENT_ID]
    assert len(matching) == 1, (
        f"expected exactly one client with clientId={settings.KEYCLOAK_CLIENT_ID!r}, "
        f"found {[c.get('clientId') for c in clients]}"
    )
    return matching[0]


@pytest.fixture(scope="module")
def compose() -> dict:
    assert COMPOSE_PATH.is_file(), f"docker-compose.yml missing at {COMPOSE_PATH}"
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def keycloak_service(compose: dict) -> dict:
    services = compose.get("services", {})
    assert "keycloak" in services, "no keycloak service in docker-compose.yml"
    return services["keycloak"]


# ---------------------------------------------------------------------------
# Case 1 — assertions 1, 2, 3
# ---------------------------------------------------------------------------


def test_realm_file_matches_settings(realm: dict, app_client: dict) -> None:
    """The three values the OIDC handshake needs must agree with the app's config.

    A mismatch here is invisible until a live login fails with a generic
    `invalid_client` — which reads like a Keycloak problem and is actually a
    one-character diff between two files.
    """
    assert realm["realm"] == settings.KEYCLOAK_REALM
    assert realm["enabled"] is True

    assert app_client["clientId"] == settings.KEYCLOAK_CLIENT_ID
    assert app_client["publicClient"] is False
    assert app_client["secret"] == settings.KEYCLOAK_CLIENT_SECRET, (
        "the realm's client secret and KEYCLOAK_CLIENT_SECRET have drifted apart; "
        "rotating one means editing both in the same commit"
    )


# ---------------------------------------------------------------------------
# Case 2 — assertion 4
# ---------------------------------------------------------------------------


def test_redirect_uri_points_at_the_app_not_keycloak(realm: dict, app_client: dict) -> None:
    """The callback is served by the application on 8000, not by Keycloak on 8080.

    Both ways of running the app land on 8000 — the `backend` service in
    docker-compose publishes `8000:8000`, and a host venv `uvicorn` binds the same
    port — so the port is stable regardless. The natural mistake is to reuse the
    Keycloak port that everything else in this file talks about.
    """
    redirect_uris = app_client.get("redirectUris", [])
    assert redirect_uris, "client declares no redirectUris; the callback will be rejected"

    parsed = [urlparse(uri) for uri in redirect_uris]

    assert any(p.path == CALLBACK_PATH and p.port == 8000 for p in parsed), (
        f"no redirectUri with path {CALLBACK_PATH} on port 8000; got {redirect_uris}"
    )
    # The logout round-trip. `app/services/auth.py:158` builds
    # post_logout_redirect_uri from url_for('login_page') -> /auth/login, and
    # Keycloak validates post-logout URIs against this same redirectUris list.
    # Without this entry logout dies on Keycloak's "Invalid redirect uri" page
    # while every other assertion in this file still passes — which is exactly
    # what happened on 2026-08-30 (fixed in dc377de). The assertion exists so
    # regenerating the realm file cannot silently put the bug back.
    assert any(p.path == LOGIN_PATH and p.port == 8000 for p in parsed), (
        f"no redirectUri with path {LOGIN_PATH} on port 8000; RP-initiated logout "
        f"will be rejected by Keycloak; got {redirect_uris}"
    )
    assert not any(p.port == KEYCLOAK_PORT for p in parsed), (
        f"a redirectUri points at Keycloak's own port {KEYCLOAK_PORT}; got {redirect_uris}"
    )
    # No wildcard — an open redirect on the client is a real vulnerability, not a
    # convenience, even in a demo realm (Forbidden item 7).
    assert "*" not in redirect_uris
    assert all("*" not in uri for uri in redirect_uris)

    assert app_client.get("webOrigins") == [APP_ORIGIN]


# ---------------------------------------------------------------------------
# Case 3 — assertion 5
# ---------------------------------------------------------------------------


def test_authorization_code_flow_is_the_only_flow_enabled(app_client: dict) -> None:
    """Authorization Code is the flow the app implements; the others are attack surface.

    `directAccessGrantsEnabled` in particular would let anyone exchange a username
    and password for a token straight from the realm, bypassing the app entirely —
    and the seeded passwords are committed in plaintext.
    """
    assert app_client["standardFlowEnabled"] is True
    assert app_client["implicitFlowEnabled"] is False
    assert app_client["directAccessGrantsEnabled"] is False
    assert app_client.get("serviceAccountsEnabled", False) is False


# ---------------------------------------------------------------------------
# Case 4 — assertions 6, 7
# ---------------------------------------------------------------------------


def test_realm_carries_no_authorization_information(realm: dict) -> None:
    """Keycloak proves identity; PostgreSQL decides access (CLAUDE.md §2, D40).

    The check is structural rather than key-by-key: the whole realm is serialised
    with documentation keys stripped, and the authorization-carrying key names must
    not appear at any depth. A role nested somewhere unexpected still fails.
    """
    realm_roles = realm.get("roles", {})
    assert not realm_roles.get("realm"), (
        f"realm declares realm roles: {realm_roles.get('realm')!r}. "
        "Nothing in the app reads them, but their presence invites the next person "
        "to make Keycloak authoritative again."
    )
    assert not realm_roles.get("client"), f"realm declares client roles: {realm_roles!r}"

    # Documentation keys (`_comment`) no longer live at realm level (Keycloak 24
    # rejects unknown top-level properties); the guard is unchanged regardless.
    blob = json.dumps(realm)

    for forbidden_key in ('"roles"', '"realmRoles"', '"clientRoles"', '"protocolMappers"'):
        assert forbidden_key not in blob, (
            f"{forbidden_key} appears in the realm file; this realm must carry no "
            "authorization information at all"
        )

    for user in realm.get("users", []):
        assert not user.get("realmRoles"), f"{user.get('username')} has realmRoles"
        assert not user.get("clientRoles"), f"{user.get('username')} has clientRoles"
        assert not user.get("groups"), f"{user.get('username')} has groups"


def test_every_seeded_user_has_a_pinned_id(realm: dict) -> None:
    """User ids are pinned so a re-import never regenerates them.

    Without an explicit `id`, every `--import-realm` into a fresh volume mints a
    NEW sub for each user. PostgreSQL rows are matched by keycloak_id, so every
    existing row is orphaned at once — login then fails on the email unique
    constraint with a 500 (observed live 2026-08-31 after two re-imports).
    Pinning the ids makes re-imports idempotent; the app-side email fallback in
    `_lazy_sync_user` is the second line of defence, not the fix.
    """
    users = realm.get("users", [])
    assert users, "realm declares no users"

    seen: set[str] = set()
    for user in users:
        uid = user.get("id")
        assert uid, (
            f"{user.get('username')} has no pinned id — a realm re-import will "
            "regenerate its sub and orphan the PostgreSQL row"
        )
        uuid.UUID(uid)  # raises on malformed ids
        assert uid not in seen, f"duplicate pinned id {uid}"
        seen.add(uid)


# ---------------------------------------------------------------------------
# Case 5 — assertion 8
# ---------------------------------------------------------------------------


def test_every_seeded_user_can_log_in_without_a_keycloak_screen(realm: dict) -> None:
    """No password update, no email verification, no consent — straight to the callback.

    A `temporary: true` credential or a leftover `requiredActions` entry produces a
    Keycloak-hosted form mid-login. The app never sees the callback, and the failure
    looks like a broken redirect rather than a seeding mistake.
    """
    users = realm.get("users", [])
    assert len(users) == EXPECTED_USER_COUNT, (
        f"expected {EXPECTED_USER_COUNT} seeded users, found {len(users)}"
    )

    # Realm-level screens, which no per-user setting can suppress.
    assert realm.get("verifyEmail", False) is False
    assert realm.get("registrationAllowed", False) is False

    for user in users:
        who = user.get("username")
        assert who, "a seeded user has no username"
        assert user["enabled"] is True, f"{who} is disabled"
        assert user.get("emailVerified") is True, f"{who} would hit email verification"
        assert not user.get("requiredActions"), (
            f"{who} has requiredActions {user.get('requiredActions')!r} — "
            "Keycloak will interrupt the login with a form"
        )

        credentials = user.get("credentials", [])
        assert len(credentials) == 1, f"{who} has {len(credentials)} credentials, expected 1"
        credential = credentials[0]
        assert credential["type"] == "password", f"{who} credential is not a password"
        assert credential["temporary"] is False, (
            f"{who} has a temporary password — Keycloak forces a reset screen"
        )
        assert credential.get("value"), f"{who} has an empty password"


# ---------------------------------------------------------------------------
# Case 6 — assertion 9
# ---------------------------------------------------------------------------


def test_seeded_emails_are_present_and_unique(realm: dict) -> None:
    """`User.email` is unique in PostgreSQL (§10), and lazy sync writes it on login.

    Two Keycloak accounts sharing an email means the second login raises an
    integrity error on a table the pipeline needs, at the worst possible moment.
    """
    emails = [user.get("email") for user in realm.get("users", [])]

    assert all(emails), f"a seeded user has no email: {emails}"
    assert len(set(emails)) == len(emails), f"duplicate emails among seeded users: {emails}"

    # Distinct usernames too — Keycloak enforces this itself, but a duplicate in
    # the file makes the import fail with a stack trace rather than a clear message.
    usernames = [user.get("username") for user in realm.get("users", [])]
    assert len(set(usernames)) == len(usernames), f"duplicate usernames: {usernames}"


# ---------------------------------------------------------------------------
# Case 7 — assertion 10
# ---------------------------------------------------------------------------


def test_compose_imports_the_realm_file_read_only(keycloak_service: dict) -> None:
    """The file only matters if it is mounted where the importer looks, read-only.

    Also checks the mount *source* exists on disk: docker creates an empty
    directory for a missing bind source, so a typo'd path starts Keycloak
    successfully with nothing to import.
    """
    command = keycloak_service.get("command", "")
    command_text = " ".join(command) if isinstance(command, list) else str(command)
    assert "--import-realm" in command_text, (
        f"keycloak command does not import the realm: {command_text!r}"
    )

    volumes = keycloak_service.get("volumes", [])
    assert volumes, "keycloak service declares no volumes"

    realm_mounts = [v for v in volumes if REALM_PATH.name in str(v)]
    assert len(realm_mounts) == 1, (
        f"expected exactly one mount for {REALM_PATH.name}, found {realm_mounts}"
    )

    source, target, *options = str(realm_mounts[0]).split(":")

    assert target.startswith(IMPORT_DIR), (
        f"realm mounted at {target!r}, which is outside {IMPORT_DIR} — the importer "
        "will not see it"
    )
    assert "ro" in options, f"realm mount is writable: {realm_mounts[0]!r}"

    resolved_source = (REPO_ROOT / source).resolve()
    assert resolved_source == REALM_PATH.resolve(), (
        f"mount source {resolved_source} is not the realm file under test ({REALM_PATH})"
    )
    assert resolved_source.is_file(), (
        f"mount source {resolved_source} does not exist; docker would mount an empty "
        "directory and the import would find nothing"
    )


# ---------------------------------------------------------------------------
# Beyond the contract's seven
# ---------------------------------------------------------------------------


def test_the_named_volume_survives_alongside_the_bind_mount(keycloak_service: dict) -> None:
    """Forbidden item 4 — `keycloak_data` must not be replaced by the bind mount.

    Both mounts coexist because docker resolves overlapping mounts by path depth,
    and the import path is deeper. Dropping the named volume would make Keycloak
    lose the realm's runtime state on every restart, which looks like the import
    misbehaving.
    """
    volumes = [str(v) for v in keycloak_service.get("volumes", [])]
    assert any(v.startswith("keycloak_data:") for v in volumes), (
        f"keycloak_data volume was removed: {volumes}"
    )


def test_the_plaintext_secret_warning_is_present() -> None:
    """The warning is the only thing standing between a demo secret and reuse.

    Asserted so that deleting it as noise fails a test instead of passing quietly.
    The warning lives in deploy/keycloak/README.md, not in the realm JSON:
    Keycloak 24's RealmRepresentation fails hard on unknown properties (a
    top-level `_comment` aborts --import-realm before the realm exists), so the
    JSON carries no comment key and never can.
    """
    readme = REALM_PATH.parent / "README.md"
    assert readme.is_file(), "deploy/keycloak/README.md missing — it carries the warning"
    lowered = readme.read_text(encoding="utf-8").lower()
    assert "plaintext" in lowered
    assert "rotat" in lowered, "the warning does not say what rotating the secret requires"
