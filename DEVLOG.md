# DEVLOG — Whitecape Knowledge Assistant

Implementation log and architecture reference.
Specification lives in `CLAUDE.md` — this file documents what was **built**, not what was planned.

---

## 1. Project Overview

RAG-based internal knowledge assistant for Whitecape Technologies. Employees ask natural language questions, the system retrieves relevant chunks from indexed company documents via a multi-stage LangGraph pipeline, and streams a cited, role-aware answer back via SSE. Every answer is grounded in source documents and verified by a faithfulness checker before reaching the user.

---

## 2. Architecture Summary

The codebase follows a **core-abstraction pattern** inspired by Java's interface-first design:

```
app/core/           Abstract contracts (ABCs, Protocols, shared types)
    |
    ├── base_node.py          BaseNode ABC — every pipeline node extends this
    ├── services/             Service ABCs — LLM, vector store, reranker, embedder
    ├── models/               SQLAlchemy ORM models (the concrete DB schema)
    ├── state.py              PipelineState TypedDict + ChunkPayload
    ├── settings.py           Pydantic Settings — single env config source
    ├── security.py           BaseAuthService ABC + UserSession dataclass
    ├── logging.py            structlog configuration
    └── exceptions.py         Typed exception hierarchy
         |
         ▼
app/services/        Concrete implementations (LiteLLMService, QdrantVectorStore, ...)
app/pipeline/nodes/  Concrete BaseNode subclasses (one per pipeline stage)
app/api/             FastAPI routes — depend on core interfaces only
app/tasks/           Celery async tasks (ingestion, cache cleanup)
```

**Why this pattern:**
- **Testability** — mocks replace concretes at the ABC boundary. `MockLLMService(BaseLLMService)` in tests, `LiteLLMService(BaseLLMService)` in production.
- **No vendor lock** — CLAUDE.md §4 mandates all LLM calls through LiteLLM, but the ABC layer means swapping providers is a single class replacement.
- **Loop engineering** — contract-driven development (CLAUDE.md §11) defines interfaces first. The ABCs *are* the programmatic form of those contracts.

**Delivery model:** HTMX + Jinja2 monolith. FastAPI serves HTML, API, and SSE from a single process. No separate frontend. No reverse proxy needed for internal deployment — Uvicorn serves directly on port 8000.

---

## 3. Directory Structure

