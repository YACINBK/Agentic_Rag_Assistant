from __future__ import annotations

from app.core.base_node import BaseNode
from app.core.settings import Settings
from app.core.state import PipelineState


class RetryNode(BaseNode):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def name(self) -> str:
        return "retry"

    async def execute(self, state: PipelineState) -> PipelineState:
        if state.get("retry_attempted"):
            return {**state, "retry_attempted": True, "retry_pass": False}

        return {
            **state,
            "rewritten_query": state["query"],
            "retry_attempted": True,
            "retry_pass": True,
        }
