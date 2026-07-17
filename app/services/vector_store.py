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
        results = await self._client.query_points(
            collection_name=collection,
            query=query_vector,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="allowed_roles",
                        match=models.MatchAny(any=allowed_roles),
                    )
                ]
            ),
            limit=limit,
            with_payload=True,
        )
        return [
            ChunkPayload(
                text=point.payload.get("text", ""),
                document_id=point.payload.get("document_id", ""),
                chunk_index=point.payload.get("chunk_index", 0),
                score=point.score,
                metadata=point.payload.get("metadata", {}),
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