```
rag_assistant/
├── CLAUDE.md                        # Specification (§0–§16) — source of truth
├── DEVLOG.md                        # This file — implementation log
├── CHANGELOG.md                     # Chronological changelog
├── Dockerfile                       # Backend container (Python 3.11, uvicorn)
├── docker-compose.yml               # Full stack: 9 services
├── pyproject.toml                   # Dependencies + tooling config (pytest, ruff)
├── .env.example                     # All env vars with defaults and comments
├── .gitignore                       # Python, secrets, IDE, state files
├── alembic.ini                      # Alembic migration config
├── run_loop.sh                      # Loop engineering automation (CLAUDE.md §11)
├── feature_list.json                # Node implementation tracker
├── log.md                           # Append-only evaluator results
├── progress.md                      # Current sprint state
│
├── app/
│   ├── main.py                      # FastAPI app: lifespan, CSRF, auth routes, templates, /health
│   ├── core/
│   │   ├── base_node.py             # BaseNode ABC: name property + execute(state) → state
│   │   ├── state.py                 # PipelineState TypedDict + ChunkPayload
│   │   ├── settings.py              # Pydantic Settings — model/service config from .env
│   │   ├── security.py              # BaseAuthService ABC + UserSession dataclass
│   │   ├── logging.py              # structlog processor chain config
│   │   ├── exceptions.py            # PipelineError, AuthenticationError, AuthorizationError, ...
│   │   ├── services/
│   │   │   ├── llm.py               # BaseLLMService ABC — complete(model, messages) → str
│   │   │   ├── vector_store.py      # BaseVectorStore ABC — search/upsert/delete with role filter
│   │   │   ├── reranker.py          # BaseReranker ABC — rerank(query, passages) → scores
│   │   │   └── embedder.py          # BaseEmbedder ABC — embed(texts) → vectors
│   │   └── models/
│   │       ├── base.py              # SQLAlchemy async engine + DeclarativeBase + get_session
│   │       ├── role.py              # Role lookup table (dynamic, not hardcoded)
│   │       ├── user.py              # User with is_admin/is_owner + partial unique index
│   │       ├── document.py          # Document with category enum + ingestion_status enum
│   │       ├── audit_log.py         # AuditLog — append-only, compliance requirement
│   │       └── escalation_event.py  # EscalationEvent — faithfulness/relevance failures
│   │
│   ├── services/                    # Concrete implementations of core ABCs
│   │   ├── __init__.py              # Re-exports all service classes
│   │   ├── auth.py                  # KeycloakAuthService — OIDC session-based auth via authlib
│   │   ├── llm.py                   # LiteLLMService — all LLM calls via litellm.acompletion
│   │   ├── embedder.py              # OllamaEmbedder — BGE-M3 embeddings via Ollama
│   │   ├── reranker.py              # TEIReranker — bge-reranker-v2-m3 via HTTP POST
│   │   └── vector_store.py          # QdrantVectorStore — hybrid search + role filter
│   │
│   ├── pipeline/
│   │   ├── __init__.py
│   │   ├── factory.py               # Composition root: build_pipeline_nodes + get_compiled_pipeline
│   │   ├── graph.py                 # LangGraph StateGraph — wires all 9 nodes + conditional routing
│   │   └── nodes/                   # One file per pipeline node (all implemented)
│   │       ├── __init__.py
│   │       ├── node_00_cache_check.py        # Semantic cache lookup (role-scoped, 0.92 threshold)
│   │       ├── node_01_classifier.py         # Binary DIRECT/SIMPLE_RAG routing
│   │       ├── node_02_rewriter_decomposer.py# Query rewrite for retrieval quality
│   │       ├── node_03_parallel_qdrant_search.py # Embed + role-filtered Qdrant search
│   │       ├── node_04_reranker.py           # bge-reranker-v2-m3 cross-encoder re-scoring
│   │       ├── node_05_relevance_gate.py     # Threshold check on top reranker score
│   │       ├── node_05b_retry.py             # One-shot query broadening on gate failure
│   │       ├── node_06_generator.py          # Buffered, cited, role-framed answer
│   │       └── node_07_faithfulness.py       # Token-overlap grounding check
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── dependencies.py              # require_auth, require_admin, require_owner
│   │   ├── error_handlers.py            # register_error_handlers() — client-aware AuthN/AuthZ responses
│   │   └── routes/
│   │       ├── __init__.py              # Exports search_router
│   │       └── search.py                # GET /search, POST /search, GET /search/stream (SSE)
│   │
│   ├── tasks/
│   │   ├── __init__.py
│   │   ├── worker.py                # Celery app creation + config
│   │   ├── ingestion.py             # Document ingestion task (async via Redis) — stub, Phase 4
│   │   └── cache_cleanup.py         # TTL-based semantic cache purge (Celery beat)
│   │
│   ├── templates/
│   │   ├── base.html                # Layout: nav, HTMX 2.0.4, SSE ext, blocks (head/content/footer)
│   │   ├── pages/
│   │   │   ├── login.html           # Keycloak sign-in (btn macro, no nav)
│   │   │   ├── dashboard.html       # User info, badges, search link
│   │   │   ├── search.html          # Search page (extends base, loads search.css)
│   │   │   └── 403.html             # Forbidden page
│   │   ├── partials/
│   │   │   ├── search_page_content.html  # Search bar + results swap target
│   │   │   ├── search_results.html       # SSE connection container (sse-swap divs)
│   │   │   └── 403.html                  # HTMX-swappable forbidden fragment
│   │   ├── components/
│   │   │   ├── search_bar.html      # Reusable search form (form_field + btn macros)
│   │   │   ├── message_bubble.html  # Answer bubble + source cards
│   │   │   └── source_card.html     # Single source filename display
│   │   └── macros/
│   │       ├── forms.html           # form_field(name, label, type, error, required, placeholder)
│   │       └── buttons.html         # btn(text, variant, type, href, **kwargs)
│   │
│   └── static/
│       ├── css/
│       │   ├── base.css             # Base styles, form fields, buttons, badges
│       │   └── pages/
│       │       └── search.css       # Message bubbles, source cards, progress, errors
│       └── js/                      # Client-side JS (empty, ready for use)
│
├── tests/
│   ├── conftest.py                  # Shared fixtures: state factories, mock services, auth mocks
│   ├── unit/                        # One test module per node + auth + frontend (17 modules)
│   └── integration/
│       ├── test_graph.py            # End-to-end pipeline routing tests
│       └── test_auth_flow.py        # Full OIDC chain: route → dependency → service → error handler
│
└── diags/                           # All architecture diagrams (.drawio format)
    ├── seq_1a_simple_rag_query.drawio
    ├── seq_1b_direct_and_cache.drawio
    ├── seq_1c_failure_paths.drawio
    ├── seq_2_document_ingestion.drawio
    ├── seq_3_authentication.drawio
    ├── uc_1a_core_query_flow_final.drawio
    ├── uc_1b_document_management.drawio
    ├── uc_1c_user_role_management.drawio
    ├── Audit_log_and_escalation.drawio
    ├── Auth_and_User_Session.drawio
    ├── Auth_and_User_Session (1).drawio
    ├── Cache_and_Rate_Limiting.drawio
    ├── Cache_and_Rate_Limiting (1).drawio
    ├── Document_and_ingestion.drawio
    ├── Document_and_ingestion (1).drawio
    ├── Identity_and_roles.drawio
    ├── Identity_and_roles (1).drawio
    ├── Pipeline_Node_Hierarchy_LangGraph.drawio
    ├── Pipeline_Node_Hierarchy_LangGraph (1).drawio
    ├── Pipeline_states.drawio
    ├── User_Query_Service_Layer.drawio
    └── User_Query_Service_Layer (1).drawio
```

