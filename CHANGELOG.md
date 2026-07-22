# CHANGELOG

All notable changes to the Whitecape Knowledge Assistant project.

---

## [2026-07-22] — Independent Review & Coherence Fixes

**Scope:** Full re-review of the pipeline after the previous verification round was
run by the same agent that generated the code (the independent Evaluator was
unavailable). This pass caught real defects the self-review missed, plus
documentation drift.

**Pipeline / node fixes:**

| Severity | Issue | Fix |
|---|---|---|
| Critical | Node 00: `vector_store.search()` failure propagated uncaught — a Qdrant outage crashed the whole pipeline at the first node | Wrapped in try/except, fail-open to cache miss |
| High | Node 01: missing/empty query raised a raw `KeyError` instead of the contracted `ClassificationError` | Added explicit validation; added 4 missing contract tests (9 total) |
| High | Node 05b: "query broadening" merely reset to the original query, contradicting spec + commit message | Rewrote to broaden via `REWRITER_MODEL` with fail-open; removed dead `retry_pass` state field |

**Service-layer fixes (`app/services/`, not covered by mocked unit tests):**

| Severity | Issue | Fix |
|---|---|---|
| High | `QdrantVectorStore.search` built a malformed `ChunkPayload` (non-existent `metadata=` key, missing `original_filename`/`category`/`page_number`/`chunk_id`) — every citation would render `(source: unknown)` | Build the full `ChunkPayload`; map cache answers from `answer_text` |
| High | Role filter hardcoded to `allowed_roles` for both collections; `semantic_cache` uses a `role` field (CLAUDE.md §9) | Made the filter key schema-aware per collection |

**Documentation:**
- `DEVLOG.md` rewritten as the clean handoff doc: all 9 nodes + `graph.py` now
  reflected in the directory tree; added §8 pipeline-implementation reference;
  clarified that loop-engineering files are gitignored process tooling.
- `recommendations.md` and `README_status.md` untracked (pre-decision drafts that
  contradict the locked spec; kept locally, excluded from the repo).
- Added top-level `README.md`.

**All 57 tests pass (51 unit + 6 integration).**

---

## [2026-07-20] — Graph Integration

**Scope:** Wired all 9 nodes into LangGraph StateGraph with conditional routing. Final piece of the MVP pipeline.

**Created:**
- `app/pipeline/graph.py` — `build_pipeline(nodes)`, 4 routing functions, compiled StateGraph
- `tests/integration/test_graph.py` — 6 integration tests covering all routing paths
- `reviews/graph_integration_summary.md` — implementation review
- `contracts/graph_integration.md` — formal contract

**Routing paths verified:**
- Cache hit → skips nodes 1-7
- DIRECT → short-circuits, no retrieval
- SIMPLE_RAG → full 9-node pipeline
- Retry loop → single attempt, then honest fallback
- Faithfulness failure → error flagged in state

**MVP pipeline is now complete end-to-end.**

---

## [2026-07-20] — Verification & Fixes

**Scope:** Systematic cross-check of all 9 nodes against contracts, CLAUDE.md constraints, and each other.

**Issues found and fixed:**

| Severity | Issue | Fix |
|---|---|---|
| Critical | Node 06: hardcoded `"employee"` fallback for `user_role` (violates CLAUDE.md §12) | Changed to `state["user_role"]` — no default |
| Critical | Node 04: `zip()` silently dropped chunks if reranker returned fewer scores | Added length validation, raises `RetrievalError` |
| Critical | `.env.example` missing `QDRANT_SEARCH_LIMIT` and `FAITHFULNESS_THRESHOLD` | Added both entries |
| Medium | Node 03: embedder failure propagated uncaught | Wrapped in try/except, raises `RetrievalError` with context |
| Medium | Node 04: reranker failure propagated uncaught | Wrapped in try/except, raises `RetrievalError` with context |
| Medium | Node 02 contract: assertion said `sub_queries` always set, but implementation skips on DIRECT | Fixed contract assertion to match implementation |
| Medium | `MockVectorStore` constructor treated `results=[]` as falsy, fell back to default | Changed `results or [make_chunk()]` to `results if results is not None else [make_chunk()]` |

**All 44 tests pass after fixes.**

---

## [2026-07-20] — Node 00: Cache Check

**Scope:** Semantic cache lookup before pipeline execution. BGE-M3 embeddings + Qdrant with role-scoped filtering.

**Created:**
- `app/pipeline/nodes/node_00_cache_check.py` — `CacheCheckNode(BaseNode)`, 0.92 similarity threshold
- `tests/unit/test_node_00_cache.py` — 5 tests all passing
- `reviews/node_00_cache_summary.md` — implementation review
- `contracts/node_00_cache.md` — formal contract

