from __future__ import annotations

from app.core.base_node import BaseNode
from app.core.services.llm import BaseLLMService
from app.core.settings import Settings
from app.core.state import PipelineState

_SYSTEM_PROMPT = """You are a query rewriter. Your job is to reformulate a user's question
so it retrieves better results from a company knowledge base.

Rules:
- Expand abbreviations (QA → quality assurance, PTO → paid time off, VPN → virtual private network)
- Add domain context using terms likely found in company documents
- Keep the original intent unchanged — do not answer the question, just rewrite it
- Use keywords that would appear in internal documentation

Return ONLY the rewritten query. No explanation, no prefix, no quotes."""


class RewriterNode(BaseNode):
    def __init__(self, llm: BaseLLMService, settings: Settings) -> None:
        self._llm = llm
        self._settings = settings

    @property
    def name(self) -> str:
        return "rewriter"

    async def execute(self, state: PipelineState) -> PipelineState:
        classification = state.get("classification", "")
        if classification == "DIRECT":
            return state

        original = state["query"]

        try:
            raw = await self._llm.complete(
                model=self._settings.REWRITER_MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": original},
                ],
                temperature=0.0,
            )
        except Exception:
            return {**state, "rewritten_query": original, "sub_queries": []}

        cleaned = raw.strip()
        if not cleaned:
            return {**state, "rewritten_query": original, "sub_queries": []}

        return {**state, "rewritten_query": cleaned, "sub_queries": []}
