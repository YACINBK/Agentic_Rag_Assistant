"""M9 admin users UI — template, nav-gating, and session-index unit tests (no DB).

Split follows M8: PostgreSQL/route-branching facts (assertions 1-6, 9, 10) live in
tests/integration/test_m9_admin_users_ui.py. What lands here is what needs no
database — the row/list templates (T1), the owner-gated nav link (T8), and
app/services/sessions.py, which talks only to Redis and is therefore fully
exercisable against MockRedis.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from app.api.dependencies import require_auth
from app.core.security import UserSession
from app.services.sessions import (
    USER_SESSIONS_PREFIX,
    purge_user_sessions,
    register_session,
    unregister_session,
    user_sessions_key,
)
from tests.conftest import MockRedis, make_user_session, serialize_user_session

_templates = Jinja2Templates(directory="app/templates")


def _make_user(**overrides) -> SimpleNamespace:
    """A stand-in for a User row with its `role` relationship loaded.

    SimpleNamespace rather than the ORM class, following M8's `_make_document`:
    the templates read plain attributes, and constructing ORM rows here would
    couple a template test to the mapper.
    """
    defaults = dict(
        id=uuid.uuid4(),
        email="user@whitecape.fr",
        is_admin=False,
        is_owner=False,
        role=SimpleNamespace(name="developer"),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestUserListPartial:
    """T1 — every user's email, role name, and admin state reaches the render."""

    def _three_users(self) -> list[SimpleNamespace]:
        return [
            _make_user(
                email="owner@whitecape.fr",
                is_admin=True,
                is_owner=True,
                role=SimpleNamespace(name="project_manager"),
            ),
            _make_user(
                email="admin@whitecape.fr",
                is_admin=True,
                role=SimpleNamespace(name="qa_engineer"),
            ),
            _make_user(
                email="plain@whitecape.fr",
                is_admin=False,
                role=SimpleNamespace(name="developer"),
            ),
        ]

    def test_every_email_and_role_name_appears(self) -> None:
        users = self._three_users()

        html = _templates.get_template("partials/user_list.html").render(users=users)

        for email in ("owner@whitecape.fr", "admin@whitecape.fr", "plain@whitecape.fr"):
            assert email in html
        # Role names come from the Role table, so all three distinct values must
        # render — a hardcoded name would show up as one repeated value.
        for role_name in ("project_manager", "qa_engineer", "developer"):
            assert role_name in html

    def test_owner_row_carries_no_revoke_control(self) -> None:
        users = self._three_users()
        owner, admin, plain = users

        html = _templates.get_template("partials/user_list.html").render(users=users)

        # The Owner's row offers nothing: is_owner is seeded, and revoking
        # is_admin would break ck_owner_implies_admin (Forbidden 1).
        assert f"/admin/users/{owner.id}/admin" not in html
        # The plain admin's row does offer revoke, so the assertion above is not
        # passing vacuously off a template that renders no controls at all.
        assert f'hx-delete="/admin/users/{admin.id}/admin"' in html
        assert f'hx-post="/admin/users/{plain.id}/admin"' in html

    def test_non_admin_row_offers_grant_not_revoke(self) -> None:
        plain = _make_user(email="plain@whitecape.fr", is_admin=False)

        html = _templates.get_template("partials/user_row.html").render(row_user=plain)

        assert f'hx-post="/admin/users/{plain.id}/admin"' in html
        assert "hx-delete" not in html

    def test_admin_row_offers_revoke_not_grant(self) -> None:
        admin = _make_user(email="admin@whitecape.fr", is_admin=True)

        html = _templates.get_template("partials/user_row.html").render(row_user=admin)

        assert f'hx-delete="/admin/users/{admin.id}/admin"' in html
        assert "hx-post" not in html


class TestNavLinkOwnerGating:
    """T8 — the /admin/users link is owner-gated, and M8's link does not regress."""

    def _authenticated_client(self, session: UserSession) -> TestClient:
        redis = MockRedis({"session:abc123": serialize_user_session(session)})
        app = FastAPI()
        app.state.redis = redis

        @app.get("/")
        async def index(request: Request, user: UserSession = Depends(require_auth)):
            return _templates.TemplateResponse(request, "pages/dashboard.html", {"user": user})

        client = TestClient(app)
        client.cookies.set("session_id", "abc123")
        return client

    def test_owner_sees_both_admin_links(self) -> None:
        client = self._authenticated_client(make_user_session(is_admin=True, is_owner=True))

        response = client.get("/", headers={"Accept": "text/html"})

        assert response.status_code == 200
        assert 'href="/admin/users"' in response.text
        assert 'href="/admin/documents"' in response.text

    def test_admin_not_owner_sees_documents_but_not_users(self) -> None:
        client = self._authenticated_client(make_user_session(is_admin=True, is_owner=False))

        response = client.get("/", headers={"Accept": "text/html"})

        assert response.status_code == 200
        assert 'href="/admin/users"' not in response.text
        # M8 assertion 10 must not regress now that a second link shares the block.
        assert 'href="/admin/documents"' in response.text

    def test_non_admin_sees_neither(self) -> None:
        client = self._authenticated_client(make_user_session(is_admin=False, is_owner=False))

        response = client.get("/", headers={"Accept": "text/html"})

        assert response.status_code == 200
        assert 'href="/admin/users"' not in response.text
        assert 'href="/admin/documents"' not in response.text

    def test_users_link_is_not_boosted(self) -> None:
        """A boosted link sends HX-Request: true, which would return the bare
        partial for a full navigation — the same trap M8's link had to avoid."""
        client = self._authenticated_client(make_user_session(is_admin=True, is_owner=True))

        response = client.get("/", headers={"Accept": "text/html"})

        link = response.text.split('href="/admin/users"')[1].split(">")[0]
        assert 'hx-boost="false"' in link


