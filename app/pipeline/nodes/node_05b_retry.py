from __future__ import annotations

from app.core.base_node import BaseNode
from app.core.services.llm import BaseLLMService
from app.core.settings import Settings
from app.core.state import PipelineState

_BROADEN_PROMPT = """You are a search query optimizer. The previous query did not retrieve relevant results.
Your job is to broaden the query so it matches more documents.

Rules:
- Remove overly specific terms
- Add synonyms or related concepts
- Keep the original intent
- Return ONLY the broadened query, no explanation

Original user question: {original}
Failed rewritten query: {rewritten}

Return a broader version of the query:"""


class RetryNode(BaseNode):
    def __init__(self, llm: BaseLLMService, settings: Settings) -> None:
        self._llm = llm
        self._settings = settings

    @property
    def name(self) -> str:
        return "retry"

    async def execute(self, state: PipelineState) -> PipelineState:
        if state.get("retry_attempted"):
            return {**state, "retry_attempted": True}

        original = state["query"]
        rewritten = state.get("rewritten_query", original)

        try:
            raw = await self._llm.complete(
                model=self._settings.REWRITER_MODEL,
                messages=[{
                    "role": "user",
                    "content": _BROADEN_PROMPT.format(
                        original=original, rewritten=rewritten,
                    ),
                }],
                temperature=0.3,
            )
            broadened = raw.strip()
        except Exception:
            broadened = ""

        if not broadened:
            broadened = original

        return {
            **state,
            "rewritten_query": broadened,
            "retry_attempted": True,
        }
