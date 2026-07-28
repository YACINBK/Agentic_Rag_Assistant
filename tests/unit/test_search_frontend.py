"""Tests for search frontend templates (Module 4).

Same auth pattern as test_search_route.py: MockRedis session + TestClient.
Templates + CSS only — no Python under test except the routes serving them.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from app.api.error_handlers import register_error_handlers
from app.api.routes import search_router
from tests.conftest import MockRedis, make_user_session, serialize_user_session

SESSION_ID = "abc123"

_templates = Jinja2Templates(directory="app/templates")


def _make_app(redis: MockRedis) -> FastAPI:
    app = FastAPI()
    app.state.redis = redis
    register_error_handlers(app)
    app.include_router(search_router)
    return app


def _auth_client(redis: MockRedis | None = None) -> tuple[TestClient, MockRedis]:
    session = make_user_session()
    if redis is None:
        redis = MockRedis()
    redis._store[f"session:{SESSION_ID}"] = serialize_user_session(session)
    client = TestClient(_make_app(redis))
    client.cookies.set("session_id", SESSION_ID)
    return client, redis


class TestSearchPage:

    def test_search_page_has_form(self) -> None:
        client, _ = _auth_client()

        response = client.get("/search/", headers={"Accept": "text/html"})

        assert response.status_code == 200
        assert "<form" in response.text
        assert 'hx-post="/search/"' in response.text

    def test_search_page_has_results_target(self) -> None:
        client, _ = _auth_client()

        response = client.get("/search/", headers={"Accept": "text/html"})

        assert 'id="search-results"' in response.text

    def test_search_page_loads_search_css(self) -> None:
        client, _ = _auth_client()

        response = client.get("/search/", headers={"Accept": "text/html"})

        assert "search.css" in response.text


class TestSearchResultsPartial:

    def test_search_results_partial_has_sse(self) -> None:
        client, _ = _auth_client()

        response = client.post("/search/", data={"query": "test question"})

        assert response.status_code == 200
        assert "sse-connect" in response.text
        assert "qid=" in response.text

    def test_search_results_shows_user_query(self) -> None:
        client, _ = _auth_client()

        response = client.post("/search/", data={"query": "What is X?"})

        assert "What is X?" in response.text
        assert "message-user" in response.text


class TestMessageBubble:

    def test_message_bubble_renders_answer(self) -> None:
        html = _templates.get_template("components/message_bubble.html").render(
            answer="X is Y.", sources=["guide.pdf"]
        )

        assert "X is Y." in html
        assert "message-assistant" in html
        assert "guide.pdf" in html
