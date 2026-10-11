"""Ingestion metadata must survive retrieval.

`app/ingestion/index.py` writes twenty-odd payload fields per chunk — BookStack
provenance and Stage C enrichment among them — and `QdrantVectorStore.search`
built a `ChunkPayload` from eleven of them. The rest were written and unreadable:
every retrieval paid the ingestion cost and discarded the result.

No node consumes them yet, which is the point. The Investigator's candidate tools
(query-time use of applicability metadata, and a source-section fetch) need the
handles to exist before they can be built, and projecting them is cheap while
re-ingesting the corpus to recover them would not be.

One test here asserts a field is ABSENT. That is deliberate — see
TestDeliberateOmission.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.settings import settings
from app.services.vector_store import QdrantVectorStore

pytestmark = pytest.mark.asyncio

FULL_PAYLOAD = {
    "text": "Pour créer une demande d'achat, ouvrez le menu Demandes.",
    "document_id": "doc-1",
    "original_filename": "askgo-621x-fr.html",
    "category": "technical",
    "page_number": 12,
    "chunk_index": 3,
    "section_path": "Demandes > Création > Formulaire",
    "anchor": "bkmrk-creation",
    "image_refs": [{"image_id": "a" * 64, "anchor": "bkmrk-creation"}],
    # BookStack provenance
    "page_id": "page-77",
    "page_title": "Création d'une demande",
    "chapter_id": "chap-4",
    "chapter_title": "Demandes",
    "related_anchors": ["bkmrk-validation", "bkmrk-suivi"],
    "table_count": 2,
    "doc_hash": "d" * 64,
    # Stage C enrichment
    "applies_to": ["Commandes", "Réceptions", "Factures"],
    "contextual_header": "Demandes > Création : procédure de saisie",
    "is_generalized_procedure": True,
    # §5 dimensions
    "allowed_roles": ["all"],
    "admin_only": False,
}


class _Client:
    """Returns one point with a caller-supplied payload."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[dict] = []

    async def query_points(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            points=[SimpleNamespace(id="point-1", score=0.87, payload=self._payload)]
        )


async def search(payload: dict) -> dict:
    client = _Client(payload)
    store = QdrantVectorStore(client=client)
    results = await store.search(
        collection=settings.QDRANT_COLLECTION,
        query_vector=[0.1] * 8,
        allowed_roles=["developer", "all"],
        user_is_admin=False,
    )
    return results[0]


class TestProjection:
    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            ("page_id", "page-77"),
            ("page_title", "Création d'une demande"),
            ("chapter_id", "chap-4"),
            ("chapter_title", "Demandes"),
            ("related_anchors", ["bkmrk-validation", "bkmrk-suivi"]),
            ("table_count", 2),
            ("doc_hash", "d" * 64),
            ("applies_to", ["Commandes", "Réceptions", "Factures"]),
            ("contextual_header", "Demandes > Création : procédure de saisie"),
            ("is_generalized_procedure", True),
            ("admin_only", False),
        ],
    )
    async def test_field_reaches_the_chunk_payload(self, field, expected) -> None:
        """Parametrised one field per case: a projection that drops a single field
        is the exact defect this closes, so a single aggregate assertion would let
        the next one through."""
        chunk = await search(FULL_PAYLOAD)
        assert chunk[field] == expected

    async def test_admin_only_true_is_carried_verbatim(self) -> None:
        """A False default would make every restricted chunk look unrestricted to
        D5's deferred cache rule — the one consumer this field exists for."""
        chunk = await search({**FULL_PAYLOAD, "admin_only": True})
        assert chunk["admin_only"] is True

    async def test_previously_projected_fields_are_unchanged(self) -> None:
        """Regression guard: the additions must not disturb what already worked."""
        chunk = await search(FULL_PAYLOAD)
        assert chunk["chunk_id"] == "point-1"
        assert chunk["score"] == 0.87
        assert chunk["section_path"] == "Demandes > Création > Formulaire"
        assert chunk["anchor"] == "bkmrk-creation"
        assert chunk["image_refs"][0]["image_id"] == "a" * 64
        assert chunk["text"].startswith("Pour créer")


class TestPreIngestionChunks:
    async def test_bare_payload_yields_safe_defaults(self) -> None:
        """A chunk indexed before these fields existed must not raise. Qdrant
        payloads are schemaless, so old points simply lack the keys."""
        bare = {
            "text": "ancien chunk",
            "document_id": "doc-0",
            "original_filename": "old.pdf",
            "category": "technical",
            "page_number": 1,
            "chunk_index": 0,
        }
        chunk = await search(bare)

        assert chunk["applies_to"] == []
        assert chunk["related_anchors"] == []
        assert chunk["contextual_header"] == ""
        assert chunk["page_title"] == ""
        assert chunk["doc_hash"] == ""
        assert chunk["table_count"] == 0
        assert chunk["is_generalized_procedure"] is False
        assert chunk["admin_only"] is False


class TestDeliberateOmission:
    async def test_allowed_roles_is_not_projected(self) -> None:
        """Asserting an ABSENCE, on purpose.

        `admin_only` is projected because D5's deferred precise cache rule names
        its absence from ChunkPayload as the blocker. `allowed_roles` has no
        consumer, and putting the subject-matter scope within reach of Python
        invites exactly the post-retrieval filtering CLAUDE.md §12 forbids: the §5
        boundary is enforced inside the vector query, and code that can re-read
        the scope downstream is code that can be tempted to re-check it there.

        If a real consumer appears, projecting it is a one-line change — but it
        should be an argued decision, which is what this test forces.
        """
        chunk = await search(FULL_PAYLOAD)
        assert "allowed_roles" not in chunk


class TestSecurityFilterUnaffected:
    async def test_query_filter_still_carries_both_dimensions(self) -> None:
        """The projection touches the response mapping only. The §5 filter is
        built before it and must be untouched: role MatchAny plus, for a
        non-admin, the appended admin_only == False condition."""
        client = _Client(FULL_PAYLOAD)
        store = QdrantVectorStore(client=client)
        await store.search(
            collection=settings.QDRANT_COLLECTION,
            query_vector=[0.1] * 8,
            allowed_roles=["developer", "all"],
            user_is_admin=False,
        )

        must = client.calls[0]["query_filter"].must
        keys = [condition.key for condition in must]
        assert keys == ["allowed_roles", "admin_only"]
        assert must[0].match.any == ["developer", "all"]
        assert must[1].match.value is False

    async def test_admin_search_omits_the_privilege_condition(self) -> None:
        """Admins must see restricted AND unrestricted chunks, so the condition is
        absent rather than inverted (CLAUDE.md §5)."""
        client = _Client(FULL_PAYLOAD)
        store = QdrantVectorStore(client=client)
        await store.search(
            collection=settings.QDRANT_COLLECTION,
            query_vector=[0.1] * 8,
            allowed_roles=["developer", "all"],
            user_is_admin=True,
        )

        keys = [c.key for c in client.calls[0]["query_filter"].must]
        assert keys == ["allowed_roles"]