---

## 4. Core Layer Reference

| File | Purpose |
|---|---|
| `base_node.py` | ABC with `name` property and `async execute(state) → state`. Every pipeline node inherits this. |
| `state.py` | `PipelineState` TypedDict — the single object flowing through all LangGraph nodes. `ChunkPayload` TypedDict for retrieved chunk metadata. |
| `settings.py` | `Settings(BaseSettings)` — loads all config from `.env`. Model names, URLs, thresholds. Single source of truth. |
| `security.py` | `BaseAuthService` ABC — `get_authorization_url`, `handle_callback`, `get_current_user`, `logout`. `UserSession` dataclass for parsed session data. |
| `logging.py` | structlog config — JSON output, bound context (user_id, request_id), stdlib integration. |
| `exceptions.py` | Typed hierarchy: `PipelineError` (base), `AuthenticationError`, `AuthorizationError`, `RetrievalError`, `GenerationError`, `FaithfulnessError`. |
| `services/llm.py` | `BaseLLMService` — `async complete(model, messages, **kwargs) → str` |
| `services/vector_store.py` | `BaseVectorStore` — `async search(query_vector, role, limit)`, `async upsert(points)`, `async delete(ids)` |
| `services/reranker.py` | `BaseReranker` — `async rerank(query, passages) → list[float]` |
| `services/embedder.py` | `BaseEmbedder` — `async embed(texts) → list[list[float]]` |

---

## 5. Database Models

All models map to CLAUDE.md §10. Key constraints:

| Model | Table | Key constraint |
|---|---|---|
| `Role` | `role` | Dynamic lookup table — roles are INSERTs, not code changes |
| `User` | `user` | `is_owner` partial unique index: `WHERE is_owner = true` (at most one owner) |
| `Document` | `document` | `doc_hash` unique — prevents duplicate ingestion |
| `AuditLog` | `audit_log` | **Append-only.** No UPDATE, no DELETE. Compliance requirement. |
| `EscalationEvent` | `escalation_event` | FK to AuditLog. Separate table — most queries never escalate. |

