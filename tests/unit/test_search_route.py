"""Tests for the search routes: page, submit, and SSE stream.

Pipeline is mocked via patch on app.api.routes.search.get_compiled_pipeline.
Auth is real: require_auth + MockRedis session. Templates render for real.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.error_handlers import register_error_handlers
from app.api.routes import search_router
from app.core.exceptions import RetrievalError
from tests.conftest import MockRedis, make_chunk, make_user_session, serialize_user_session

SESSION_ID = "abc123"


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


def _mock_pipeline(
    result: dict | None = None,
    error: Exception | None = None,
    nodes: tuple[str, ...] = ("cache_check", "classifier"),
) -> MagicMock:
    """Stand-in compiled graph shaped like `astream(stream_mode=["debug","values"])`.

    The route streams rather than `ainvoke`s so the progress line can advance while
    the pipeline runs — a single `await` cannot yield. LangGraph's tuple shape is
    reproduced exactly: ("debug", {"type": "task", "payload": {"name": <node>}}) at
    each node start, then ("values", <accumulated state>). The route keeps the last
    `values` payload, so yielding it once at the end is equivalent to what
    `ainvoke` returned.

    `nodes` drives which stage labels the route should emit; `error` is raised from
    inside the generator so it surfaces through the route's `async for`, which is
    where a real pipeline failure would land.
    """
    graph = MagicMock()

    async def fake_astream(state, stream_mode=None):
        if error is not None:
            raise error
        for name in nodes:
            yield "debug", {"type": "task", "payload": {"name": name}}
        yield "values", result or {}

    graph.astream = fake_astream
    return graph


class TestSearchPage:

    def test_search_page_renders(self) -> None:
        client, _ = _auth_client()

        response = client.get("/search/", headers={"Accept": "text/html"})

        assert response.status_code == 200
        assert "<form" in response.text

    def test_search_page_requires_auth(self) -> None:
        client = TestClient(_make_app(MockRedis()), follow_redirects=False)

        response = client.get("/search/", headers={"Accept": "text/html"})

        assert response.status_code == 303
        assert "/auth/login" in response.headers["location"]


class TestSearchSubmit:

    def test_search_post_stores_query(self) -> None:
        client, redis = _auth_client()

        response = client.post("/search/", data={"query": "What is Whitecape?"})

        assert response.status_code == 200
        assert "sse-connect" in response.text

        query_keys = [k for k in redis._store if k.startswith("query:")]
        assert len(query_keys) == 1
        assert redis._store[query_keys[0]] == "What is Whitecape?"

    def test_search_post_empty_query(self) -> None:
        client, _ = _auth_client()

        response = client.post("/search/", data={"query": "   "})

        assert response.status_code == 400


class TestSearchStream:

    def test_sse_stream_faithful_answer(self) -> None:
        client, redis = _auth_client()
        redis._store["query:qid-1"] = "What is the leave policy?"

        result = {
            "generated_answer": "The answer",
            "is_faithful": True,
            "reranked_chunks": [make_chunk(original_filename="handbook.pdf")],
        }
        with patch(
            "app.api.routes.search.get_compiled_pipeline",
            return_value=_mock_pipeline(result=result),
        ):
            response = client.get("/search/stream?qid=qid-1")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: answer" in response.text
        assert "The answer" in response.text

    def test_sse_stream_unfaithful_answer(self) -> None:
        client, redis = _auth_client()
        redis._store["query:qid-2"] = "What is the leave policy?"

        result = {"generated_answer": "Hallucinated nonsense", "is_faithful": False}
        with patch(
            "app.api.routes.search.get_compiled_pipeline",
            return_value=_mock_pipeline(result=result),
        ):
            response = client.get("/search/stream?qid=qid-2")

        assert "event: error" in response.text
        assert "event: answer" not in response.text

    def test_sse_stream_pipeline_error(self) -> None:
        client, redis = _auth_client()
        redis._store["query:qid-3"] = "What is the leave policy?"

        with patch(
            "app.api.routes.search.get_compiled_pipeline",
            return_value=_mock_pipeline(error=RetrievalError("Qdrant unavailable")),
        ):
            response = client.get("/search/stream?qid=qid-3")

        assert "event: error" in response.text
        assert "Qdrant unavailable" in response.text

    def test_sse_stream_emits_a_progress_label_per_pipeline_stage(self) -> None:
        """The stage line must advance as the pipeline runs, not sit static.

        Before this, the route awaited `ainvoke` — one await, which cannot yield —
        so a single "Searching documents…" frame covered the whole 2–4s+ run. Here
        four nodes start, so four distinct stage labels must reach the client, in
        node order, each naming the stage that is running.

        Pinned because a revert to `ainvoke` is otherwise silent: every other SSE
        test still passes with one progress frame.
        """
        client, redis = _auth_client()
        redis._store["query:qid-5"] = "What is the leave policy?"

        result = {"generated_answer": "The answer", "is_faithful": True, "reranked_chunks": []}
        with patch(
            "app.api.routes.search.get_compiled_pipeline",
            return_value=_mock_pipeline(
                result=result,
                nodes=("classifier", "qdrant_search", "generator", "faithfulness"),
            ),
        ):
            response = client.get("/search/stream?qid=qid-5")

        expected = [
            "Understanding the question",
            "Searching indexed documents",
            "Composing the answer",
            "Verifying every claim against the sources",
        ]
        positions = [response.text.find(label) for label in expected]
        missing = [label for label, pos in zip(expected, positions) if pos == -1]
        assert not missing, f"missing stage labels: {missing}"
        # Node order, not just presence — a set-membership check would pass on a
        # scrambled or coincidental ordering.
        assert positions == sorted(positions)

        # The answer is still one buffered event after the stages (§12).
        assert response.text.count("event: answer") == 1
        assert response.text.find("event: answer") > max(positions)

    def test_sse_stream_ends_with_done(self) -> None:
        client, redis = _auth_client()
        redis._store["query:qid-4"] = "What is the leave policy?"

        result = {"generated_answer": "The answer", "is_faithful": True, "reranked_chunks": []}
        with patch(
            "app.api.routes.search.get_compiled_pipeline",
            return_value=_mock_pipeline(result=result),
        ):
            response = client.get("/search/stream?qid=qid-4")

        events = [line for line in response.text.splitlines() if line.startswith("event:")]
        assert len(events) > 0
        assert events[-1] == "event: done"
