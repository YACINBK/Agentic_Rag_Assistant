# Search Route + SSE Streaming + Pipeline Factory

**File:** `app/api/routes/search.py`, `app/pipeline/factory.py` · **LLM:** all (via factory) · **External services:** Redis, Qdrant, Ollama, TEI (via pipeline)

---

## 1. Role

Three endpoints that drive the search feature. `POST /search` accepts the query
and returns an SSE connection partial. `GET /search/stream` runs the full pipeline
and yields progress + answer events as HTML fragments. `GET /search` renders the
page shell.

Pipeline factory (`app/pipeline/factory.py`) is the composition root — the single
place all 9 nodes are instantiated with their service dependencies.

## 2. Endpoints

### `GET /search/`

Renders the search page. Full page on browser GET, partial on HTMX request (per
CLAUDE.md §17 rule 5). Requires auth via `require_auth`.

### `POST /search/`

Accepts `query` form field, validates non-empty (after strip), generates a UUID4
hex `qid`, stores query in Redis under `query:{qid}` with 60s TTL, returns
`partials/search_results.html` with the SSE connection URL.

- Empty query → `400 {"detail": "Query cannot be empty"}`
- Never runs the pipeline — that happens in `/stream`

### `GET /search/stream?qid={qid}`

SSE endpoint via `sse_starlette.sse.EventSourceResponse`. Runs the full pipeline
to completion (`pipeline.ainvoke(state)`), then emits the answer as ONE event
after the faithfulness check — never before (CLAUDE.md §12).

**Event sequence:**
1. `progress` — "Searching documents…" (immediately after query lookup)
2. `answer` OR `error` — depending on pipeline result
3. `done` — always last (empty data, signals client to close)

**Answer resolution (post-pipeline):**
```python
answer = result.get("cached_answer") or result.get("direct_response")
sources: list[str] = []
if not answer and result.get("is_faithful") and result.get("generated_answer"):
    answer = result["generated_answer"]
    sources = sorted({c.get("original_filename", "unknown") for c in result.get("reranked_chunks", [])})
```

Three sources of "answer":
- **Cache hit** (Node 0): `cached_answer` — no faithfulness check needed, cached answers were already verified when originally generated
- **DIRECT classification** (Node 1): `direct_response` — canned response, no retrieval, no faithfulness check
- **Generated + faithful** (Nodes 2-7): `generated_answer` only when `is_faithful=True`

## 3. Query ID (qid) pattern

Query text is stored in Redis, not in the SSE URL. Reasons:
- Long queries would need URL encoding
- Special characters (Unicode, punctuation) cause encoding issues
- Cleaner logs and SSE URLs
- 60s TTL auto-cleans expired queries

```
POST /search/  → generate qid = uuid.uuid4().hex
             → SETEX query:{qid} 60 "user's question"
             → return partial with sse-connect="/search/stream?qid={qid}"

GET /search/stream?qid={qid}
             → GET query:{qid}
             → if None → error event "Query expired"
             → else run pipeline
```

## 4. Pipeline factory

`build_pipeline_nodes(settings) → dict[str, BaseNode]` instantiates all services
once, injects them into the 9 nodes, and returns the node dict.

`get_compiled_pipeline(settings) → CompiledStateGraph` calls the factory and
compiles the LangGraph StateGraph via `build_pipeline(nodes)`.

**Service instances (all singletons per pipeline build):**
- `LiteLLMService` — all LLM calls
- `OllamaEmbedder` — BGE-M3 embeddings
- `QdrantVectorStore` — hybrid search + role filter
- `TEIReranker` — bge-reranker-v2-m3 cross-encoder

**Node wiring:**
- `cache_check`: embedder, vector_store, settings
- `classifier`: llm, settings
- `rewriter`: llm, settings
- `qdrant_search`: embedder, vector_store, settings
- `reranker`: reranker, settings
- `relevance_gate`: settings
- `retry`: **llm, settings** (fixed — retry uses REWRITER_MODEL to broaden queries)
- `generator`: llm, settings
- `faithfulness`: settings

## 5. Buffered answer discipline

CLAUDE.md §12 hard constraint: **do not stream before faithfulness verdict.**

The pipeline's `ainvoke()` runs synchronously (from the caller's perspective) —
the generator writes the full answer into `state["generated_answer"]`, the
faithfulness node reads it and sets `state["is_faithful"]`. Only after both
complete does the SSE endpoint emit the `answer` event.

If `is_faithful=False`, the answer is discarded and an `error` event is sent
with a safe fallback ("I could not find sufficient information…").

## 6. Test coverage

8 tests in `tests/unit/test_search_route.py`:
- `GET /search` renders + requires auth
- `POST /search` stores query in Redis + returns SSE partial
- `POST /search` rejects empty query
- SSE stream emits `answer` on faithful result
- SSE stream emits `error` on unfaithful result
- SSE stream emits `error` on pipeline exception
- SSE stream always ends with `done` event

Pipeline is mocked via `patch("app.api.routes.search.get_compiled_pipeline")` —
the factory is not exercised in unit tests, but is exercised at runtime and by
the integration test suite when services are available.
