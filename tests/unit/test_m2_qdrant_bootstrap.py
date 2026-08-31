"""M2 — Qdrant bootstrap unit tests (all mocked, no live Qdrant).

Covers assertions 1-10 from the contract. All tests use the fake_qdrant_admin
fixture from tests/conftest.py.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from qdrant_client import models

from app.services import qdrant_bootstrap
from app.core.settings import settings


def test_creates_both_collections_with_bge_m3_params(fake_qdrant_admin):
    """Assertions 1, 2, 3: both collections created with EMBEDDING_DIM and COSINE."""
    asyncio.run(qdrant_bootstrap.ensure_collections(fake_qdrant_admin))

    assert len(fake_qdrant_admin.create_collection_calls) == 2

    # Check documents collection
    docs_call = next(
        c for c in fake_qdrant_admin.create_collection_calls
        if c["collection_name"] == settings.QDRANT_COLLECTION
    )
    assert docs_call["vectors_config"].size == settings.EMBEDDING_DIM
    assert docs_call["vectors_config"].distance == models.Distance.COSINE

    # Check semantic_cache collection
    cache_call = next(
        c for c in fake_qdrant_admin.create_collection_calls
        if c["collection_name"] == settings.QDRANT_CACHE_COLLECTION
    )
    assert cache_call["vectors_config"].size == settings.EMBEDDING_DIM
    assert cache_call["vectors_config"].distance == models.Distance.COSINE


def test_documents_payload_indexes(fake_qdrant_admin):
    """Assertion 4: documents receives exactly allowed_roles, admin_only, document_id."""
    asyncio.run(qdrant_bootstrap.ensure_collections(fake_qdrant_admin))

    docs_indexes = [
        (call["field_name"], call["field_schema"])
        for call in fake_qdrant_admin.create_payload_index_calls
        if call["collection_name"] == settings.QDRANT_COLLECTION
    ]

    expected = {
        ("allowed_roles", models.PayloadSchemaType.KEYWORD),
        ("admin_only", models.PayloadSchemaType.BOOL),
        ("document_id", models.PayloadSchemaType.KEYWORD),
    }
    assert set(docs_indexes) == expected


def test_cache_payload_indexes(fake_qdrant_admin):
    """Assertion 5: semantic_cache receives role, admin_only, created_at, chunk_ids."""
    asyncio.run(qdrant_bootstrap.ensure_collections(fake_qdrant_admin))

    cache_indexes = [
        (call["field_name"], call["field_schema"])
        for call in fake_qdrant_admin.create_payload_index_calls
        if call["collection_name"] == settings.QDRANT_CACHE_COLLECTION
    ]

    expected = {
        ("role", models.PayloadSchemaType.KEYWORD),
        ("admin_only", models.PayloadSchemaType.BOOL),
        ("created_at", models.PayloadSchemaType.FLOAT),
        ("chunk_ids", models.PayloadSchemaType.KEYWORD),
    }
    assert set(cache_indexes) == expected


def test_second_run_creates_nothing(fake_qdrant_admin):
    """Assertions 6, 7: when both collections exist with all indexes, no calls made.
    Also checks the Forbidden rule: no delete_collection calls.
    """
    # Set up: both collections exist with all their indexes
    fake_qdrant_admin.existing_collections = {
        settings.QDRANT_COLLECTION,
        settings.QDRANT_CACHE_COLLECTION,
    }
    fake_qdrant_admin.payload_schemas = {
        settings.QDRANT_COLLECTION: {
            "allowed_roles": models.PayloadSchemaType.KEYWORD,
            "admin_only": models.PayloadSchemaType.BOOL,
            "document_id": models.PayloadSchemaType.KEYWORD,
        },
        settings.QDRANT_CACHE_COLLECTION: {
            "role": models.PayloadSchemaType.KEYWORD,
            "admin_only": models.PayloadSchemaType.BOOL,
            "created_at": models.PayloadSchemaType.FLOAT,
            "chunk_ids": models.PayloadSchemaType.KEYWORD,
        },
    }

    asyncio.run(qdrant_bootstrap.ensure_collections(fake_qdrant_admin))

    assert fake_qdrant_admin.create_collection_calls == []
    assert fake_qdrant_admin.create_payload_index_calls == []
    # Forbidden rule: never delete/recreate
    assert fake_qdrant_admin.delete_collection_calls == []


def test_repairs_single_missing_index(fake_qdrant_admin):
    """Assertion 8: when semantic_cache exists but is missing only admin_only,
    exactly that one index is created.
    """
    # Both collections exist, but semantic_cache is missing admin_only (D5)
    fake_qdrant_admin.existing_collections = {
        settings.QDRANT_COLLECTION,
        settings.QDRANT_CACHE_COLLECTION,
    }
    fake_qdrant_admin.payload_schemas = {
        settings.QDRANT_COLLECTION: {
            "allowed_roles": models.PayloadSchemaType.KEYWORD,
            "admin_only": models.PayloadSchemaType.BOOL,
            "document_id": models.PayloadSchemaType.KEYWORD,
        },
        settings.QDRANT_CACHE_COLLECTION: {
            "role": models.PayloadSchemaType.KEYWORD,
            # admin_only is missing — the realistic D5 case
            "created_at": models.PayloadSchemaType.FLOAT,
            "chunk_ids": models.PayloadSchemaType.KEYWORD,
        },
    }

    asyncio.run(qdrant_bootstrap.ensure_collections(fake_qdrant_admin))

    # No collection created
    assert fake_qdrant_admin.create_collection_calls == []

    # Exactly one index call: semantic_cache.admin_only
    assert len(fake_qdrant_admin.create_payload_index_calls) == 1
    call = fake_qdrant_admin.create_payload_index_calls[0]
    assert call["collection_name"] == settings.QDRANT_CACHE_COLLECTION
    assert call["field_name"] == "admin_only"
    assert call["field_schema"] == models.PayloadSchemaType.BOOL


def test_cli_entry_point_runs_bootstrap():
    """Assertion 9: main() is reachable as __main__ and calls ensure_collections once."""
    with patch.object(
        qdrant_bootstrap, "ensure_collections", new_callable=AsyncMock
    ) as mock_ensure:
        qdrant_bootstrap.main()
        mock_ensure.assert_awaited_once()


def test_startup_awaits_bootstrap_once():
    """Assertion 10: FastAPI lifespan awaits ensure_collections exactly once."""
    from fastapi.testclient import TestClient

    with patch.object(
        qdrant_bootstrap, "ensure_collections", new_callable=AsyncMock
    ) as mock_ensure:
        from app.main import app

        with TestClient(app):
            pass  # lifespan startup runs here

        mock_ensure.assert_awaited_once()
