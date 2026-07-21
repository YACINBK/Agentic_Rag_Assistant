from __future__ import annotations

import re
import string

from app.core.base_node import BaseNode
from app.core.settings import Settings
from app.core.state import ChunkPayload, PipelineState

_SENTENCE_BOUNDARY = re.compile(r"[.!?]+")
_PUNCT_TRANSLATOR = str.maketrans("", "", string.punctuation)


def _tokenize(text: str) -> set[str]:
    cleaned = text.lower().translate(_PUNCT_TRANSLATOR)
    return {w for w in cleaned.split() if len(w) > 1}


def _split_claims(text: str) -> list[str]:
    parts = _SENTENCE_BOUNDARY.split(text)
    return [p.strip() for p in parts if p.strip()]


class FaithfulnessNode(BaseNode):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def name(self) -> str:
        return "faithfulness"

    async def execute(self, state: PipelineState) -> PipelineState:
        answer = state.get("generated_answer", "").strip()
        chunks: list[ChunkPayload] = state.get("reranked_chunks", [])

        if not answer or not chunks:
            return {**state, "is_faithful": False, "faithfulness_score": 0.0}

        chunk_vocab: set[str] = set()
        for chunk in chunks:
            chunk_vocab |= _tokenize(chunk["text"])

        claims = _split_claims(answer)
        grounded = 0
        for claim in claims:
            claim_words = _tokenize(claim)
            if not claim_words:
                continue
            overlap = len(claim_words & chunk_vocab) / len(claim_words)
            if overlap >= 0.3:
                grounded += 1

        score = grounded / len(claims) if claims else 0.0
        is_faithful = score >= self._settings.FAITHFULNESS_THRESHOLD

        return {**state, "is_faithful": is_faithful, "faithfulness_score": score}
