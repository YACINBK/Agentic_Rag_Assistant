from __future__ import annotations

from qdrant_client import AsyncQdrantClient, models

from app.core.services.vector_store import BaseVectorStore
from app.core.settings import settings
from app.core.state import ChunkPayload


class QdrantVectorStore(BaseVectorStore):

    def __init__(self) -> None:
        self._client = AsyncQdrantClient(
            host=settings.QDRANT_HOST, port=settings.QDRANT_PORT
        )

    async def search(
        self,
        collection: str,
        query_vector: list[float],
        allowed_roles: list[str],
        limit: int = 10,
    ) -> list[ChunkPayload]:
        # Role filter field differs by collection schema (CLAUDE.md §8 vs §9):
        #   documents      → payload key "allowed_roles" (list[str])
        #   semantic_cache → payload key "role"          (str)
        role_key = (
            "role"
            if collection == settings.QDRANT_CACHE_COLLECTION
            else "allowed_roles"
        )
        results = await self._client.query_points(
            collection_name=collection,
            query=query_vector,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key=role_key,
                        match=models.MatchAny(any=allowed_roles),
                    )
                ]
            ),
            limit=limit,
            with_payload=True,
        )
        return [
            ChunkPayload(
                chunk_id=str(point.id),
                # documents store the chunk under "text"; semantic_cache stores
                # the generated answer under "answer_text" (CLAUDE.md §9).
                text=point.payload.get("text") or point.payload.get("answer_text", ""),
                document_id=point.payload.get("document_id", ""),
                original_filename=point.payload.get("original_filename", ""),
                category=point.payload.get("category", ""),
                page_number=point.payload.get("page_number", 0),
                chunk_index=point.payload.get("chunk_index", 0),
                score=point.score,
            )
            for point in results.points
        ]

    async def upsert(
        self,
        collection: str,
        points: list[dict],
    ) -> None:
        await self._client.upsert(
            collection_name=collection,
            points=[
                models.PointStruct(
                    id=p["id"],
                    vector=p["vector"],
                    payload=p["payload"],
                )
                for p in points
            ],
        )

    async def delete_by_filter(
        self,
        collection: str,
        filter_conditions: dict,
    ) -> None:
        must_conditions = [
            models.FieldCondition(key=k, match=models.MatchValue(value=v))
            for k, v in filter_conditions.items()
        ]
        await self._client.delete(
            collection_name=collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(must=must_conditions)
            ),
        )
