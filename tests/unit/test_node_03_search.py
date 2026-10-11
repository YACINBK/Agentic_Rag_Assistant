from __future__ import annotations

import pytest

from app.core.exceptions import RetrievalError
from app.core.settings import Settings
from app.pipeline.nodes.node_03_parallel_qdrant_search import QdrantSearchNode
from tests.conftest import MockEmbedder, MockVectorStore, make_chunk, make_post_classifier_state


class TestQdrantSearchNode:
    async def test_embeds_and_searches_with_role_filter(self) -> None:
        chunks = [make_chunk(), make_chunk()]
        embedder = MockEmbedder()
        store = MockVectorStore(results=chunks)
        settings = Settings()
        node = QdrantSearchNode(embedder=embedder, vector_store=store, settings=settings)
        state = make_post_classifier_state(
            rewritten_query="deployment process steps",
            user_role="developer",
        )

        result = await node.execute(state)

        assert len(result["retrieved_chunks"]) == 2
        assert len(embedder.calls) == 1
        assert len(store.search_calls) == 1
        assert store.search_calls[0]["allowed_roles"] == ["developer", "all"]

    async def test_empty_results_returns_empty_list(self) -> None:
        embedder = MockEmbedder()
        store = MockVectorStore(results=[])
        settings = Settings()
        node = QdrantSearchNode(embedder=embedder, vector_store=store, settings=settings)
        state = make_post_classifier_state(rewritten_query="nonexistent topic")

        result = await node.execute(state)

        assert result["retrieved_chunks"] == []

    async def test_missing_rewritten_query_raises_error(self) -> None:
        embedder = MockEmbedder()
        store = MockVectorStore()
        settings = Settings()
        node = QdrantSearchNode(embedder=embedder, vector_store=store, settings=settings)
        state = make_post_classifier_state()
        state.pop("rewritten_query", None)

        with pytest.raises(RetrievalError):
            await node.execute(state)

    async def test_preserves_existing_state_keys(self) -> None:
        embedder = MockEmbedder()
        store = MockVectorStore(results=[make_chunk()])
        settings = Settings()
        node = QdrantSearchNode(embedder=embedder, vector_store=store, settings=settings)
        state = make_post_classifier_state(
            rewritten_query="company policy",
            user_role="employee",
        )

        result = await node.execute(state)

        assert result["query"] == state["query"]
        assert result["rewritten_query"] == "company policy"
        assert result["classification"] == "SIMPLE_RAG"
        assert result["user_role"] == "employee"
        assert "retrieved_chunks" in result

    async def test_role_filter_includes_all(self) -> None:
        embedder = MockEmbedder()
        store = MockVectorStore(results=[make_chunk()])
        settings = Settings()
        node = QdrantSearchNode(embedder=embedder, vector_store=store, settings=settings)
        state = make_post_classifier_state(
            rewritten_query="test query",
            user_role="qa_engineer",
        )

        await node.execute(state)

        assert store.search_calls[0]["allowed_roles"] == ["qa_engineer", "all"]

    async def test_passes_admin_flag_true(self) -> None:
        """Case 6 / assertion 9 — the True path.

        Without this, a node hardcoding `user_is_admin=False` passes every other
        test in the suite: the False cases agree with it, and the admin filter
        tests in test_m0_privilege_dimension.py call the vector store directly,
        never routing through this node. The resulting bug is silent — admins
        simply stop seeing restricted chunks, with no error and no log line.
        """
        embedder = MockEmbedder()
        store = MockVectorStore(results=[make_chunk()])
        settings = Settings()
        node = QdrantSearchNode(embedder=embedder, vector_store=store, settings=settings)
        state = make_post_classifier_state(
            rewritten_query="test query",
            user_role="developer",
            user_is_admin=True,
        )

        await node.execute(state)

        assert store.search_calls[0]["user_is_admin"] is True

    async def test_passes_admin_flag_false(self) -> None:
        """Case 7 / assertion 9 — the False path."""
        embedder = MockEmbedder()
        store = MockVectorStore(results=[make_chunk()])
        settings = Settings()
        node = QdrantSearchNode(embedder=embedder, vector_store=store, settings=settings)
        state = make_post_classifier_state(
            rewritten_query="test query",
            user_role="developer",
            user_is_admin=False,
        )

        await node.execute(state)

        assert store.search_calls[0]["user_is_admin"] is False

    async def test_missing_admin_flag_raises(self) -> None:
        """Case 8 / assertion 10 — omission must fail loudly.

        `state.get("user_is_admin", False)` would pass every other test here.
        A default of False silently under-serves admins; a default of True leaks
        restricted content. Neither is detectable at runtime, so the only safe
        behaviour is to raise — and to raise before search() is reached.
        """
        embedder = MockEmbedder()
        store = MockVectorStore(results=[make_chunk()])
        settings = Settings()
        node = QdrantSearchNode(embedder=embedder, vector_store=store, settings=settings)
        state = make_post_classifier_state(rewritten_query="test query")
        del state["user_is_admin"]

        with pytest.raises(KeyError):
            await node.execute(state)

        assert store.search_calls == []