---

## 6. Infrastructure — Docker Compose Services

9 services, private internal network:

| Service | Image | Port | Purpose |
|---|---|---|---|
| `backend` | Custom (Dockerfile) | 8000 | FastAPI — serves HTMX UI + API + SSE directly via Uvicorn. Session middleware + CSRF. |
| `celery-worker` | Same image | — | Async task execution (ingestion, heavy processing) |
| `celery-beat` | Same image | — | Periodic tasks (cache TTL cleanup, hourly) |
| `postgres` | postgres:16-alpine | 5432 | Relational data (users, roles, documents, audit log) |
| `redis` | redis:7-alpine | 6379 | Session store + Celery broker + result backend |
| `qdrant` | qdrant/qdrant:v1.12.1 | 6333/6334 | Vector store (document chunks) + semantic cache |
| `keycloak` | keycloak:24.0 | 8080 | OIDC identity provider, session-based auth |
| `ollama` | ollama/ollama:latest | 11434 | Local LLM inference (Qwen2.5) + BGE-M3 embeddings |
| `reranker` | huggingface/tei:cpu | 8082 | bge-reranker-v2-m3 cross-encoder scoring |

No Nginx. No frontend container. Backend serves everything.

---

## 7. Diagrams Inventory

| File | Type | Description |
|---|---|---|
| `seq_1a_simple_rag_query.drawio` | Sequence | Full SIMPLE_RAG pipeline — 14 lifelines, nodes 0–7 |
| `seq_1b_direct_and_cache.drawio` | Sequence | Cache hit + DIRECT classification short-circuit paths |
| `seq_1c_failure_paths.drawio` | Sequence | Relevance gate failure, faithfulness failure, escalation |
| `seq_2_document_ingestion.drawio` | Sequence | Admin upload → Celery → chunk → embed → upsert → cache invalidation |
| `seq_3_authentication.drawio` | Sequence | OIDC Authorization Code + PKCE, session cookie, lazy sync |
| `uc_1a_core_query_flow_final.drawio` | Use Case | Owner > Admin > User hierarchy, query flow |
| `uc_1b_document_management.drawio` | Use Case | Admin document ops, Owner overrides |
| `uc_1c_user_role_management.drawio` | Use Case | Admin/Owner user management |
| `Audit_log_and_escalation.drawio` | Class | AuditLog + EscalationEvent relationships |
| `Auth_and_User_Session.drawio` | Class | Auth service, session, user model |
| `Cache_and_Rate_Limiting.drawio` | Class | Semantic cache collection, TTL, rate limiter |
| `Document_and_ingestion.drawio` | Class | Document model, ingestion pipeline, Celery tasks |
| `Identity_and_roles.drawio` | Class | Role table, User flags, permission model |
| `Pipeline_Node_Hierarchy_LangGraph.drawio` | Class | BaseNode inheritance tree, all 8 nodes |
| `Pipeline_states.drawio` | Class | PipelineState TypedDict fields |
| `User_Query_Service_Layer.drawio` | Class | Service ABCs and concrete implementations |

Files with `(1)` suffix are revision variants of the same diagram.

All diagrams: black strokes, white fill, plain text only. Editable at diagrams.net.

---

## 8. Pipeline implementation — all 9 nodes built

The full MVP pipeline is implemented and wired. Every node subclasses `BaseNode`
(`name` property + `async execute(state) → state`), takes its dependencies via
constructor injection, and returns a new merged state dict (never mutates input).
`app/pipeline/graph.py` composes them into a LangGraph `StateGraph` with conditional
routing. **57 tests pass (51 unit + 6 integration).**

