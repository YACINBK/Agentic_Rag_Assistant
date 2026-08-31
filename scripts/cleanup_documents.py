"""Remove test documents from every store they touch. Dry-run unless --apply.

Written for the pre-demo corpus cleanup: the stripped Ask&Go document (superseded
by the image-complete full version) and the PDFs/CV used while probing extraction.

Deletion order matters and is not arbitrary:

    1. Qdrant points   — reuses delete_by_filter({"document_id": ...}), the SAME
                         primitive app/ingestion/index.py:198 uses to purge before
                         re-indexing. No second deletion path to drift from it.
    2. semantic_cache  — a cached answer grounded in deleted chunks is stale (§9).
                         Deleted by role-agnostic scroll because chunk_ids overlap
                         is a list-contains test, not a field match.
    3. derived images   — ONLY those no surviving document references. Images
                         deduplicate by content hash and document_image is a
                         junction table, so a shared image must survive the
                         deletion of any one of its documents.
    4. Postgres row     — last, because it is the thing that tells us 1–3 are
                         needed. document_image rows go with it (ondelete=CASCADE).
    5. Source upload    — the content-addressed file under uploads/.

AuditLog is untouched: it is append-only (§12), has no FK to Document, and its
chunks_used JSON is a historical record of what *was* retrieved. Dangling ids
there are correct, not corruption.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qdrant_client import AsyncQdrantClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.core.settings import settings  # noqa: E402
from app.services.vector_store import QdrantVectorStore  # noqa: E402

# ── The corpus decision, stated as data ─────────────────────────────────────
# KEEP is a guard, not documentation: a document id in both sets aborts the run.
# Without it a copy-paste slip silently deletes the one corpus the demo needs.
KEEP: dict[str, str] = {
    "f4b11ea3-717e-4b72-b5a7-0e84a4bc06c7": "askgo-621x-fr.html — 477 chunks, 2199 images, image-complete",
}

DELETE: dict[str, str] = {
    "e4cce0a4-9fe7-425b-8a00-e83b98c646f6": "askgo-621x-fr.stripped.html — superseded; its 1 image is the 8-byte broken stub",
    "6d36db3c-d3f9-47df-952d-2e415e63a669": "Charte IA Etudiants.pdf — extraction probe, failed, 0 chunks",
    "6e9ce26c-4f7c-4148-b3b4-1ec5bc18ec0a": "Yacin_Ben_Kacem.pdf — extraction probe, failed, 0 chunks",
    "556a6724-f51d-4466-84cb-aaaaa92f76ea": "Yacin_Ben_Kacem_CV.pdf — extraction probe, failed, 0 chunks",
    "665d942e-ac71-4731-aebc-e71290d8778e": "Yacin_Ben_Kacem_CV.html — probe, status=done but 0 chunks (silent zero-chunk success)",
}


async def orphaned_images(conn, document_id: str) -> list[str]:
    """image_ids this document owns that NO other document references.

    EXCEPT against every other document is what makes a shared image survive.
    """
    rows = await conn.execute(
        text(
            "SELECT image_id FROM document_image WHERE document_id = :d"
            " EXCEPT"
            " SELECT image_id FROM document_image WHERE document_id != :d"
        ),
        {"d": document_id},
    )
    return list(rows.scalars().all())


async def stale_cache_points(client: AsyncQdrantClient, chunk_ids: set[str]) -> list[str]:
    """Cache point ids whose chunk_ids overlap the chunks being deleted (§9)."""
    if not chunk_ids:
        return []
    stale, offset = [], None
    while True:
        points, offset = await client.scroll(
            collection_name=settings.QDRANT_CACHE_COLLECTION,
            limit=500,
            offset=offset,
            with_payload=["chunk_ids"],
            with_vectors=False,
        )
        for point in points:
            if chunk_ids & set(point.payload.get("chunk_ids") or []):
                stale.append(point.id)
        if offset is None:
            return stale


async def main(apply: bool) -> int:
    overlap = KEEP.keys() & DELETE.keys()
    if overlap:
        print(f"ABORT: id in both KEEP and DELETE: {overlap}")
        return 1

    mode = "APPLY" if apply else "DRY-RUN"
    print(f"=== cleanup_documents [{mode}] ===\n")

    engine = create_async_engine(settings.DATABASE_URL)
    raw = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)
    store = QdrantVectorStore(client=raw)
    uploads = Path(settings.UPLOAD_DIR) if hasattr(settings, "UPLOAD_DIR") else Path("uploads")
    derived = uploads / "derived"

    before = (await raw.get_collection(settings.QDRANT_COLLECTION)).points_count
    print(f"qdrant '{settings.QDRANT_COLLECTION}' points before: {before}\n")

    planned = {"points": 0, "images": 0, "files": 0, "rows": 0, "cache": 0}

    async with engine.begin() as conn:
        for document_id, reason in DELETE.items():
            row = (
                await conn.execute(
                    text(
                        "SELECT original_filename, source_path, chunk_count, ingestion_status"
                        " FROM document WHERE id = :d"
                    ),
                    {"d": document_id},
                )
            ).mappings().one_or_none()

            if row is None:
                print(f"-- {document_id}: already absent, skipping")
                continue

            print(f"-- {row['original_filename']}  ({document_id})")
            print(f"   why: {reason}")

            # 1. Qdrant points for this document
            ids, offset = [], None
            while True:
                points, offset = await raw.scroll(
                    collection_name=settings.QDRANT_COLLECTION,
                    limit=1000,
                    offset=offset,
                    scroll_filter={"must": [{"key": "document_id", "match": {"value": document_id}}]},
                    with_payload=False,
                    with_vectors=False,
                )
                ids.extend(str(p.id) for p in points)
                if offset is None:
                    break
            print(f"   qdrant points     : {len(ids)}")
            planned["points"] += len(ids)

            # 2. Cache entries grounded in those chunks
            stale = await stale_cache_points(raw, set(ids))
            print(f"   stale cache points: {len(stale)}")
            planned["cache"] += len(stale)

            # 3. Derived images no surviving document needs
            orphans = await orphaned_images(conn, document_id)
            print(f"   orphaned images   : {len(orphans)}")
            planned["images"] += len(orphans)

            # 4/5. Row + source file
            source = Path(row["source_path"])
            print(f"   source file       : {source.name} (exists={source.exists()})")
            planned["rows"] += 1
            planned["files"] += 1 if source.exists() else 0

            if apply:
                if ids:
                    await store.delete_by_filter(
                        settings.QDRANT_COLLECTION, {"document_id": document_id}
                    )
                if stale:
                    await raw.delete(
                        collection_name=settings.QDRANT_CACHE_COLLECTION,
                        points_selector=stale,
                    )
                for image_id in orphans:
                    target = derived / f"{image_id}.png"
                    if target.exists():
                        target.unlink()
                # CASCADE clears document_image; do this before unlinking the source
                # so a crash leaves an orphan file (harmless) not an orphan row.
                await conn.execute(
                    text("DELETE FROM document WHERE id = :d"), {"d": document_id}
                )
                if source.exists():
                    source.unlink()
                print("   -> deleted")
            print()

    print("planned totals:", planned)

    after = (await raw.get_collection(settings.QDRANT_COLLECTION)).points_count
    print(f"\nqdrant points after: {after}  (delta {after - before})")

    if apply:
        expected = before - planned["points"]
        if after != expected:
            print(f"WARNING: expected {expected} points, found {after}")
        else:
            print("point count matches expectation")

    print("\nKEEP (untouched):")
    for document_id, why in KEEP.items():
        remaining, offset = 0, None
        while True:
            points, offset = await raw.scroll(
                collection_name=settings.QDRANT_COLLECTION,
                limit=1000,
                offset=offset,
                scroll_filter={"must": [{"key": "document_id", "match": {"value": document_id}}]},
                with_payload=False,
                with_vectors=False,
            )
            remaining += len(points)
            if offset is None:
                break
        print(f"  {remaining:>5} points  {why}")

    await raw.close()
    await engine.dispose()
    if not apply:
        print("\nDry run — nothing changed. Re-run with --apply to execute.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    raise SystemExit(asyncio.run(main(parser.parse_args().apply)))