**Architecture:** Embeds query, searches `semantic_cache` collection with `[user_role]` filter. Hit returns cached answer immediately (~50ms). Miss proceeds to pipeline. Fail-open: embedder failure → miss.

**Evaluator verdict:** PASS (5/5 tests).

---

## [2026-07-20] — Node 07: Faithfulness Checker

**Scope:** Token-overlap safety check on generated answers. Verifies claims are grounded in source chunks.

**Created:**
- `app/pipeline/nodes/node_07_faithfulness.py` — `FaithfulnessNode(BaseNode)`, per-claim token overlap algorithm
- `tests/unit/test_node_07_faithfulness.py` — 6 tests all passing
- `reviews/node_07_faithfulness_summary.md` — implementation review
- `contracts/node_07_faithfulness.md` — formal contract
- Added `FAITHFULNESS_THRESHOLD` to settings (default 0.5)

**Algorithm:** Splits answer into claims, computes word overlap with chunk vocabulary. Claim grounded if >=30% overlap. Overall faithful if >=50% claims grounded. Zero LLM cost.

**Evaluator verdict:** PASS (6/6 tests).

---

## [2026-07-20] — Node 06: Generator

**Scope:** Generates cited, role-framed answers from retrieved chunks. Fully buffered — held in memory until faithfulness check passes.

**Created:**
- `app/pipeline/nodes/node_06_generator.py` — `GeneratorNode(BaseNode)`, role-adaptive prompts, inline citations
- `tests/unit/test_node_06_generator.py` — 5 tests all passing
- `reviews/node_06_generator_summary.md` — implementation review
- `contracts/node_06_generator.md` — formal contract

**Architecture:** System prompt includes role persona + citation rules + anti-hallucination constraints. Chunks formatted as numbered excerpts with source filenames. Empty chunks → honest fallback. LLM failure → GenerationError.

**Evaluator verdict:** PASS (5/5 tests).

---

## [2026-07-20] — Node 05b: Retry

**Scope:** One-shot retry on relevance gate failure. Falls back to original query (broader), idempotent guard prevents loops.

**Created:**
- `app/pipeline/nodes/node_05b_retry.py` — `RetryNode(BaseNode)`, 23 lines
- `tests/unit/test_node_05b_retry.py` — 3 tests all passing
- `reviews/node_05b_retry_summary.md` — implementation review
- `contracts/node_05b_retry.md` — formal contract

**Evaluator verdict:** PASS (3/3 tests).

> **Superseded 2026-07-22:** this original version only reset the query to the
> original. It was rewritten to broaden via `REWRITER_MODEL` (see the top entry).

---

## [2026-07-20] — Node 05: Relevance Gate

**Scope:** Threshold check on top reranker score. Pure Python, zero dependencies.

**Created:**
- `app/pipeline/nodes/node_05_relevance_gate.py` — `RelevanceGateNode(BaseNode)`, 14 lines
- `tests/unit/test_node_05_relevance_gate.py` — 5 tests all passing
- `reviews/node_05_relevance_gate_summary.md` — implementation review
- `contracts/node_05_relevance_gate.md` — formal contract

**Logic:** If `max(chunk_scores) >= RELEVANCE_THRESHOLD` → pass to Generator. Otherwise → fail (triggers retry).

**Evaluator verdict:** PASS (5/5 tests).

---

## [2026-07-20] — Node 04: Reranker

**Scope:** Cross-encoder re-scoring of retrieved chunks for accurate relevance ranking.

**Created:**
- `app/pipeline/nodes/node_04_reranker.py` — `RerankerNode(BaseNode)`, sorts by score descending
- `tests/unit/test_node_04_reranker.py` — 5 tests all passing
- `reviews/node_04_reranker_summary.md` — implementation review
- `contracts/node_04_reranker.md` — formal contract

**Architecture:** bge-reranker-v2-m3 cross-encoder via Hugging Face TEI. Not Ollama (Ollama cannot run cross-encoder architectures). Scores (query, passage) pairs jointly — more accurate than vector similarity.

**Evaluator verdict:** PASS (5/5 tests).

---

## [2026-07-20] — Node 03: Qdrant Search

**Scope:** Embeds rewritten query via BGE-M3 and searches Qdrant with mandatory role filter.

**Created:**
- `app/pipeline/nodes/node_03_parallel_qdrant_search.py` — `QdrantSearchNode(BaseNode)`, single search MVP
- `tests/unit/test_node_03_search.py` — 5 tests all passing
- `reviews/node_03_search_summary.md` — implementation review
- `contracts/node_03_search.md` — formal contract
- Added `QDRANT_SEARCH_LIMIT` to settings