| Node | Class | LLM | Behaviour |
|---|---|---|---|
| 00 Cache Check | `CacheCheckNode` | — | Embeds query, searches `semantic_cache` (role-scoped, 0.92 threshold). Hit → return cached answer, skip 1–7. Fail-open: embed **or** search failure → miss. |
| 01 Classifier | `ClassifierNode` | `CLASSIFIER_MODEL` | Binary DIRECT/SIMPLE_RAG. Raises `ClassificationError` on missing/empty query. Manual mode + fail-open to SIMPLE_RAG. |
| 02 Rewriter | `RewriterNode` | `REWRITER_MODEL` | Rewrites query for retrieval. Skips on DIRECT. Fail-open to original query. (Decomposition is post-MVP.) |
| 03 Qdrant Search | `QdrantSearchNode` | — | Embeds rewritten query, searches `documents` with role filter `[user_role, "all"]`. Failures raise `RetrievalError`. |
| 04 Reranker | `RerankerNode` | — | bge-reranker-v2-m3 via TEI. Re-scores + sorts descending. Validates score count matches chunk count. |
| 05 Relevance Gate | `RelevanceGateNode` | — | `relevance_pass = max(score) >= RELEVANCE_THRESHOLD`. Empty chunks → fail. |
| 05b Retry | `RetryNode` | `REWRITER_MODEL` | One-shot broadening of the failed query via LLM. Idempotent guard (`retry_attempted`) prevents loops. Fail-open to original query. |
| 06 Generator | `GeneratorNode` | `GENERATOR_MODEL` | Buffered, cited, role-framed answer from top chunks. Empty chunks → honest fallback. LLM failure → `GenerationError`. |
| 07 Faithfulness | `FaithfulnessNode` | — | Token-overlap grounding check on the buffered answer. Per-claim ≥30% overlap, overall `is_faithful = score >= FAITHFULNESS_THRESHOLD`. Zero LLM cost. |

**Routing (`graph.py`):**
- `cache_check` → hit `END` / miss `classifier`
- `classifier` → DIRECT `END` / SIMPLE_RAG `rewriter`
- `rewriter → qdrant_search → reranker → relevance_gate` (sequential)
- `relevance_gate` → pass `generator` / fail+not-retried `retry` / fail+retried `END`
- `retry → qdrant_search` (loops back once)
- `generator → faithfulness → END`

The faithfulness verdict (`is_faithful`) is surfaced in state; the SSE orchestrator
(Phase 3) decides stream-vs-escalate. The graph itself always terminates at `END`.

**Per-node reference:** `docs/pipeline/` contains one detailed document per node
(role, interface, state contract, behaviour, error handling, design rationale, test
coverage) plus an overview — see `docs/pipeline/README.md`.

---

## 9. Loop Engineering Setup

See CLAUDE.md §11 for the full specification. The loop-engineering artifacts
(`contracts/`, `reviews/`, `test_results/`, `feature_list.json`, `progress.md`,
`log.md`, `for_oc_*.md`, `run_loop.sh`) are **process tooling, not project deliverables**,
and are intentionally excluded from git (see `.gitignore`). They live on the
developer's machine to drive the Planner → Generator → Evaluator cycle.

| Path | Purpose |
|---|---|
| `run_loop.sh` | Automation script — Generator → pytest → Evaluator per node |
| `contracts/` | One `.md` per node + `conftest.md` — written by Planner |
| `reviews/` | Generator summary per node — written by Generator |
| `test_results/` | pytest output per node — written by `run_loop.sh` |
| `feature_list.json` | Tracks node status: done / in_progress / next / blocked |
| `progress.md` | Current sprint, active contract, last verified state |
| `log.md` | Append-only — Evaluator writes PASS/FAIL per node per run |
| `tests/conftest.py` | Shared fixtures — state factories, mock patterns for all nodes (tracked — it is test code) |

**Current status:** Phase 3b (frontend) complete — all 4 frontend modules verified,
112 tests passing (57 pipeline + 26 auth + 29 frontend). Next: Phase 4 (ingestion + admin).
See `CHANGELOG.md` for the chronological build log.

---

## 10. Auth layer — Phase 3a

The full session-based auth layer is implemented and wired. Keycloak OIDC Authorization
Code + PKCE, server-side sessions in Redis, HTTP-only cookie. **26 tests pass (20 unit + 6 integration).**

