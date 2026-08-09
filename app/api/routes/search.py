"""Search routes — page render, query submit, and SSE streaming endpoint.

Flow: POST /search stores the query in Redis and returns an HTMX partial that
opens an SSE connection to GET /search/stream. The stream runs the pipeline to
completion (buffered), then emits the answer as ONE event after the faithfulness
check — never before (CLAUDE.md §12).
"""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sse_starlette.sse import EventSourceResponse

from app.api.dependencies import require_auth
from app.core.security import UserSession
from app.core.settings import settings
from app.core.state import PipelineState
from app.pipeline.factory import get_compiled_pipeline

search_router = APIRouter(prefix="/search", tags=["search"])
templates = Jinja2Templates(directory="app/templates")

QUERY_PREFIX = "query:"
QUERY_TTL_SECONDS = 60

_FALLBACK_HTML = (
    '<div class="error-container"><p>I could not find sufficient information '
    "in the available documents to answer your question.</p></div>"
)

# A bracketed run of digits — the [N] citation marker the generator emits.
_MARKER_RE = re.compile(r"\[(\d+)\]")


def markup_answer(answer: str, citations: list[dict]) -> Markup:
    """Escape the answer, then splice a rendered citation marker over each [N]
    that has a matching Citation.

    Three constraints, all from the contract's substitution seam:
    - Escape FIRST (markupsafe.escape), then substitute — chunk- and model-derived
      prose is neutralised before any trusted markup is inserted.
    - Only markers with a Citation are replaced; an unmatched [7] survives as
      literal text (never rewritten, never dropped).
    - `_MARKER_RE` matches the exact bracketed token, so [1] cannot corrupt [11].
    """
    escaped = str(escape(answer))
    citation_macro = templates.get_template("components/citation.html").module.citation
    by_index = {cite["index"]: str(citation_macro(cite)) for cite in citations}

    def _replace(match: re.Match) -> str:
        rendered = by_index.get(int(match.group(1)))
        return rendered if rendered is not None else match.group(0)

    return Markup(_MARKER_RE.sub(_replace, escaped))


@search_router.get("/")
async def search_page(request: Request, user: UserSession = Depends(require_auth)):
    """Full page on browser GET, partial on HTMX request."""
    if request.headers.get("hx-request", "").lower() == "true":
        return templates.TemplateResponse(
            request, "partials/search_page_content.html", {"user": user}
        )
    return templates.TemplateResponse(request, "pages/search.html", {"user": user})


@search_router.post("/")
async def search_submit(
    request: Request,
    query: str = Form(...),
    user: UserSession = Depends(require_auth),
):
    """Store the query in Redis, return the SSE-connect partial. Never runs the pipeline here."""
    cleaned = query.strip()
    if not cleaned:
        return JSONResponse(status_code=400, content={"detail": "Query cannot be empty"})

    qid = uuid.uuid4().hex
    await request.app.state.redis.setex(f"{QUERY_PREFIX}{qid}", QUERY_TTL_SECONDS, cleaned)

    return templates.TemplateResponse(
        request,
        "partials/search_results.html",
        {"qid": qid, "query": cleaned, "user": user},
    )


@search_router.get("/stream")
async def search_stream(
    request: Request,
    qid: str,
    user: UserSession = Depends(require_auth),
):
    """SSE endpoint — runs the pipeline, yields progress → answer|error → done."""
    redis = request.app.state.redis

    async def event_generator():
        query = await redis.get(f"{QUERY_PREFIX}{qid}")
        if not query:
            yield {
                "event": "error",
                "data": '<div class="error-container"><p>Query expired. Please search again.</p></div>',
            }
            yield {"event": "done", "data": ""}
            return

        yield {
            "event": "progress",
            "data": '<div class="progress">Searching documents&hellip;</div>',
        }

        state: PipelineState = {
            "query": query,
            "user_id": user.user_id,
            "user_role": user.role,
            "user_email": user.email,
            "user_is_admin": user.is_admin,
        }

        try:
            pipeline = get_compiled_pipeline(settings)
            result = await pipeline.ainvoke(state)
        except Exception as e:
            yield {
                "event": "error",
                "data": f'<div class="error-container"><p>Search failed: {e}</p></div>',
            }
            yield {"event": "done", "data": ""}
            return

        # Pipeline complete — answer released ONLY after this point (§12).
        answer = result.get("cached_answer") or result.get("direct_response")
        sources: list[str] = []
        if not answer and result.get("is_faithful") and result.get("generated_answer"):
            answer = result["generated_answer"]
            sources = sorted(
                {c.get("original_filename", "unknown") for c in result.get("reranked_chunks", [])}
            )

        citations = result.get("citations", [])

        # Clear the progress indicator now that the pipeline has finished.
        yield {"event": "progress", "data": "<div></div>"}

        if answer:
            html = templates.get_template("components/message_bubble.html").render(
                answer=markup_answer(answer, citations), citations=citations, sources=sources
            )
            yield {"event": "answer", "data": html}
        else:
            yield {"event": "error", "data": _FALLBACK_HTML}

        yield {"event": "done", "data": ""}

    return EventSourceResponse(event_generator())