class TestRetryAccumulation:
    """The retry pass must widen the candidate pool, not replace it.

    Before this, node 05b broadened the query, re-entered node 03, and the new
    result list overwrote `retrieved_chunks` wholesale. A broadened search that
    returned worse candidates silently discarded better ones from the first
    attempt, and nothing anywhere could recover them — the gate then judged the
    second pass alone and could decline a question the first pass had already
    found evidence for.
    """

    @staticmethod
    def _node(results):
        return QdrantSearchNode(
            embedder=MockEmbedder(),
            vector_store=MockVectorStore(results=results),
            settings=Settings(),
        )

    async def test_first_pass_does_not_accumulate(self) -> None:
        """No retry yet: behaviour is exactly as before, so the common path is
        untouched."""
        stale = [make_chunk(chunk_id="stale", score=0.9)]
        fresh = [make_chunk(chunk_id="fresh", score=0.4)]
        state = make_post_classifier_state(
            rewritten_query="deployment steps", retrieved_chunks=stale
        )

        result = await self._node(fresh).execute(state)

        assert [c["chunk_id"] for c in result["retrieved_chunks"]] == ["fresh"]

    async def test_retry_pass_keeps_first_attempt_evidence(self) -> None:
        """The defect, stated directly: a worse second pass must not erase a
        better first one."""
        first = [make_chunk(chunk_id="good", score=0.95)]
        second = [make_chunk(chunk_id="weak", score=0.01)]
        state = make_post_classifier_state(
            rewritten_query="broadened query",
            retrieved_chunks=first,
            retry_attempted=True,
        )

        result = await self._node(second).execute(state)

        ids = [c["chunk_id"] for c in result["retrieved_chunks"]]
        assert "good" in ids, "first attempt's evidence was discarded by the retry"
        assert "weak" in ids
        assert len(ids) == 2

    async def test_retry_pass_dedupes_by_chunk_id(self) -> None:
        """Both passes legitimately return the same chunk; it must appear once, or
        it gets double weight in the generator's context."""
        shared = make_chunk(chunk_id="shared", score=0.5)
        state = make_post_classifier_state(
            rewritten_query="broadened query",
            retrieved_chunks=[shared, make_chunk(chunk_id="only-first", score=0.3)],
            retry_attempted=True,
        )

        result = await self._node([make_chunk(chunk_id="shared", score=0.8)]).execute(state)

        ids = [c["chunk_id"] for c in result["retrieved_chunks"]]
        assert ids.count("shared") == 1
        assert sorted(ids) == ["only-first", "shared"]

    async def test_retry_keeps_the_fresh_copy_of_a_duplicate(self) -> None:
        """The surviving copy is the one from this pass. Node 04 rescores the whole
        union against the broadened query anyway, so this only matters for which
        payload fields ride along — and the fresh search is the current truth."""
        state = make_post_classifier_state(
            rewritten_query="broadened query",
            retrieved_chunks=[make_chunk(chunk_id="dup", text="stale text")],
            retry_attempted=True,
        )

        result = await self._node([make_chunk(chunk_id="dup", text="fresh text")]).execute(state)

        assert [c["text"] for c in result["retrieved_chunks"]] == ["fresh text"]

    async def test_retry_with_no_prior_chunks_is_not_special_cased(self) -> None:
        """A retry after an empty first pass has nothing to merge and must not
        crash or invent entries."""
        state = make_post_classifier_state(
            rewritten_query="broadened query", retrieved_chunks=[], retry_attempted=True
        )

        result = await self._node([make_chunk(chunk_id="fresh")]).execute(state)

        assert [c["chunk_id"] for c in result["retrieved_chunks"]] == ["fresh"]

    async def test_retry_pass_still_applies_the_security_filter(self) -> None:
        """Regression guard: accumulation must not become a path that reuses
        chunks fetched under a different authorization context. Both passes query
        with this user's own role and privilege flag."""
        state = make_post_classifier_state(
            rewritten_query="broadened query",
            user_role="qa_engineer",
            user_is_admin=False,
            retrieved_chunks=[make_chunk(chunk_id="prior")],
            retry_attempted=True,
        )
        store = MockVectorStore(results=[make_chunk(chunk_id="fresh")])
        node = QdrantSearchNode(
            embedder=MockEmbedder(), vector_store=store, settings=Settings()
        )

        await node.execute(state)

        assert store.search_calls[0]["allowed_roles"] == ["qa_engineer", "all"]
        assert store.search_calls[0]["user_is_admin"] is False
