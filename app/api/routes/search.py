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

# Progress label per pipeline node, keyed by the LangGraph node name registered in
# app/pipeline/graph.py:65-73. Emitted when the node STARTS, so the line always
# names work in flight rather than work already finished.
#
# Sourced from `stream_mode="debug"`, whose `type: "task"` events fire at task
# schedule time. Deliberately not derived from `stream_mode="updates"` (which
# fires on node *completion*): mapping completion → "what runs next" would have to
# re-derive the conditional routing at graph.py:80-111 — cache hit, DIRECT, and
# the gate's pass/retry/exhausted fork — in a second place that could drift from
# the graph. Node-start events need no routing knowledge at all.
#
# This does NOT relax §12. What streams here is the name of the stage; the answer
# stays buffered until FAITHFUL, and is still emitted as one `answer` event below.
# The route already sent an SSE progress frame before the pipeline ran, so §12's
# boundary was never "no SSE at all before the verdict" — it is "no answer".
_STAGE_LABELS: dict[str, str] = {
    "cache_check": "Checking for a cached answer&hellip;",
    "classifier": "Understanding the question&hellip;",
    "rewriter": "Refining the search query&hellip;",
    "qdrant_search": "Searching indexed documents&hellip;",
    "reranker": "Ranking passages by relevance&hellip;",
    "relevance_gate": "Weighing whether the evidence is strong enough&hellip;",
    "retry": "Retrying with a broader query&hellip;",
    "generator": "Composing the answer&hellip;",
    "faithfulness": "Verifying every claim against the sources&hellip;",
}

# A bracketed run of digits — the [N] citation marker the generator emits.
_MARKER_RE = re.compile(r"\[(\d+)\]")

# The ingestion-internal image anchor: chunk.py writes " [[image:<sha256>]] " into
# the chunk TEXT so the enricher can locate figures. It is not meant for humans and
# must never surface in the UI. It is stripped HERE — the single place chunk text is
# rendered (source_card.html, via the citation macro) — rather than in node 06, whose
# contract asserts citation.text == chunk.text. The picture still renders: <img> tags
# come from cite.image_refs, not from this marker. D35.
_IMAGE_MARKER_RE = re.compile(r"\s*\[\[image:[0-9a-f]{64}\]\]\s*")


def _strip_image_markers(text: str) -> str:
    return _IMAGE_MARKER_RE.sub(" ", text).strip()


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
    by_index = {
        cite["index"]: str(citation_macro({**cite, "text": _strip_image_markers(cite["text"])}))
        for cite in citations
    }

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

        # Neutral opener, not a stage name: it covers only the window before the
        # first node starts (pipeline compile on a cold call). `cache_check`
        # replaces it within milliseconds, so wording it as a stage — the old
        # "Searching documents…" — read as the progress line rewinding.
        yield {
            "event": "progress",
            "data": '<div class="progress">Starting&hellip;</div>',
        }

        state: PipelineState = {
            "query": query,
            "user_id": user.user_id,
            "user_role": user.role,
            "user_email": user.email,
            "user_is_admin": user.is_admin,
        }

        # `astream` over `ainvoke` purely so the stage line can advance while the
        # pipeline runs — a single `await` cannot yield, which is why one static
        # "Searching documents…" used to sit there for the whole 2–4s+ (longer on a
        # 32b generator). `values` carries the accumulated state, so the last one
        # is exactly what `ainvoke` would have returned; `debug` carries the
        # node-start events that drive _STAGE_LABELS. Nothing from a node's own
        # payload is ever emitted — only the label for its name (§12).
        result: PipelineState = {}
        try:
            pipeline = get_compiled_pipeline(settings)
            async for mode, payload in pipeline.astream(state, stream_mode=["debug", "values"]):
                if mode == "values":
                    result = payload
                    continue
                if payload.get("type") != "task":
                    continue
                label = _STAGE_LABELS.get(payload.get("payload", {}).get("name", ""))
                if label:
                    yield {"event": "progress", "data": f'<div class="progress">{label}</div>'}
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
