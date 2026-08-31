"""Admin document-upload route (M7).

`POST /admin/documents` — an admin uploads a document, it is content-hashed,
persisted to storage and PostgreSQL, and an ingestion task is enqueued. The
route returns 202 immediately; Celery does the ingestion out of band (§8).

Security posture (§2, §5):
- `Depends(require_admin)` gates the whole route — primary role alone never
  grants upload. Owner is covered transitively (is_owner ⇒ is_admin, §2).
- `allowed_roles` is hardcoded to ["all"] server-side, never read from the
  form: Document has no allowed_roles column in the MVP schema, and letting a
  client name roles would be a subject-matter-scope escalation vector. Per-role
  scoping is a later milestone.
- `admin_only` for the resulting chunks comes from `Document.restricted`
  (§5, §8) — the one form field that touches the privilege tier.

Error handling is deliberately local (400/409/413 returned as JSONResponse from
this handler), not delegated to a global exception handler: FileValidationError
is a per-route input concern here, and a global handler would also swallow it in
contexts that should propagate.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.dependencies import require_admin, require_owner
from app.core.exceptions import FileValidationError
from app.core.models.base import get_session
from app.core.models.document import Document
from app.core.models.document_image import DocumentImage
from app.core.models.user import User
from app.core.models.role import Role
from app.core.security import UserSession
from app.core.services.storage import DERIVED_EXTENSIONS, compute_doc_hash
from app.core.settings import settings
from app.services.sessions import purge_user_sessions
from app.services.storage import LocalFileStorage
from app.services.vector_store import QdrantVectorStore
from app.tasks.ingestion import ingest_document

admin_router = APIRouter(prefix="/admin", tags=["admin"])

# Subject-matter scope is fixed server-side for the MVP — see module docstring.
DEFAULT_ALLOWED_ROLES: list[str] = ["all"]

# The four §10 Document.category values, as a request-validation type. FastAPI
# rejects anything else with 422 before the handler body runs.
DocumentCategory = Literal["technical", "quality", "projects", "company"]

# Module-level so tests can patch `app.api.routes.admin.storage` and
# `app.api.routes.admin.ingest_document`. LocalFileStorage takes no args and
# reads settings.UPLOAD_DIR at each save (tests monkeypatch that directory).
storage = LocalFileStorage()

# Read granularity for the bounded upload read. 1 MiB balances syscall count
# against the transient buffer held per chunk before it joins the whole body.
_CHUNK_SIZE = 1024 * 1024


class _UploadTooLarge(Exception):
    """Raised by `_read_bounded` when the body exceeds the size ceiling.

    Carries the limit so the handler can name it in the 413 body. Internal to
    this module — never leaves the handler.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit


async def _read_bounded(file: UploadFile, limit: int) -> bytes:
    """Read the whole upload into memory, aborting past `limit` bytes.

    The abort happens as chunks arrive, so an oversized upload is rejected
    without ever materializing the full body or hashing it (§ the 413-before-
    hashing assertion). MVP buffers fully in memory — accepted for the known
    corpus size, bounded by `limit`.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise _UploadTooLarge(limit)
        chunks.append(chunk)
    return b"".join(chunks)


@admin_router.post("/documents", status_code=202)
async def upload_document(
    request: Request,
    file: UploadFile,
    category: DocumentCategory = Form(...),
    restricted: bool = Form(False),
    user: UserSession = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Accept an admin upload, persist it, and enqueue ingestion.

    Ordering is load-bearing: size check → hash → dedup → storage → DB row →
    enqueue. Dedup precedes storage so a duplicate never writes a file, and the
    enqueue happens only after the row is committed so the worker can read it.
    """
    if not file.filename:
        return JSONResponse(status_code=400, content={"detail": "A filename is required"})

    # Size ceiling first, read at the call site so monkeypatched settings apply.
    try:
        content = await _read_bounded(file, settings.MAX_UPLOAD_SIZE_BYTES)
    except _UploadTooLarge as exc:
        return JSONResponse(
            status_code=413,
            content={"detail": f"File exceeds maximum upload size of {exc.limit} bytes"},
        )

    # Content-addressed dedup BEFORE any filesystem write (§ dedup precedes
    # storage): a re-upload of identical bytes must not orphan a second file.
    doc_hash = compute_doc_hash(content)
    existing = await session.execute(select(Document).where(Document.doc_hash == doc_hash))
    existing_doc = existing.scalar_one_or_none()
    if existing_doc is not None:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Document with this content already exists",
                "document_id": str(existing_doc.id),
            },
        )

    # Storage validates the extension and raises FileValidationError for a
    # disallowed type — caught here, never by a global handler.
    try:
        source_path = await storage.save(file.filename, content)
    except FileValidationError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    document = Document(
        source_path=source_path,
        original_filename=file.filename,
        category=category,
        restricted=restricted,
        doc_hash=doc_hash,
        uploaded_by=uuid.UUID(user.user_id),
    )
    session.add(document)
    # Flush assigns the client-side UUID default so we can read the id, then
    # commit so the row is durable before the worker is told it exists.
    await session.flush()
    document_id = str(document.id)
    await session.commit()

    ingest_document.delay(document_id, DEFAULT_ALLOWED_ROLES)

    # §17 rule 5 — same full-page-vs-partial branch the GET below uses, and the
    # reason D28 is fixed without touching M8 Assertion 7. The browser's form
    # targets #document-list, so an HTMX upload gets the re-queried list partial:
    # the row just committed is `pending`, so `_document_list_context` computes
    # poll=True and the swapped-in partial arrives already carrying its own
    # `hx-trigger`. The list then walks itself pending → running → done with no
    # manual reload. Trigger presence is still decided solely by
    # IN_FLIGHT_STATUSES — nothing is made unconditional.
    #
    # Non-HTMX callers (the API, and every test in
    # tests/integration/test_admin_upload_route.py, whose `_post` sends no
    # HX-Request header) keep the 202 + {"document_id"} contract unchanged.
    # Both branches return 202 — M7 forbids a bare 200 on any success path.
    if request.headers.get("hx-request", "").lower() == "true":
        return templates.TemplateResponse(
            request,
            "partials/document_list.html",
            await _document_list_context(user, session),
            status_code=202,
        )

    return JSONResponse(status_code=202, content={"document_id": document_id})


# --- M8: admin document list (read-only) ---

# Matches app/api/routes/search.py:27 and app/api/error_handlers.py:15 — each
# module that renders builds its own instance against the same directory.
templates = Jinja2Templates(directory="app/templates")

# UI refresh cadence, not an operational knob — a module constant, not a new
# setting. (M7 added MAX_UPLOAD_SIZE_BYTES to settings because a size ceiling is
# deployment-tunable; a poll interval is not.)
POLL_INTERVAL_SECONDS = 3

IN_FLIGHT_STATUSES = ("pending", "running")


async def _document_list_context(user: UserSession, session: AsyncSession) -> dict:
    """Load the ordered document list plus the derived `poll` flag.

    Shared by `GET /admin/documents` and the HTMX branch of `upload_document`
    above (a forward reference — resolved at call time, not import time) so both
    render `partials/document_list.html` from byte-identical context. One query,
    one ordering, one place `poll` is computed.

    Ordering (Decision 1) puts NULL `last_ingested_at` first — a freshly
    uploaded, not-yet-ingested document — then newest-ingested first, tie-broken
    by filename for a total order. `poll` is computed here and never in the
    template: §17 rule 3 keeps components stateless.
    """
    result = await session.execute(
        select(Document).order_by(
            Document.last_ingested_at.desc().nulls_first(),
            Document.original_filename.asc(),
        )
    )
    documents = result.scalars().all()
    return {
        "user": user,
        "documents": documents,
        "poll": any(document.ingestion_status in IN_FLIGHT_STATUSES for document in documents),
        "poll_interval": POLL_INTERVAL_SECONDS,
    }


