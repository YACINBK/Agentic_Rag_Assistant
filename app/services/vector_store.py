from __future__ import annotations

from qdrant_client import AsyncQdrantClient, models

from app.core.services.vector_store import BaseVectorStore
from app.core.settings import settings
from app.core.state import ChunkPayload


class QdrantVectorStore(BaseVectorStore):
    def __init__(self, client: AsyncQdrantClient | None = None) -> None:
        self._client = client or AsyncQdrantClient(
            host=settings.QDRANT_HOST, port=settings.QDRANT_PORT
        )

    async def search(
        self,
        collection: str,
        query_vector: list[float],
        allowed_roles: list[str],
        user_is_admin: bool,
        limit: int = 10,
    ) -> list[ChunkPayload]:
        # Role filter field differs by collection schema (CLAUDE.md §8 vs §9):
        #   documents      → payload key "allowed_roles" (list[str])
        #   semantic_cache → payload key "role"          (str)
        role_key = "role" if collection == settings.QDRANT_CACHE_COLLECTION else "allowed_roles"

        # Two-dimension security filter (CLAUDE.md §5 Layer 1):
        #   allowed_roles — subject-matter scope (whose job is this about?)
        #   admin_only    — privilege tier (how much trust does reading it need?)
        # Non-admin: both conditions ANDed. Admin: privilege condition ABSENT
        # (not inverted — admins must see restricted AND unrestricted chunks).
        must = [
            models.FieldCondition(
                key=role_key,
                match=models.MatchAny(any=allowed_roles),
            )
        ]
        if not user_is_admin:
            must.append(
                models.FieldCondition(
                    key="admin_only",
                    match=models.MatchValue(value=False),
                )
            )

        results = await self._client.query_points(
            collection_name=collection,
            query=query_vector,
            query_filter=models.Filter(must=must),
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
                # Written by the ingestion pipeline; default for pre-ingestion chunks.
                section_path=point.payload.get("section_path", ""),
                anchor=point.payload.get("anchor", ""),
                image_refs=point.payload.get("image_refs", []),
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
            points_selector=models.FilterSelector(filter=models.Filter(must=must_conditions)),
        )

    async def delete_by_any(
        self,
        collection: str,
        key: str,
        values: list[str],
    ) -> None:
        # MatchAny, not MatchValue: the cache's `chunk_ids` payload field is an
        # array, and MatchValue cannot match a value *inside* one (M6 D2).
        #
        # The empty guard is load-bearing, not defensive. An empty `any=[]` leaves
        # a filter with no effective condition, and Filter(must=[]) selects EVERY
        # point in the collection — so a zero-chunk document would wipe the whole
        # cache instead of invalidating nothing. Returning before the client call
        # is the only shape that cannot express that.
        if not values:
            return

        await self._client.delete(
            collection_name=collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(key=key, match=models.MatchAny(any=values)),
                    ]
                )
            ),
        )

    async def get_chunk_ids_for_document(
        self,
        collection: str,
        document_id: str,
    ) -> list[str]:
        """Retrieve all point IDs associated with a document. Used for cache invalidation."""
        result = await self._client.scroll(
            collection_name=collection,
            scroll_filter=models.Filter(
                must=[models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id))]
            ),
            limit=1000,
            with_payload=False,
        )
        return [point.id for point in result[0]]
