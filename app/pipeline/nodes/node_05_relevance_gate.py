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
        return {**state, "relevance_pass": top_score >= self._settings.RELEVANCE_THRESHOLD}
