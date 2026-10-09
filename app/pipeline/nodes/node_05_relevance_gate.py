from __future__ import annotations

from app.core.base_node import BaseNode
from app.core.settings import Settings
from app.core.state import ChunkPayload, PipelineState


class RelevanceGateNode(BaseNode):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def name(self) -> str:
        return "relevance_gate"

    async def execute(self, state: PipelineState) -> PipelineState:
        chunks: list[ChunkPayload] = state.get("reranked_chunks", [])

        if not chunks:
            return {**state, "relevance_pass": False}

        top_score = max(c["score"] for c in chunks)
        relevance_pass = top_score >= self._settings.RELEVANCE_THRESHOLD
        if not relevance_pass or self._settings.CONTEXT_PRUNE_THRESHOLD == 0:
            return {**state, "relevance_pass": relevance_pass}

        retained = [
            chunk for chunk in chunks
            if chunk["score"] >= self._settings.CONTEXT_PRUNE_THRESHOLD
        ]
        return {
            **state,
            "relevance_pass": True,
            "reranked_chunks": retained if len(retained) != len(chunks) else chunks,
        }
