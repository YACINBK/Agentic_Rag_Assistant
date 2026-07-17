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
    ├── security.py           BaseAuthService ABC + TokenClaims
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
│   │   └── nodes/                   # One file per pipeline node (not yet implemented)
│   │       └── __init__.py
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   └── routes/
│   │       └── __init__.py
│   │
│   ├── tasks/
│   │   ├── __init__.py
│   │   ├── worker.py                # Celery app creation + config
│   │   ├── ingestion.py             # Document ingestion task (async via Redis)
│   │   └── cache_cleanup.py         # TTL-based semantic cache purge (Celery beat)
│   │
│   ├── templates/
│   │   ├── base.html                # Layout: nav, HTMX script, content block
│   │   ├── pages/                   # Full-page templates (login, dashboard, ...)
│   │   ├── partials/                # HTMX partial responses
│   │   ├── components/              # Reusable Jinja2 include blocks
│   │   └── macros/                  # Jinja2 macros
│   │
│   └── static/
│       ├── css/base.css             # Base stylesheet
│       └── js/                      # Client-side JS (empty, ready for use)
│
├── tests/
│   ├── conftest.py                  # Shared fixtures: state factories + mock patterns
│   ├── unit/
│   │   └── __init__.py
│   └── integration/
│       └── __init__.py
│
├── contracts/                       # Loop engineering node contracts (CLAUDE.md §11)
├── reviews/                         # Generator summaries per node
├── test_results/                    # pytest output per node
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

## 8. Loop Engineering Setup

See CLAUDE.md §11 for the full specification. Summary of what's on disk:

| Path | Purpose |
|---|---|
| `run_loop.sh` | Automation script — Generator → pytest → Evaluator per node |
| `contracts/` | One `.md` per node + `conftest.md` — written by Planner |
| `reviews/` | Generator summary per node — written by Generator |
| `test_results/` | pytest output per node — written by `run_loop.sh` |
| `feature_list.json` | Tracks node status: done / in_progress / next / blocked |
| `progress.md` | Current sprint, active contract, last verified state |
| `log.md` | Append-only — Evaluator writes PASS/FAIL per node per run |
| `tests/conftest.py` | Shared fixtures — state factories, mock patterns for all nodes |

**Current status:** Infrastructure in place. No node contracts written yet. No nodes implemented. `tests/conftest.py` contains state factories and mock fixture patterns ready for use once contracts are authored.

---

## 9. Changelog

### [2025-07-17] — Session-Based OIDC Auth Implementation

**Scope:** Full implementation of session-based OIDC auth flow. Replaced JWT-validation stub with working authlib + Redis session implementation.

**Changes:**
- Rewrote `app/core/security.py` — new ABC methods: `get_authorization_url`, `handle_callback`, `get_current_user`, `logout`. Replaced `TokenClaims` with `UserSession` dataclass (user_id, keycloak_id, email, role, is_admin, is_owner).
- Rewrote `app/services/auth.py` — `KeycloakAuthService` using `authlib.integrations.httpx_client.AsyncOAuth2Client` with PKCE. Server-side sessions in Redis (configurable TTL). OAuth state stored in Redis (5 min TTL). Lazy user/role sync to PostgreSQL on login.
- Rewrote `app/main.py` — FastAPI lifespan (Redis init/cleanup), CSRF middleware (`starlette-csrf`), Jinja2 templates, static file serving, auth routes (`/auth/login`, `/auth/start`, `/auth/callback`, `/auth/logout`), session cookie management.
- Created `app/templates/` — base layout (HTMX-boosted, nav), login page, dashboard page.
- Created `app/static/css/base.css` — minimal styling for nav, login, dashboard.
- Added `SECRET_KEY` and `SESSION_TTL_SECONDS` to settings and `.env.example`.
- Fixed lint issues in models: unused imports, forward reference warnings (added `TYPE_CHECKING` guards), `== True` → `.is_(True)`.
- Removed stale `python-jose` from venv (was still installed despite pyproject.toml removal).
- Updated `app/core/exceptions.py` — `AuthenticationError` docstring updated from "JWT validation" to "OIDC flow or session validation".

**Template structure:**
```
app/templates/
├── base.html              # Layout: nav + content block + HTMX script
├── pages/
│   ├── login.html         # "Sign in with Keycloak" button
│   └── dashboard.html     # Post-login landing (role display, search link)
├── partials/              # HTMX partial responses (empty, ready for use)
├── components/            # Reusable Jinja2 components (empty, ready for use)
└── macros/                # Jinja2 macros (empty, ready for use)
```

