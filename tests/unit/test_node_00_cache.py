from __future__ import annotations

from app.core.settings import Settings
from app.pipeline.nodes.node_00_cache_check import CacheCheckNode
from tests.conftest import MockEmbedder, MockVectorStore, make_chunk, make_state


class TestCacheCheckNode:

    async def test_cache_hit_returns_cached_answer(self) -> None:
        embedder = MockEmbedder()
        hit = make_chunk(score=0.95, text="cached answer text")
        store = MockVectorStore(results=[hit])
        settings = Settings()
        node = CacheCheckNode(embedder=embedder, vector_store=store, settings=settings)
        state = make_state(query="How do I reset my VPN?")

        result = await node.execute(state)

        assert result["cache_hit"] is True
        assert result["cached_answer"] == "cached answer text"
        assert len(embedder.calls) == 1

    async def test_cache_miss_when_score_below_threshold(self) -> None:
        embedder = MockEmbedder()
        low = make_chunk(score=0.5, text="low score answer")
        store = MockVectorStore(results=[low])
        settings = Settings(CACHE_SIMILARITY_THRESHOLD=0.92)
        node = CacheCheckNode(embedder=embedder, vector_store=store, settings=settings)
        state = make_state(query="some query")

        result = await node.execute(state)

        assert result["cache_hit"] is False
        assert "cached_answer" not in result

    async def test_cache_miss_when_no_results(self) -> None:
        embedder = MockEmbedder()
        store = MockVectorStore(results=[])
        settings = Settings()
        node = CacheCheckNode(embedder=embedder, vector_store=store, settings=settings)
        state = make_state()

        result = await node.execute(state)

        assert result["cache_hit"] is False

    async def test_embedder_failure_returns_miss(self) -> None:
        embedder = MockEmbedder()

        async def failing_embed(text):
            embedder.calls.append([text])
            raise RuntimeError("embedding failed")

        embedder.embed_single = failing_embed
        store = MockVectorStore()
        settings = Settings()
        node = CacheCheckNode(embedder=embedder, vector_store=store, settings=settings)
        state = make_state()

        result = await node.execute(state)

        assert result["cache_hit"] is False

    async def test_role_scoped_cache_search(self) -> None:
        embedder = MockEmbedder()
        store = MockVectorStore(results=[make_chunk(score=0.95)])
        settings = Settings()
        node = CacheCheckNode(embedder=embedder, vector_store=store, settings=settings)
        state = make_state(query="test", user_role="developer")

        await node.execute(state)

        assert store.search_calls[0]["allowed_roles"] == ["developer"]
        assert store.search_calls[0]["collection"] == "semantic_cache"