class TestSessionKeyPrefixes:
    """The session-key literal is defined in three modules and cannot be shared
    (app/services/auth.py imports sessions.py, so sessions.py cannot import back).
    Drift would make the purge silently delete nothing — D22 reopened with no
    symptom. This test is what makes that loud."""

    def test_all_three_session_prefixes_agree(self) -> None:
        from app.api.dependencies import SESSION_PREFIX as dependencies_prefix
        from app.services.auth import SESSION_PREFIX as auth_prefix
        from app.services.sessions import SESSION_PREFIX as sessions_prefix

        assert sessions_prefix == auth_prefix == dependencies_prefix

    def test_user_sessions_key_is_prefixed_once(self) -> None:
        assert user_sessions_key("abc") == f"{USER_SESSIONS_PREFIX}abc"


class TestSessionIndex:
    """app/services/sessions.py against MockRedis — the reverse index itself."""

    @pytest.mark.asyncio
    async def test_register_adds_member_and_sets_ttl(self) -> None:
        redis = MockRedis()

        await register_session(redis, "user-1", "sid-1", 86400)

        assert await redis.smembers(user_sessions_key("user-1")) == {"sid-1"}
        assert redis._ttls[user_sessions_key("user-1")] == 86400

    @pytest.mark.asyncio
    async def test_register_is_idempotent_and_accumulates(self) -> None:
        redis = MockRedis()

        await register_session(redis, "user-1", "sid-1", 60)
        await register_session(redis, "user-1", "sid-1", 60)
        await register_session(redis, "user-1", "sid-2", 60)

        assert await redis.smembers(user_sessions_key("user-1")) == {"sid-1", "sid-2"}

    @pytest.mark.asyncio
    async def test_unregister_removes_only_that_member(self) -> None:
        redis = MockRedis()
        await register_session(redis, "user-1", "sid-1", 60)
        await register_session(redis, "user-1", "sid-2", 60)

        await unregister_session(redis, "user-1", "sid-1")

        assert await redis.smembers(user_sessions_key("user-1")) == {"sid-2"}

    @pytest.mark.asyncio
    async def test_unregister_missing_member_is_a_no_op(self) -> None:
        redis = MockRedis()
        await register_session(redis, "user-1", "sid-1", 60)

        await unregister_session(redis, "user-1", "never-existed")
        await unregister_session(redis, "user-unknown", "sid-1")

        assert await redis.smembers(user_sessions_key("user-1")) == {"sid-1"}

    @pytest.mark.asyncio
    async def test_purge_deletes_every_session_key_and_the_set(self) -> None:
        redis = MockRedis()
        redis._store["session:sid-1"] = serialize_user_session(make_user_session())
        redis._store["session:sid-2"] = serialize_user_session(make_user_session())
        redis._store["session:other"] = serialize_user_session(make_user_session())
        await register_session(redis, "user-1", "sid-1", 60)
        await register_session(redis, "user-1", "sid-2", 60)
        await register_session(redis, "user-2", "other", 60)

        deleted = await purge_user_sessions(redis, "user-1")

        assert deleted == 2
        assert "session:sid-1" not in redis._store
        assert "session:sid-2" not in redis._store
        # A second user's session is untouched — the purge is keyed on one id.
        assert "session:other" in redis._store
        assert await redis.smembers(user_sessions_key("user-1")) == set()
        assert await redis.smembers(user_sessions_key("user-2")) == {"other"}

    @pytest.mark.asyncio
    async def test_purge_counts_only_keys_that_existed(self) -> None:
        """A member whose session key already expired was not deleted by us, so
        it is not counted — the return value is a deletion count, not a set size."""
        redis = MockRedis()
        redis._store["session:live"] = serialize_user_session(make_user_session())
        await register_session(redis, "user-1", "live", 60)
        await register_session(redis, "user-1", "already-expired", 60)

        deleted = await purge_user_sessions(redis, "user-1")

        assert deleted == 1

    @pytest.mark.asyncio
    async def test_purge_of_unknown_user_returns_zero(self) -> None:
        redis = MockRedis()
        redis._store["session:sid-1"] = serialize_user_session(make_user_session())

        deleted = await purge_user_sessions(redis, "no-such-user")

        assert deleted == 0
        assert "session:sid-1" in redis._store
