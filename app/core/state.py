"""Pipeline state — the single TypedDict that flows through every LangGraph node.

Every key used by any node is defined here. No node may invent new keys.
State factory functions in tests/conftest.py build instances of this type.
"""

from __future__ import annotations

from typing import NotRequired, TypedDict


class ChunkPayload(TypedDict):
    """A single retrieved chunk with its metadata and scores."""

    chunk_id: str
    text: str
    document_id: str
    original_filename: str
    category: str
    page_number: int
    chunk_index: int
    score: float  # vector similarity or reranker score

    # --- Written by the ingestion pipeline; absent on pre-ingestion chunks ---
    section_path: NotRequired[str]  # "Chapter > Page > sub-section" breadcrumb
    anchor: NotRequired[str]  # bkmrk-*/page-* deep-link target; "" if absent
    image_refs: NotRequired[list[dict]]  # [{image_id, anchor}] for this chunk

    # --- BookStack provenance and Stage C enrichment ---
    #
    # These were written into the Qdrant payload by app/ingestion/index.py and then
    # dropped on the floor: QdrantVectorStore.search built a ChunkPayload without
    # them, so every retrieval paid the ingestion cost and discarded the result.
    # Projected here so the data is reachable; no node reads them yet, and that is
    # deliberate — the Investigator's candidate tools (query-time use of
    # applicability metadata, and a source-section fetch) are what consume them,
    # and they need the handles to exist before they can be built.
    #
    # Applicability must NOT become an exclusive filter that discards untagged
    # material. `applies_to` holds module names, never roles — it is not a security
    # field and carries no authorization meaning.
    page_id: NotRequired[str]
    page_title: NotRequired[str]
    chapter_id: NotRequired[str]
    chapter_title: NotRequired[str]
    related_anchors: NotRequired[list[str]]
    table_count: NotRequired[int]
    doc_hash: NotRequired[str]  # corpus-version anchor for the owning document
    applies_to: NotRequired[list[str]]  # module names — NOT roles
    contextual_header: NotRequired[str]  # prepended to the embedded text; debug only
    is_generalized_procedure: NotRequired[bool]

    # `admin_only` is projected; `allowed_roles` is deliberately NOT, and the
    # asymmetry is a decision rather than an oversight.
    #
    # admin_only has a named consumer in waiting: D5 deferred the precise cache
    # rule — `any(chunk.admin_only for chunk in chunks_used)`, so an admin's answer
    # grounded purely in unrestricted chunks stays shareable — explicitly because
    # the field was absent from ChunkPayload. This unblocks it.
    #
    # allowed_roles has no consumer, and putting it within reach of Python invites
    # exactly the post-retrieval filtering CLAUDE.md §12 forbids. The §5 boundary is
    # enforced inside the vector query; code that can read the scope downstream is
    # code that can be tempted to re-check it there, badly. Neither field may be
    # used to filter results after retrieval.
    admin_only: NotRequired[bool]

    # --- Cache-only: a semantic_cache hit carries the citations the original
    # answer was generated with, so the cached path renders with the same
    # hover cards, sources and images as the full pipeline (found live
    # 2026-08-31: text-only cache hits served raw [N] markers, no images). ---
    citations: NotRequired[list[dict]]


class Citation(TypedDict):
    """A resolved [N] marker — one cited chunk, what the hover card renders."""

    index: int  # the N in [N], 1-based
    chunk_id: str  # Qdrant point id of the cited chunk
    document_id: str  # owning document — the hover card's image URLs need it
    text: str  # the chunk's own text — what the hover card shows
    section_path: str  # "Chapter > Page > sub-section" breadcrumb; "" if absent
    anchor: str  # bkmrk-*/page-* deep-link target; "" if absent
    image_refs: list[dict]  # [{image_id, anchor}] belonging to this chunk
    original_filename: str


class PipelineState(TypedDict, total=False):
    """LangGraph state passed between all pipeline nodes.

    Every field is optional (total=False) so nodes only set what they own.
    Downstream nodes must check presence before reading.
    """

    # --- Input (set once at pipeline entry) ---
    query: str  # original user query
    user_id: str  # UUID from JWT
    user_role: str  # primary role name from JWT
    user_email: str
    user_is_admin: bool  # is_admin flag from the session (§5 privilege tier)

    # --- Node 0: Cache Check ---
    cache_hit: bool
    cached_answer: str  # populated only on cache hit

    # --- Node 1: Classifier ---
    classification: str  # "DIRECT" | "SIMPLE_RAG" | "COMPLEX_RAG"

    # --- Node 2: Rewriter + Decomposer ---
    rewritten_query: str  # rewritten for retrieval quality
    sub_queries: list[str]  # decomposed queries (COMPLEX_RAG only, post-MVP)

    # --- Node 3: Parallel Qdrant Search ---
    retrieved_chunks: list[ChunkPayload]

    # --- Node 4: Reranker ---
    reranked_chunks: list[ChunkPayload]  # sorted by reranker score descending

    # --- Node 5: Relevance Gate ---
    relevance_pass: bool

    # --- Node 5b: Retry ---
    retry_attempted: bool

    # --- Node 6: Generator ---
    generated_answer: str  # BUFFERED — not streamed
    citations: list[Citation]  # one entry per validated [N] marker, ordered by index

    # --- Node 7: Faithfulness ---
    is_faithful: bool
    faithfulness_score: float

    # --- Pipeline metadata ---
    direct_response: str  # canned response for DIRECT path
    error: str  # set if pipeline fails unexpectedly
    audit_log_id: str  # UUID after AuditLog INSERT
    escalation_id: str  # UUID after EscalationEvent INSERT (faithfulness fail)