**Security:** Role filter `[user_role, "all"]` applied at Qdrant query time — hard boundary, no Python post-filtering.

**Evaluator verdict:** PASS (5/5 tests).

---

## [2026-07-20] — Node 02: Rewriter

**Scope:** Query reformulation for retrieval quality. Takes raw user question and produces an optimized search query.

**Created:**
- `app/pipeline/nodes/node_02_rewriter_decomposer.py` — `RewriterNode(BaseNode)`, SIMPLE_RAG path only
- `tests/unit/test_node_02_rewriter.py` — 5 tests all passing
- `reviews/node_02_rewriter_summary.md` — implementation review
- `contracts/node_02_rewriter.md` — formal contract

**How it works:** Expands abbreviations, adds company context, preserves intent. Skips on DIRECT classification. Fails-open to original query on LLM error.

**Evaluator verdict:** PASS (5/5 tests).

---

## [2026-07-20] — Node 01: Classifier

**Scope:** Pipeline's traffic controller — the first decision point every user query hits.

**Created:**
- `app/pipeline/nodes/node_01_classifier.py` — `ClassifierNode(BaseNode)`, binary DIRECT/SIMPLE_RAG routing
- `tests/unit/test_node_01_classifier.py` — 5 tests all passing
- `reviews/node_01_classifier_summary.md` — full implementation review
- `contracts/node_01_classifier.md` — formal contract (8 assertions, 8 test cases)

**How it works:** Every query enters `execute(state)` and gets classified as either `DIRECT` (chitchat/out-of-scope → canned response, pipeline stops) or `SIMPLE_RAG` (might be answerable → pipeline continues to Node 2 Rewriter).

Two modes:
- **LLM mode** (default): constrained-output prompt at `temperature=0.0` to `CLASSIFIER_MODEL`. 3-layer parser (exact match → substring fallback → default SIMPLE_RAG).
- **Manual mode** (`CLASSIFIER_MODEL=manual`): skips LLM, routes everything to `SIMPLE_RAG` for downstream node testing.

**Design:** Fail-open philosophy — any error/default routes to `SIMPLE_RAG`. Dependency injection via constructor (no global imports). Immutable state updates (`{**state, ...}`).

**Evaluator verdict:** PASS (27/27 assertions, 5/5 tests).

---

## [2026-07-17] — Session-Based OIDC Auth

- Rewrote `app/core/security.py`: new ABC methods `get_authorization_url`, `handle_callback`, `get_current_user`, `logout`. `UserSession` dataclass replaces `TokenClaims`.
- Rewrote `app/services/auth.py`: `KeycloakAuthService` using `authlib` with PKCE. Redis sessions. Lazy user/role sync.
- Rewrote `app/main.py`: FastAPI lifespan, CSRF middleware, Jinja2 templates, auth routes.
- Created `app/templates/` and `app/static/css/base.css`.
- Added `SECRET_KEY`, `SESSION_TTL_SECONDS` to settings.

---

## [2026-07-16] — HTMX Monolith Migration

- Removed Nginx and Next.js frontend.
- Single FastAPI process serves HTML + API + SSE via Uvicorn.
- Session-based OIDC auth (authlib, PKCE, HTTP-only cookie, Redis sessions).
- Rewrote sequence diagrams (seq_1b, seq_2, seq_3) to remove proxy lifelines.
- Docker Compose: 11 services → 9 services.

---

## [2026-07-15] — Service Layer Skeleton

- `app/services/auth.py` — KeycloakAuthService
- `app/services/llm.py` — LiteLLMService
- `app/services/embedder.py` — OllamaEmbedder (BGE-M3)
- `app/services/reranker.py` — TEIReranker (bge-reranker-v2-m3)
- `app/services/vector_store.py` — QdrantVectorStore (hybrid search + role filter)
- `app/core/logging.py` — structlog configuration
- `tests/conftest.py` — state factories + mock fixtures

---

## [2026-07-14] — Core Skeleton + Diagrams

- Full `app/core/` layer: ABCs, models, state, settings, exceptions.
- Docker Compose (9 services), Dockerfile, pyproject.toml, .env.example, alembic.ini.
- Loop engineering files: run_loop.sh, feature_list.json, progress.md, log.md.
- `app/main.py` with /health endpoint.
- `app/tasks/` — Celery worker, ingestion, cache cleanup.
- 5 sequence diagrams, 3 use case diagrams, 8 class diagrams.

---

## [2026-07-14] — Initial Commit

Repository initialization: .gitignore, README, initial diagrams directory.
