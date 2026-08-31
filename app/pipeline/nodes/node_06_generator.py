from __future__ import annotations

import re

from app.core.base_node import BaseNode
from app.core.exceptions import GenerationError
from app.core.services.llm import BaseLLMService
from app.core.settings import Settings
from app.core.state import ChunkPayload, Citation, PipelineState

_ROLE_PERSONA_BASE = "You are a knowledgeable assistant for Whitecape Technologies. You answer questions from a {role} based ONLY on the provided document excerpts."

_SYSTEM_PROMPT = """{role_persona}

Rules:
- Answer ONLY using the information in the provided chunks. Do not use outside knowledge.
- Cite sources inline using [N] format, where N is the number of the excerpt that supports the fact (e.g. [1], [2]). Place the marker right after the fact it supports.
- Only cite excerpt numbers that were actually provided to you. Never invent a number.
- If the chunks do not contain enough information to answer, say so honestly.
- Keep answers concise and professional.
- Never fabricate information. If you don't know, say you don't know."""

_FALLBACK_ANSWER = (
    "I could not find relevant information in the available documents to answer your question. "
    "Please try rephrasing or contact your team lead for assistance."
)

# Matches a citation marker like [1], [12] — a bracketed run of digits.
_MARKER_RE = re.compile(r"\[(\d+)\]")


class GeneratorNode(BaseNode):
    def __init__(self, llm: BaseLLMService, settings: Settings) -> None:
        self._llm = llm
        self._settings = settings

    @property
    def name(self) -> str:
        return "generator"

    async def execute(self, state: PipelineState) -> PipelineState:
        query = state["query"]
        user_role = state["user_role"]
        chunks: list[ChunkPayload] = state.get("reranked_chunks", [])

        if not chunks:
            return {**state, "generated_answer": _FALLBACK_ANSWER, "citations": []}

        role_persona = _ROLE_PERSONA_BASE.format(role=user_role)
        system = _SYSTEM_PROMPT.format(role_persona=role_persona)

        user_message = self._build_user_message(query, chunks)

        try:
            raw = await self._llm.complete(
                model=self._settings.GENERATOR_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.0,
            )
        except Exception as e:
            raise GenerationError(f"Generator LLM call failed: {e}") from e

        answer = raw.strip()
        if not answer:
            return {**state, "generated_answer": _FALLBACK_ANSWER, "citations": []}

        citations = self._build_citations(answer, chunks)
        return {**state, "generated_answer": answer, "citations": citations}

    @staticmethod
    def _build_user_message(query: str, chunks: list[ChunkPayload]) -> str:
        parts = [f"Question: {query}\n\nRelevant document excerpts:\n"]
        for i, chunk in enumerate(chunks, 1):
            filename = chunk.get("original_filename", "unknown")
            parts.append(f"[{i}] {chunk['text']}  (source: {filename})")
        return "\n".join(parts)

    @staticmethod
    def _build_citations(answer: str, chunks: list[ChunkPayload]) -> list[Citation]:
        """Resolve each distinct, in-range [N] marker to exactly one Citation.

        - distinct: a chunk cited twice yields one entry (the map is injective).
        - in-range: a marker outside 1..len(chunks) is a hallucination — it is
          excluded and left verbatim in the answer (not rewritten here).
        - ordered by index ascending.
        Fields absent on pre-ingestion chunks default to ""/"" /[].
        """
        seen: set[int] = set()
        citations: list[Citation] = []
        for match in _MARKER_RE.finditer(answer):
            index = int(match.group(1))
            if index in seen:
                continue
            if index < 1 or index > len(chunks):
                continue
            seen.add(index)
            chunk = chunks[index - 1]
            citations.append(
                Citation(
                    index=index,
                    chunk_id=chunk["chunk_id"],
                    document_id=chunk["document_id"],
                    text=chunk["text"],
                    section_path=chunk.get("section_path", ""),
                    anchor=chunk.get("anchor", ""),
                    image_refs=chunk.get("image_refs", []),
                    original_filename=chunk.get("original_filename", ""),
                )
            )
        citations.sort(key=lambda c: c["index"])
        return citations
