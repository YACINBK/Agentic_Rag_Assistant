"""Image serving route — authenticated, junction-checked delivery of extracted images.

The security property is the JOIN. `doc_id` and `image_id` arrive as two
independent, user-controlled path parameters; checking `Document.restricted`
without joining `document_image` validates the WRONG row — an authenticated
non-admin could pair a valid unrestricted `doc_id` with a restricted `image_id`
and read the restricted image. Resolving ownership and privilege in one query
closes that window (§5 two-layer security; contract assertion 4).

The §5 Qdrant filter cannot help here: this request never touches Qdrant, it is a
filesystem read keyed by a content hash, so the boundary is rebuilt in this second
place.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_auth
from app.core.models.base import get_session
from app.core.models.document import Document
from app.core.models.document_image import DocumentImage
from app.core.security import UserSession
from app.core.settings import settings

router = APIRouter(tags=["images"])

# SHA256 hex — the only shape image_id may take. This guard is what makes the glob
# below safe: without it, image_id is an arbitrary path fragment and `../../` walks
# out of derived/.
_IMAGE_ID_RE = re.compile(r"^[0-9a-f]{64}$")


@router.get("/documents/{doc_id}/images/{image_id}")
async def get_image(
    doc_id: str,
    image_id: str,
    user: UserSession = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """Serve one extracted image, if this user may read its owning document."""
    # image_id must be a bare SHA256 hex BEFORE it reaches the filesystem. A failing
    # pattern is 404, never 400 — "no such image" and "malformed image id" return the
    # same status, so neither is a membership oracle.
    if not _IMAGE_ID_RE.match(image_id):
        raise HTTPException(status_code=404)

    # doc_id is taken as str, not UUID: a malformed UUID must 404, not 422. A 422 body
    # distinguishes "malformed" from "not found" and leaks the same membership signal
    # 404-vs-403 does (contract Forbidden).
    try:
        doc_uuid = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=404) from None

    # The JOIN is the security property, not an optimisation. It ties image_id to its
    # owning document so `restricted` is read off the row that actually owns the image.
    restricted = (
        await session.execute(
            select(Document.restricted)
            .join(DocumentImage, DocumentImage.document_id == Document.id)
            .where(
                DocumentImage.document_id == doc_uuid,
                DocumentImage.image_id == image_id,
            )
        )
    ).scalar_one_or_none()

    if restricted is None:  # no such (doc_id, image_id) pairing — 404, never 403
        raise HTTPException(status_code=404)
    # is_admin alone: is_owner implies is_admin by DB constraint (§2, §12), so
    # `or user.is_owner` would be redundant and is forbidden.
    if restricted and not user.is_admin:
        raise HTTPException(status_code=403)

    # The suffix is not in the URL — the citation payload carries the hash only, so
    # resolve the stored file by globbing derived/. Serve ONLY from derived/, never
    # UPLOAD_DIR/ itself (raw uploads).
    derived_dir = Path(settings.UPLOAD_DIR) / "derived"
    matches = sorted(derived_dir.glob(f"{image_id}.*"))
    if not matches:  # pairing exists but the file is absent from disk — 404, not 500
        raise HTTPException(status_code=404)

    # Cache-Control is `private`, never `public`: a shared cache holding a restricted
    # image would serve it to users who never passed the check — the same bypass,
    # moved into a proxy.
    return FileResponse(
        matches[0],
        headers={"Cache-Control": "private, max-age=3600"},
    )
