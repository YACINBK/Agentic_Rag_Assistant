"""Abstract vector store service.

Concrete implementation uses Qdrant.
Qdrant role filter is a hard security boundary — applied at query time,
never filtered in Python after the fact.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.state import ChunkPayload


class BaseVectorStore(ABC):

    @abstractmethod
    async def search(
        self,
        collection: str,
        query_vector: list[float],
        allowed_roles: list[str],
        user_is_admin: bool,
        limit: int = 10,
    ) -> list[ChunkPayload]:
        """Semantic search with the mandatory two-dimension security filter.

        user_is_admin is a required argument with NO default (CLAUDE.md §5):
        omitting it must raise TypeError, never silently guess a privilege tier.
        """
        ...

    @abstractmethod
    async def upsert(
        self,
        collection: str,
        points: list[dict],
    ) -> None:
        """Insert or update vectors with payload metadata."""
        ...

    @abstractmethod
    async def delete_by_filter(
        self,
        collection: str,
        filter_conditions: dict,
    ) -> None:
        """Delete points matching filter conditions."""
        ...

    @abstractmethod
    async def delete_by_any(
        self,
        collection: str,
        key: str,
        values: list[str],
    ) -> None:
        """Delete points whose payload `key` matches ANY of `values`.

        Distinct from delete_by_filter, which builds MatchValue only and therefore
        cannot match a value *inside* a payload array (M6 D2). The semantic cache's
        `chunk_ids` is such an array, so MatchValue against it would match nothing
        and report success — a silent no-op where a deletion was required.
        """
        ...
