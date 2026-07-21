from __future__ import annotations

from app.core.base_node import BaseNode
from app.core.services.llm import BaseLLMService
from app.core.settings import Settings
from app.core.state import PipelineState

_DIRECT_RESPONSE = (
    "I can only answer questions based on Whitecape's indexed internal documents. "
    "Please ask something related to our company knowledge base."
)

_SYSTEM_PROMPT = """You are a query classifier. Your ONLY job is to classify user queries into one of two categories:

DIRECT — the query cannot be answered from indexed company documents. This includes:
- Greetings and chitchat (hello, how are you, thanks, hey)
- Out-of-scope questions (weather, sports, news, general trivia)
- Anything clearly unrelated to internal company knowledge

SIMPLE_RAG — the query might be answerable from company documents. This includes:
- Questions about policies, procedures, standards, deployment
- Questions about the company, team, tools, projects
- Anything that could plausibly relate to internal documentation

Reply with EXACTLY one word: DIRECT or SIMPLE_RAG. No explanation, no punctuation, no extra text."""


class ClassifierNode(BaseNode):
    def __init__(self, llm: BaseLLMService, settings: Settings) -> None:
        self._llm = llm
        self._settings = settings

    @property
    def name(self) -> str:
        return "classifier"

    async def execute(self, state: PipelineState) -> PipelineState:
        query = state["query"]

        if self._settings.CLASSIFIER_MODEL == "manual":
            return {**state, "classification": "SIMPLE_RAG"}

        try:
            raw = await self._llm.complete(
                model=self._settings.CLASSIFIER_MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": query},
                ],
                temperature=0.0,
            )
        except Exception:
            return {**state, "classification": "SIMPLE_RAG"}

        classification = self._parse(raw)

        if classification == "DIRECT":
            return {
                **state,
                "classification": "DIRECT",
                "direct_response": _DIRECT_RESPONSE,
            }

        return {**state, "classification": "SIMPLE_RAG"}

    @staticmethod
    def _parse(raw: str) -> str:
        cleaned = raw.strip().upper()
        if cleaned == "DIRECT":
            return "DIRECT"
        if cleaned == "SIMPLE_RAG":
            return "SIMPLE_RAG"
        if "DIRECT" in cleaned and "SIMPLE" not in cleaned:
            return "DIRECT"
        return "SIMPLE_RAG"
