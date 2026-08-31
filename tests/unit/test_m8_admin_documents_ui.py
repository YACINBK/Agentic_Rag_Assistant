"""M8 admin documents UI — template-only unit tests (no database).

Assertions 1-4 and 6 are PostgreSQL facts (route branching + real ordering) and
live in tests/integration/test_m8_admin_documents_ui.py. Assertions 5, 7, 8, 9,
10 are template/layout facts and need no database — they live here, following
test_search_frontend.py's direct-template-render shape (TestMessageBubble) and
test_frontend_base_pages.py's TestClient + MockRedis shape.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import Depends, FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from app.api.dependencies import require_auth
from app.core.security import UserSession
from tests.conftest import MockRedis, make_user_session, serialize_user_session

_templates = Jinja2Templates(directory="app/templates")


def _make_document(**overrides) -> SimpleNamespace:
    defaults = dict(
        original_filename="askgo.html",
        ingestion_status="pending",
        category="technical",
        restricted=False,
        chunk_count=0,
        last_ingested_at=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestDocumentListPartial:
    """T1 — every status reaches the rendering (Assertion 5)."""

    def test_every_status_and_filename_appears(self) -> None:
        documents = [
            _make_document(original_filename="a.html", ingestion_status="pending"),
            _make_document(original_filename="b.html", ingestion_status="running"),
            _make_document(original_filename="c.html", ingestion_status="done"),
            _make_document(original_filename="d.html", ingestion_status="failed"),
        ]

        html = _templates.get_template("partials/document_list.html").render(
            documents=documents, poll=True, poll_interval=3
        )

        for status in ("pending", "running", "done", "failed"):
            assert status in html
        for filename in ("a.html", "b.html", "c.html", "d.html"):
            assert filename in html

    def test_empty_state_renders_explicit_message(self) -> None:
        html = _templates.get_template("partials/document_list.html").render(
            documents=[], poll=False, poll_interval=3
        )

        assert "No documents" in html
        # M8c amendment: "idle" is no longer "no hx-trigger at all" — the event
        # refresh trigger is always present. Idle means: no polling trigger.
        assert "every" not in html
        assert "document-changed from:body" in html


class TestDocumentListPolling:
    """T2 — polling is conditional (Assertion 7)."""

    def test_poll_false_omits_hx_trigger(self) -> None:
        documents = [_make_document(ingestion_status="done")]

        html = _templates.get_template("partials/document_list.html").render(
            documents=documents, poll=False, poll_interval=3
        )

        # M8c amendment: the polling trigger is conditional; the event refresh
        # trigger is not. Asserting both sides keeps a dead refresh path from
        # passing as "idle".
        assert "every" not in html
        assert "document-changed from:body" in html

    def test_poll_true_includes_hx_trigger_every_3s(self) -> None:
        documents = [_make_document(ingestion_status="running")]

        html = _templates.get_template("partials/document_list.html").render(
            documents=documents, poll=True, poll_interval=3
        )

        assert "hx-trigger" in html
        assert "every 3s" in html


class TestUploadFormShape:
    """T3 — upload form shape (Assertion 8)."""

    def test_upload_form_has_exactly_the_scoping_controls(self) -> None:
        html = _templates.get_template("components/upload_form.html").render()

        assert 'name="file"' in html
        assert 'name="category"' in html
        assert 'name="restricted"' in html
        assert 'name="allowed_roles"' not in html

    def test_upload_form_posts_to_admin_documents(self) -> None:
        html = _templates.get_template("components/upload_form.html").render()

        assert 'hx-post="/admin/documents"' in html

    def test_upload_form_swaps_the_document_list(self) -> None:
        """D28 — the response must land in the DOM, not be discarded.

        The original `hx-swap="none"` is what made a fresh upload invisible: the
        route's 202 went nowhere, so the list stayed as rendered — without a
        poll trigger, since nothing was in flight at page-load time — and only a
        manual reload showed the new row. Pinning the target here because a
        revert to `none` is silent: every other M8 test still passes with it.
        """
        html = _templates.get_template("components/upload_form.html").render()

        assert 'hx-target="#document-list"' in html
        assert 'hx-swap="outerHTML"' in html
        assert 'hx-swap="none"' not in html


class TestBaseLayoutAdminWiring:
    """T4 — layout wiring (Assertions 9, 10)."""

    def _make_app(self, redis: MockRedis) -> FastAPI:
        app = FastAPI()
        app.state.redis = redis

        @app.get("/")
        async def index(request: Request, user: UserSession = Depends(require_auth)):
            return _templates.TemplateResponse(request, "pages/dashboard.html", {"user": user})

        return app

    def _authenticated_client(self, session: UserSession) -> TestClient:
        redis = MockRedis({"session:abc123": serialize_user_session(session)})
        client = TestClient(self._make_app(redis))
        client.cookies.set("session_id", "abc123")
        return client

    def test_admin_sees_csrf_script_and_documents_link(self) -> None:
        client = self._authenticated_client(make_user_session(is_admin=True))

        response = client.get("/", headers={"Accept": "text/html"})

        assert response.status_code == 200
        assert "/static/js/csrf.js" in response.text
        assert 'href="/admin/documents"' in response.text

    def test_non_admin_sees_csrf_script_but_no_documents_link(self) -> None:
        client = self._authenticated_client(make_user_session(is_admin=False))

        response = client.get("/", headers={"Accept": "text/html"})

        assert response.status_code == 200
        assert "/static/js/csrf.js" in response.text
        assert 'href="/admin/documents"' not in response.text
