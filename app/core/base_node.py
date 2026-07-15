"""Abstract base for all pipeline nodes.

Every node in app/pipeline/nodes/ inherits from BaseNode.
This enforces a uniform interface: execute(state) → state.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.state import PipelineState


class BaseNode(ABC):
    """Abstract pipeline node. Subclasses implement execute()."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier used in logging and routing (e.g. 'classifier')."""
        ...

    @abstractmethod
    async def execute(self, state: PipelineState) -> PipelineState:
        """Run the node's logic, return updated state.

        Must not mutate the input dict — return a new dict with updates merged.
        """
        ...
