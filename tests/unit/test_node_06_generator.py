from __future__ import annotations

import pytest

from app.core.exceptions import GenerationError
from app.core.settings import Settings
from app.pipeline.nodes.node_06_generator import GeneratorNode
from tests.conftest import MockLLMService, make_chunk, make_post_rerank_state


class TestGeneratorNode:
    async def test_generates_cited_answer_from_chunks(self) -> None:
        llm = MockLLMService(response="According to company policy, leave is 25 days [1].")
        settings = Settings()
        node = GeneratorNode(llm=llm, settings=settings)
        state = make_post_rerank_state(num_chunks=2)

        result = await node.execute(state)

        assert isinstance(result["generated_answer"], str)
        assert len(result["generated_answer"]) > 0
        assert len(llm.calls) == 1
        assert llm.calls[0]["model"] == settings.GENERATOR_MODEL

    async def test_empty_chunks_produces_fallback_answer(self) -> None:
        llm = MockLLMService()
        settings = Settings()
        node = GeneratorNode(llm=llm, settings=settings)
        state = make_post_rerank_state(reranked_chunks=[])

        result = await node.execute(state)

        assert isinstance(result["generated_answer"], str)
        assert len(result["generated_answer"]) > 0
        assert len(llm.calls) == 0
        assert result["citations"] == []

    async def test_llm_failure_raises_generation_error(self) -> None:
        llm = MockLLMService(response="irrelevant")

        async def failing_complete(model, messages, temperature=0.0):
            llm.calls.append({"model": model, "messages": messages, "temperature": temperature})
            raise RuntimeError("Ollama down")

        llm.complete = failing_complete
        settings = Settings()
        node = GeneratorNode(llm=llm, settings=settings)
        state = make_post_rerank_state(num_chunks=1)

        with pytest.raises(GenerationError):
            await node.execute(state)

    async def test_prompt_includes_role_and_chunks(self) -> None:
        llm = MockLLMService(response="Answer text [1].")
        settings = Settings()
        node = GeneratorNode(llm=llm, settings=settings)
        state = make_post_rerank_state(num_chunks=2, user_role="developer")

        await node.execute(state)

        system_msg = llm.calls[0]["messages"][0]["content"]
        user_msg = llm.calls[0]["messages"][1]["content"]
        assert "developer" in system_msg
        assert "[N]" in system_msg
        assert state["query"] in user_msg
        assert "Sample chunk text" not in user_msg
        assert "Reranked chunk" in user_msg
        assert "[1]" in user_msg
        assert "[2]" in user_msg

    async def test_preserves_existing_state_keys(self) -> None:
        llm = MockLLMService(response="Answer [1].")
        settings = Settings()
        node = GeneratorNode(llm=llm, settings=settings)
        state = make_post_rerank_state(num_chunks=1)

        result = await node.execute(state)

        assert result["query"] == state["query"]
        assert result["rewritten_query"] == state["rewritten_query"]
        assert result["classification"] == state["classification"]
        assert "generated_answer" in result
        assert "citations" in result

    async def test_citations_resolve_to_cited_chunks_including_repeats(self) -> None:
        chunks = [
            make_chunk(
                chunk_id=f"chunk-{i}",
                text=f"Distinct text {i}.",
                section_path=f"Chapter {i} > Page {i}",
                anchor=f"bkmrk-{i}",
                image_refs=[{"image_id": f"img-{i}", "anchor": f"bkmrk-{i}"}],
            )
            for i in range(1, 4)
        ]
        llm = MockLLMService(response="Ouvrez le menu [1]. Puis validez [3]. Enfin revérifiez [1].")
        settings = Settings()
        node = GeneratorNode(llm=llm, settings=settings)
        state = make_post_rerank_state(reranked_chunks=chunks)

        result = await node.execute(state)
        citations = result["citations"]

        assert [c["index"] for c in citations] == [1, 3]

        first = citations[0]
        assert first["chunk_id"] == chunks[0]["chunk_id"]
        assert first["document_id"] == chunks[0]["document_id"]
        assert first["text"] == chunks[0]["text"]
        assert first["anchor"] == chunks[0]["anchor"]
        assert first["image_refs"] == chunks[0]["image_refs"]

        third = citations[1]
        assert third["chunk_id"] == chunks[2]["chunk_id"]
        assert third["document_id"] == chunks[2]["document_id"]
        assert third["text"] == chunks[2]["text"]
        assert third["anchor"] == chunks[2]["anchor"]
        assert third["image_refs"] == chunks[2]["image_refs"]

    async def test_hallucinated_marker_is_excluded_but_left_in_text(self) -> None:
        chunks = [make_chunk(chunk_id="chunk-1"), make_chunk(chunk_id="chunk-2")]
        llm = MockLLMService(response="Voir [1] et aussi [7].")
        settings = Settings()
        node = GeneratorNode(llm=llm, settings=settings)
        state = make_post_rerank_state(reranked_chunks=chunks)

        result = await node.execute(state)
        citations = result["citations"]

        assert [c["index"] for c in citations] == [1]
        assert "[7]" in result["generated_answer"]

    async def test_missing_anchor_fields_still_produce_citation(self) -> None:
        chunk = make_chunk(chunk_id="chunk-1", original_filename="askgo-621x-fr.html")
        assert "anchor" not in chunk
        assert "section_path" not in chunk
        assert "image_refs" not in chunk

        llm = MockLLMService(response="Réponse [1].")
        settings = Settings()
        node = GeneratorNode(llm=llm, settings=settings)
        state = make_post_rerank_state(reranked_chunks=[chunk])

        result = await node.execute(state)
        citations = result["citations"]

        assert len(citations) == 1
        assert citations[0]["anchor"] == ""
        assert citations[0]["section_path"] == ""
        assert citations[0]["image_refs"] == []
        assert citations[0]["text"] == chunk["text"]
        assert citations[0]["original_filename"] == "askgo-621x-fr.html"