| Module | File | What |
|---|---|---|
| Dependency chain | `app/api/dependencies.py` | `require_auth → require_admin → require_owner` via `Depends()`. Raises `AuthenticationError` or `AuthorizationError`. |
| Error handlers | `app/api/error_handlers.py` | `register_error_handlers(app)` — detects HTMX / HTML / JSON client, returns redirect / 403 page / JSON accordingly. Wired in `app/main.py`. |
| Auth service | `app/services/auth.py` | `KeycloakAuthService` — full OIDC flow, PKCE, lazy PostgreSQL sync on login, Redis session write/read/delete. |
| 403 templates | `app/templates/pages/403.html`, `app/templates/partials/403.html` | Full page + HTMX-swappable fragment. |

**Security layers:**
- Three-layer CSRF: OAuth `state` parameter (login-CSRF) + PKCE `code_verifier` (code interception) + Starlette CSRF middleware (form CSRF). All three present, none redundant.
- Qdrant role filter remains the hard retrieval boundary (unchanged from Phase 2).
- Session IDs: `secrets.token_urlsafe(32)` — cryptographically secure.
- Cookie: `httponly=True`, `samesite="lax"`, `secure=False` (deliberate — internal HTTP deployment; set `secure=True` when Caddy TLS is enabled).

**Design decisions:**
- Absolute TTL session expiry (no sliding). Accepted trade-off: a disabled Keycloak account remains valid until TTL expires. Post-MVP: add Keycloak introspection on sensitive operations.
- `UserSession` holds `user_id, keycloak_id, email, role, is_admin, is_owner` — no access token, no refresh token retained server-side.
- Lazy Keycloak sync: user row created/updated in PostgreSQL on first login, not via webhook.

**Per-module reference:** `docs/auth/` contains one detailed document per module plus an overview — see `docs/auth/README.md`.

---

## 11. Frontend layer — Phase 3b

The full HTMX + Jinja2 frontend is implemented and wired. Server-side rendering,
SSE streaming for pipeline output, buffered-answer discipline (answer never
streamed before the faithfulness verdict). **29 tests pass (8 macros + 7 pages + 8 route + 6 templates).**

| Module | File | What |
|---|---|---|
| Macros + base | `app/templates/macros/`, `app/templates/base.html` | `form_field` + `btn` macros; HTMX SSE extension; block head/footer for extensibility |
| Base pages | `app/templates/pages/login.html`, `app/templates/pages/dashboard.html` | Login (Keycloak button, no nav); dashboard (user info, admin/owner badges, search link) |
| Search route | `app/api/routes/search.py`, `app/pipeline/factory.py` | GET/POST /search, GET /search/stream (SSE); composition root for all 9 pipeline nodes |
| Search templates | `app/templates/pages/search.html`, `partials/`, `components/`, `static/css/pages/search.css` | Search page, SSE connection partial, message bubble, source cards |

**SSE event protocol:**
- `progress` → HTML progress indicator (immediately)
- `answer` → rendered message_bubble.html (ONLY if `is_faithful=True`)
- `error` → HTML error fragment (unfaithful answer or pipeline exception)
- `done` → always last (signals client to close)

**Buffered answer discipline (CLAUDE.md §12):** The pipeline's `ainvoke()` runs to
completion. The generator writes the full answer into state. The faithfulness node
verifies it. Only then does the SSE endpoint emit the answer event. If unfaithful,
the answer is discarded and an error fallback is sent.

**qid (query ID) pattern:** `POST /search` generates a UUID4 hex, stores the query
in Redis under `query:{qid}` with 60s TTL, returns a partial with
`sse-connect="/search/stream?qid={qid}"`. Avoids URL encoding long or special-character
queries.

**Pipeline factory (composition root):** `app/pipeline/factory.py` instantiates all
service singletons (LiteLLM, OllamaEmbedder, QdrantVectorStore, TEIReranker) and
injects them into the 9 nodes. `get_compiled_pipeline(settings)` returns the compiled
LangGraph StateGraph, ready for `ainvoke()`. Single wiring point — routes never
instantiate nodes directly.

**Per-module reference:** `docs/frontend/` contains one detailed document per module
plus an overview — see `docs/frontend/README.md`.