**No Caddy/Nginx:** Confirmed no proxy layer in code or docker-compose. Docs mention Caddy as a future option only — nothing to remove.

---

### [2025-07-16] — HTMX Monolith Migration

**Scope:** Remove Nginx and Next.js frontend. Consolidate to single FastAPI process serving HTMX + Jinja2.

**Changes:**
- Removed `nginx/` directory (was untracked, contained `nginx.conf`)
- Removed `nginx` and `frontend` services from `docker-compose.yml`
- Backend now exposes port 8000 directly with `uvicorn --workers 4`
- Added template/static volume mounts to backend service
- Updated Redis comment to reflect triple role (session store + broker + backend)
- Rewrote `diags/seq_1b_direct_and_cache.drawio` — removed Next.js/Nginx lifelines, shows FastAPI serving directly
- Rewrote `diags/seq_2_document_ingestion.drawio` — removed Next.js/Nginx lifelines, shows session cookie auth
- Rewrote `diags/seq_3_authentication.drawio` — fundamentally redesigned for session-based OIDC (PKCE, authlib, HTTP-only cookie, lazy PostgreSQL sync)
- Updated `.gitignore` — added secrets patterns (`*api_key*`, `*.key`, `*.pem`), allowed `diags/` to be committed

**Auth flow (new):**
1. User hits `/login` → FastAPI generates PKCE challenge + redirects to Keycloak
2. User authenticates at Keycloak → callback with authorization code
3. FastAPI exchanges code for tokens (server-side, PKCE-verified)
4. Lazy sync: upsert User in PostgreSQL from JWT claims (role, email, keycloak_id)
5. Create server-side session in Redis → Set-Cookie (HTTP-only, Secure, SameSite=Lax)
6. CSRF middleware protects all state-changing endpoints

**Docker Compose:** 11 services → 9 services. No proxy layer.

---

### [2025-07-15] — Service Layer Skeleton

**Scope:** Concrete implementations of all core service ABCs.

**Created:**
- `app/services/auth.py` — `KeycloakAuthService`: OIDC via authlib, session validation, lazy user sync
- `app/services/llm.py` — `LiteLLMService`: wraps `litellm.acompletion`, respects settings model names
- `app/services/embedder.py` — `OllamaEmbedder`: BGE-M3 embeddings via Ollama HTTP API
- `app/services/reranker.py` — `TEIReranker`: POST to TEI `/rerank` endpoint, returns scores
- `app/services/vector_store.py` — `QdrantVectorStore`: hybrid search with mandatory role filter, upsert, delete
- `app/services/__init__.py` — re-exports all service classes
- `app/core/logging.py` — structlog configuration (JSON, bound context, stdlib bridge)
- `tests/conftest.py` — state factories + mock fixtures for loop engineering

---

### [2025-07-14] — Core Skeleton + Diagrams

**Scope:** Project structure, core ABCs, ORM models, Docker Compose, all diagrams.

**Created:**
- Full `app/core/` layer: ABCs (`base_node`, `security`, `services/*`), models (`role`, `user`, `document`, `audit_log`, `escalation_event`), `state.py`, `settings.py`, `exceptions.py`
- `docker-compose.yml` — 9 services (see §6)
- `Dockerfile`, `pyproject.toml`, `.env.example`, `alembic.ini`
- Loop engineering files: `run_loop.sh`, `feature_list.json`, `progress.md`, `log.md`
- `app/main.py` — FastAPI app with `/health` endpoint
- `app/tasks/` — Celery worker, ingestion task, cache cleanup task

**Diagrams created:**
- 5 sequence diagrams (seq_1a through seq_3)
- 3 use case diagrams (uc_1a through uc_1c)
- 8 class diagrams (covering all subsystems)

**Design decision:** Core-abstraction pattern (Java-style). `app/core/` contains all ABCs and shared types. Everything else inherits. Chosen for testability (mock at ABC boundary), vendor independence (CLAUDE.md §4), and alignment with contract-driven loop engineering (CLAUDE.md §11).

---

### [2025-07-14] — Initial Commit

**Scope:** Repository initialization.

**Created:** `.gitignore`, project README, initial diagrams directory.