@admin_router.get("/documents")
async def list_documents(
    request: Request,
    user: UserSession = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the admin document list — full page, or the list partial for HTMX.

    Read-only: no mutation, no Celery interaction.
    """
    context = await _document_list_context(user, session)

    if request.headers.get("hx-request", "").lower() == "true":
        return templates.TemplateResponse(request, "partials/document_list.html", context)
    return templates.TemplateResponse(request, "pages/admin/documents.html", context)


# --- M8b: admin document mutations ---

@admin_router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    request: Request,
    document_id: uuid.UUID,
    user: UserSession = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Wipe a document from the system: DB, storage, Qdrant, and semantic cache.

    Ordering is load-bearing in two separate places.

    1. The chunk IDs are read from Qdrant BEFORE the document's points are
       deleted. Read afterwards they come back empty, and `delete_by_any` then
       short-circuits on the empty list (app/services/vector_store.py) — so the
       semantic cache would never be invalidated and nothing would say so. That
       empty guard is correct and must stay; it exists because an empty
       `MatchAny` leaves a filter with no condition, which selects every point
       in the collection. Only the call order was wrong.

    2. The Document row is deleted but NOT committed until every external store
       has succeeded. Any failure rolls it back, which is what keeps this
       operation RETRYABLE: the document stays listed and the admin can press
       delete again. Committing first would leave orphaned files, vectors and
       cache entries behind with no row left to reach them through — a permanent
       and invisible leak (contract assertion 10).

    Retrying is safe because every external delete is idempotent: `unlink` uses
    `missing_ok`, the Qdrant filter delete matches nothing the second time, and
    `delete_by_any` no-ops on an empty list.
    """
    result = await session.execute(select(Document).where(Document.id == document_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="No such document")

    source_path = doc.source_path

    img_result = await session.execute(
        select(DocumentImage.image_id).where(DocumentImage.document_id == document_id)
    )
    image_ids = img_result.scalars().all()

    vector_store = QdrantVectorStore()

    # Read while the points still exist — see docstring (1).
    chunk_ids = await vector_store.get_chunk_ids_for_document(
        collection=settings.QDRANT_COLLECTION,
        document_id=str(document_id),
    )

    # Issued, not committed — see docstring (2). The document_image rows go with
    # it through the FK's ON DELETE CASCADE (app/core/models/document_image.py),
    # so they need no separate statement.
    await session.execute(delete(Document).where(Document.id == document_id))

    try:
        await storage.delete(source_path)

        # DocumentImage stores only the SHA256 id, never the suffix, so a derived
        # artifact's extension is not recoverable from the row. Sweep the whole
        # allowed set rather than a hardcoded subset: adding an entry to
        # DERIVED_EXTENSIONS would otherwise start leaking files silently.
        derived_dir = Path(settings.UPLOAD_DIR) / "derived"
        for img_id in image_ids:
            for ext in DERIVED_EXTENSIONS:
                await storage.delete(str(derived_dir / f"{img_id}{ext}"))

        await vector_store.delete_by_filter(
            collection=settings.QDRANT_COLLECTION,
            filter_conditions={"document_id": str(document_id)},
        )

        await vector_store.delete_by_any(
            collection=settings.QDRANT_CACHE_COLLECTION,
            key="chunk_ids",
            values=chunk_ids,
        )
    except Exception:
        # Re-raised, never swallowed: a 204 here would report a deletion that
        # did not happen. The row survives the rollback, so a retry can finish.
        await session.rollback()
        raise

    await session.commit()
    # M8c: the only route-side change — announce the mutation so the list
    # refreshes (contract Forbidden 1: header only, no logic touched).
    return Response(status_code=204, headers={"HX-Trigger": "document-changed"})


@admin_router.post("/documents/{document_id}/reingest", status_code=202)
async def reingest_document(
    request: Request,
    document_id: uuid.UUID,
    user: UserSession = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Reset ingestion state and enqueue a fresh pipeline run.

    Returns 409 rather than 202 when a run is already outstanding. The guard
    covers `pending` as well as `running`: `pending` means a task is already
    queued and not yet picked up, so admitting it would enqueue the duplicate
    that assertion 8 exists to prevent. A silent 202 would also tell the caller
    a run was queued when none was.

    Known limit, stated rather than fixed: a document left `pending` by a broker
    failure cannot be re-triggered from this route. Recovering it needs a forced
    re-enqueue, which no contract covers yet.
    """
    result = await session.execute(select(Document).where(Document.id == document_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="No such document")

    if doc.ingestion_status in ("pending", "running"):
        return JSONResponse(
            status_code=409,
            content={"detail": f"Document ingestion is already {doc.ingestion_status}"},
        )

    doc.ingestion_status = "pending"
    doc.last_ingested_at = None
    await session.commit()

    ingest_document.delay(str(document_id), DEFAULT_ALLOWED_ROLES)

    # M8c: same one-header change as the delete route. The 409 path above
    # deliberately carries no header — nothing changed, a refresh would be a no-op.
    return Response(status_code=202, headers={"HX-Trigger": "document-changed"})

# Owner-only, not admin-only: primary-role assignment was removed from this stage
# (M9 D21 — the lazy Keycloak sync at app/services/auth.py:176 reverts it on the
# target's next login), and §2 grants Admin no other user-management capability,
# so an admin-but-not-owner has no business on this page at all.


async def _load_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """One user with `role` eagerly loaded — the row template reads `role.name`.

    selectinload is not an optimisation here: the template renders synchronously,
    so a lazy load at render time raises MissingGreenlet rather than emitting SQL.
    """
    result = await session.execute(
        select(User).options(selectinload(User.role)).where(User.id == user_id)
    )
    return result.scalar_one_or_none()


async def _set_admin_flag(
    request: Request,
    user_id: uuid.UUID,
    value: bool,
    actor: UserSession,
    session: AsyncSession,
) -> Response:
    """Shared body of grant/revoke: one flag, one row, one partial back."""
    target = await _load_user(session, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="No such user")

    # §2's Owner invariant is enforced in data by ck_owner_implies_admin, so
    # letting the constraint fire would surface as an IntegrityError — a 500,
    # which is a crash, not a refusal. Refuse in application code; the constraint
    # stays the backstop it was designed to be.
    if not value and target.is_owner:
        raise HTTPException(
            status_code=409,
            detail="The Owner's admin flag cannot be revoked (is_owner implies is_admin)",
        )

    # Only a real change purges. An idempotent grant that logs the target out is
    # a defect (contract Forbidden 7) — the stored session value is still correct.
    if target.is_admin != value:
        target.is_admin = value
        await session.commit()
        # The purge is the point of the write: UserSession carries is_admin into
        # §5's filter dimension, so PostgreSQL alone leaves the old scope live for
        # up to SESSION_TTL_SECONDS (D22). Keyed on the target's id, never the
        # actor's — Forbidden 8.
        await purge_user_sessions(request.app.state.redis, str(target.id))

    return templates.TemplateResponse(
        request, "partials/user_row.html", {"user": actor, "row_user": target}
    )


@admin_router.get("/users")
async def list_users(
    request: Request,
    user: UserSession = Depends(require_owner),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Owner-only user list. Full page, or partials/user_list.html on HX-Request.

    Ordered by email: the column is unique and NOT NULL, so it is a total order
    with no tie-break needed and no NULL case to place (contrast M8's ordering,
    where `last_ingested_at` is nullable).
    """
    result = await session.execute(
        select(User).options(selectinload(User.role)).order_by(User.email.asc())
    )
    users = result.scalars().all()

    context = {"user": user, "users": users}

    if request.headers.get("hx-request", "").lower() == "true":
        return templates.TemplateResponse(request, "partials/user_list.html", context)
    return templates.TemplateResponse(request, "pages/admin/users.html", context)


@admin_router.post("/users/{user_id}/admin")
async def grant_admin(
    request: Request,
    user_id: uuid.UUID,
    user: UserSession = Depends(require_owner),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Set is_admin = True on the target. Returns partials/user_row.html."""
    return await _set_admin_flag(request, user_id, True, actor=user, session=session)


@admin_router.delete("/users/{user_id}/admin")
async def revoke_admin(
    request: Request,
    user_id: uuid.UUID,
    user: UserSession = Depends(require_owner),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Set is_admin = False on the target. Returns partials/user_row.html.

    Refuses with 409 when the target has is_owner = True.
    """
    return await _set_admin_flag(request, user_id, False, actor=user, session=session)


@admin_router.post("/users/{user_id}/role")
async def assign_role(
    request: Request,
    user_id: uuid.UUID,
    role_id: str = Form(...),
    actor: UserSession = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Change the primary role of a target user. Returns partials/user_row.html.

    Implementation of M9d. Gated by require_admin.
    Sets role_source = 'admin_assigned' and purges sessions on change.
    """
    try:
        parsed_role_id = uuid.UUID(str(role_id))
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid role ID") from exc

    # 1. Target validation
    target = await _load_user(session, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="No such user")

    # 2. Role validation
    role_result = await session.execute(select(Role).where(Role.id == parsed_role_id))
    if role_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="No such role")

    # 3. Idempotent update & session purge
    if target.role_id != parsed_role_id:
        from app.core.models.user import ROLE_SOURCE_ADMIN_ASSIGNED
        target.role_id = parsed_role_id
        target.role_source = ROLE_SOURCE_ADMIN_ASSIGNED
        await session.commit()

        # Purge only the target's sessions, never the actor's (§12 Forbidden 2).
        await purge_user_sessions(request.app.state.redis, str(target.id))
        # Refresh target to ensure any potential DB-level changes are loaded.
        target = await _load_user(session, user_id)

    return templates.TemplateResponse(
        request, "partials/user_row.html", {"user": actor, "row_user": target}
    )
