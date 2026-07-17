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
    ├── state.py              PipelineState TypedDict — flows through every node
    ├── settings.py           Pydantic Settings — single env config source
    ├── security.py           BaseAuthService ABC + TokenClaims
    └── exceptions.py         Typed exception hierarchy
         |
         ▼
app/services/        Concrete implementations (LiteLLMService, QdrantVectorStore, ...)
app/pipeline/nodes/  Concrete BaseNode subclasses (one per pipeline stage)
app/api/             FastAPI routes — depend on core interfaces only
app/tasks/           Celery async tasks (ingestion, cache cleanup)
```

**Why this pattern:**
- **Testability** — mocks replace concretes at the ABC boundary. `MockLLMService(BaseLLMService)` in tests, `LiteLLMService(BaseLLMService)` in production. Clean substitution, no patching internals.
- **No vendor lock** — CLAUDE.md §4 mandates all LLM calls through LiteLLM, but the ABC layer means swapping LiteLLM for another provider is a single class replacement, not a codebase-wide refactor.
- **Loop engineering** — contract-driven development (CLAUDE.md §11) defines interfaces first. The ABCs *are* the programmatic form of those contracts.

---

## 3. Directory Structure

```
rag_assistant/
├── CLAUDE.md                        # Specification (§0–§16) — DO NOT MODIFY without instruction
├── DEVLOG.md                        # This file — implementation log
├── Dockerfile                       # Backend container (Python 3.11, uvicorn)
├── docker-compose.yml               # Full stack: 9 services (see §6 below)
├── pyproject.toml                    # Dependencies + tooling config (pytest, ruff)
├── .env.example                     # All env vars with defaults and comments
├── .gitignore                       # Python, Node, IDE, state files
├── alembic.ini                      # Alembic migration config
├── run_loop.sh                      # Loop engineering automation (CLAUDE.md §11)
├── feature_list.json                # Node implementation tracker (done/in_progress/next/blocked)
├── log.md                           # Append-only evaluator results
├── progress.md                      # Current sprint state
│
├── app/
│   ├── main.py                      # FastAPI app entry point (/health endpoint)
│   ├── core/                        # THE CORE — abstract contracts + shared types
│   │   ├── base_node.py             # BaseNode ABC: name property + execute(state) → state
│   │   ├── state.py                 # PipelineState TypedDict + ChunkPayload
│   │   ├── settings.py              # Pydantic Settings — all model/service config from .env
│   │   ├── security.py              # BaseAuthService ABC + TokenClaims dataclass
│   │   ├── exceptions.py            # PipelineError, AuthenticationError, AuthorizationError, ...
│   │   ├── services/
│   │   │   ├── llm.py               # BaseLLMService ABC — complete(model, messages) → str
│   │   │   ├── vector_store.py      # BaseVectorStore ABC — search/upsert/delete with role filter
│   │   │   ├── reranker.py          # BaseReranker ABC — rerank(query, passages) → scores
│   │   │   └── embedder.py          # BaseEmbedder ABC — embed(texts) → vectors
│   │   └── models/
│   │       ├── base.py              # SQLAlchemy async engine + DeclarativeBase + get_session
│   │       ├── role.py              # Role lookup table (dynamic, not hardcoded)
│   │       ├── user.py              # User with is_admin/is_owner flags + partial unique index
│   │       ├── document.py          # Document with category enum + ingestion_status enum
│   │       ├── audit_log.py         # AuditLog — append-only, compliance requirement
│   │       └── escalation_event.py  # EscalationEvent — faithfulness/relevance failures
│   │
│   ├── services/                    # Concrete implementations of core ABCs (not yet written)
│   ├── pipeline/
│   │   └── nodes/                   # Concrete BaseNode subclasses (not yet written)
│   ├── api/
│   │   └── routes/                  # FastAPI routers (not yet written)
│   └── tasks/
│       ├── worker.py                # Celery app config + beat schedule
│       ├── cache_cleanup.py         # Hourly semantic_cache TTL purge (implemented)
│       └── ingestion.py             # Async document ingestion (stub)
│
├── tests/
│   ├── unit/                        # Per-node unit tests (written during loop engineering)
│   └── integration/                 # Graph-level integration tests (written after all nodes)
│
├── contracts/                       # Node contracts written by Planner (CLAUDE.md §11)
├── reviews/                         # Generator summaries per node
├── test_results/                    # Pytest output per node (neutral executor)
│
├── alembic/
│   ├── env.py                       # Async migration runner, imports all models
│   └── versions/                    # Auto-generated migration files
│
└── diags/                           # UML diagrams (.drawio format, black/white only)
    ├── Class diagrams (A–H)         # System class structure
    ├── Use case diagrams (1a–1c)    # Actor-use case relationships
    └── Sequence diagrams (1a–3)     # Interaction flows (see §7 below)
```

---

## 4. Core Layer Reference

### `BaseNode` (`app/core/base_node.py`)

Abstract base class for all pipeline nodes. Enforces a uniform interface:

| Member | Type | Purpose |
|---|---|---|
| `name` | `@property @abstractmethod → str` | Short identifier for logging and routing |
| `execute(state)` | `@abstractmethod async → PipelineState` | Run node logic, return updated state |

Every node in `app/pipeline/nodes/` inherits from `BaseNode` and implements both.

### `PipelineState` (`app/core/state.py`)

A single `TypedDict(total=False)` that flows through every LangGraph node. Every key used anywhere in the pipeline is defined here — no node may invent new keys. Sections:

| Section | Keys | Set by |
|---|---|---|
| Input | `query`, `user_id`, `user_role`, `user_email` | Pipeline entry |
| Node 0 | `cache_hit`, `cached_answer` | CacheCheck |
| Node 1 | `classification` | Classifier |
| Node 2 | `rewritten_query`, `sub_queries` | Rewriter |
| Node 3 | `retrieved_chunks` | Qdrant Search |
| Node 4 | `reranked_chunks` | Reranker |
| Node 5 | `relevance_pass` | RelevanceGate |
| Node 5b | `retry_attempted`, `retry_pass` | Retry |
| Node 6 | `generated_answer` | Generator |
| Node 7 | `is_faithful`, `faithfulness_score` | Faithfulness |
| Metadata | `direct_response`, `error`, `audit_log_id`, `escalation_id` | Various |

`ChunkPayload` is a companion TypedDict for individual retrieved chunks (id, text, metadata, score).

### `Settings` (`app/core/settings.py`)

Pydantic Settings — single source of truth for all configuration. Reads from `.env`. Groups: Database, Redis, Qdrant, Keycloak, LLM models, Embeddings, Reranker, Ingestion, Relevance gate.

### Service ABCs (`app/core/services/`)

| ABC | File | Key method | Concrete (planned) |
|---|---|---|---|
| `BaseLLMService` | `llm.py` | `complete(model, messages) → str` | LiteLLM → Ollama |
| `BaseVectorStore` | `vector_store.py` | `search(collection, vector, roles, limit) → [ChunkPayload]` | Qdrant client |
| `BaseReranker` | `reranker.py` | `rerank(query, passages) → [float]` | TEI HTTP POST |
| `BaseEmbedder` | `embedder.py` | `embed(texts) → [[float]]` | Ollama BGE-M3 |

### `BaseAuthService` (`app/core/security.py`)

ABC for JWT authentication + lazy user sync. `TokenClaims` dataclass holds extracted claims (keycloak_id, email, role).

### Exceptions (`app/core/exceptions.py`)

Typed hierarchy: `PipelineError` → `ClassificationError`, `RewriteError`, `RetrievalError`, `GenerationError`, `FaithfulnessError`. Plus standalone `AuthenticationError`, `AuthorizationError`.

---

## 5. Database Models

All models in `app/core/models/`, matching CLAUDE.md §10 exactly.

| Model | Table | Key constraints |
|---|---|---|
| `Role` | `role` | `name` unique. Lookup table — not hardcoded. Seed: developer, qa_engineer |
| `User` | `user` | `email` + `keycloak_id` unique. `is_owner` enforced single via partial unique index: `CREATE UNIQUE INDEX idx_single_owner ON "user" (is_owner) WHERE is_owner = true` |
| `Document` | `document` | `doc_hash` unique (SHA256). `category` enum (technical/quality/projects/company). `ingestion_status` enum |
| `AuditLog` | `audit_log` | **Append-only. No UPDATE. No DELETE.** `chunks_used` is JSON array (MVP simplification) |
| `EscalationEvent` | `escalation_event` | FK to AuditLog. `reason` enum (faithfulness_failure/relevance_failure) |

Base setup in `base.py`: async engine via `asyncpg`, `sessionmaker` with `AsyncSession`, `DeclarativeBase`.

---

## 6. Infrastructure — Docker Compose Services

| Service | Image | Port | Role |
|---|---|---|---|
| `nginx` | nginx:1.27-alpine | 80 | Reverse proxy — sole public entry point |
| `frontend` | Custom (Next.js) | 3000 (internal) | React UI |
| `backend` | Custom (FastAPI) | 8000 (internal) | API server |
| `celery-worker` | Same as backend | — | Async task processing (ingestion) |
| `celery-beat` | Same as backend | — | Periodic tasks (cache TTL cleanup, hourly) |
| `postgres` | postgres:16-alpine | 5432 | Relational DB with healthcheck |
| `redis` | redis:7-alpine | 6379 | Celery broker + result backend |
| `qdrant` | qdrant:v1.12.1 | 6333/6334 | Vector store + semantic cache |
| `keycloak` | keycloak:24.0 | 8080 | OIDC/SSO identity provider |
| `ollama` | ollama:latest | 11434 | Local LLM runtime + BGE-M3 embeddings (GPU) |
| `reranker` | HF TEI cpu-latest | 8082 | bge-reranker-v2-m3 cross-encoder (CPU only) |

Named volumes: `pg_data`, `redis_data`, `qdrant_data`, `keycloak_data`, `ollama_data`, `tei_cache`.

---

## 7. Diagrams Inventory

### Class Diagrams

| File | Description | Status |
|---|---|---|
| `class_A_user_identity.drawio` | User, Role, Owner/Admin flags | Done |
| `class_B_document_management.drawio` | Document, category, ingestion status | Done |
| `class_C_pipeline_nodes.drawio` | BaseNode + all node subclasses | Done |
| `class_D_services.drawio` | Service ABCs + concrete implementations | Done |
| `class_E_api_layer.drawio` | FastAPI routers + request/response models | Done |
| `class_F_audit_compliance.drawio` | AuditLog, EscalationEvent | Done |
| `class_G_cache_system.drawio` | Semantic cache, TTL, invalidation | Done |
| `class_H_task_queue.drawio` | Celery tasks, Redis broker | Done |

### Use Case Diagrams

| File | Description | Status |
|---|---|---|
| `uc_1a_core_query_flow_final.drawio` | User/Admin/Owner query flow | Done |
| `uc_1b_document_management.drawio` | Admin document operations, Owner overrides | Done |
| `uc_1c_user_role_management.drawio` | Admin/Owner user management | Done |

### Sequence Diagrams

| File | Description | UML Fragments | Status |
|---|---|---|---|
| `seq_1a_simple_rag_query.drawio` | SIMPLE_RAG happy path (full pipeline Node 0-7) | `alt` (cache hit/miss), `ref` (failure paths) | Done |
| `seq_1b_direct_and_cache.drawio` | DIRECT path + Cache Hit short-circuits | `alt` (cache hit / DIRECT classification) | Done |
| `seq_1c_failure_paths.drawio` | Relevance gate retry + Faithfulness failure | `alt` (gate pass/fail), `opt` (retry), `alt` (faithfulness) | Done |
| `seq_2_document_ingestion.drawio` | Admin upload + Celery async ingestion | `alt` (admin check), `async`, `loop` (embed batches), `opt` (cache invalidation) | Done |
| `seq_3_authentication.drawio` | Keycloak OIDC Authorization Code flow | `alt` (valid/invalid credentials), `opt` (lazy sync) | Done |

All diagrams: black strokes, white fill, no colors. EU/France UML standards. Evenly-spaced lifeline pipes. `.drawio` format only.

---

## 8. Loop Engineering Setup

Three-role loop per CLAUDE.md §11:

| Role | Tool | Responsibility |
|---|---|---|
| Planner (Opus) | Claude Code | Writes contracts to `contracts/`, decomposes tasks, verifies output |
| Generator | OpenCode | Reads contract, writes node code + tests, writes summary to `reviews/` |
| Evaluator (Sonnet) | Claude Code | Reads test results + diff, checks assertions, appends to `log.md` |

**Files:**
- `run_loop.sh` — automation script. Usage: `./run_loop.sh node_01_classifier`
- `contracts/` — one `.md` per node, written by Planner. Follows contract format (CLAUDE.md §11)
- `reviews/` — generator summary per node
- `test_results/` — pytest output, written by `run_loop.sh` (neutral executor)
- `feature_list.json` — tracks done/in_progress/next/blocked
- `log.md` — append-only evaluator results

**Prerequisite:** `contracts/conftest.md` must be written before any node contract. Defines PipelineState factories, mock fixtures, and mock contracts that all node tests import.

**Current state:** All items in `next` — no node contracts written yet. conftest is the first task.

---

## 9. Changelog

### [2025-07-14] — Concrete Services + Infrastructure Gap-Fill

**Scope:** Implemented all concrete service classes, added structlog, nginx config, and test conftest.

**Created:**
- `app/services/llm.py` — `LiteLLMService(BaseLLMService)` — calls `litellm.acompletion` pointing at Ollama
- `app/services/vector_store.py` — `QdrantVectorStore(BaseVectorStore)` — role filter enforced at Qdrant query level
- `app/services/reranker.py` — `TEIReranker(BaseReranker)` — HTTP POST to TEI `/rerank` endpoint
- `app/services/embedder.py` — `OllamaEmbedder(BaseEmbedder)` — calls Ollama `/api/embed` endpoint
- `app/services/auth.py` — `KeycloakAuthService(BaseAuthService)` — JWKS validation + lazy sync to User table
- `app/services/__init__.py` — re-exports all concrete services
- `app/core/logging.py` — structlog config (JSON output, ISO timestamps, context vars)
- `nginx/nginx.conf` — reverse proxy: `/api/` → backend:8000, `/` → frontend:3000, SSE buffering disabled
- `tests/conftest.py` — `make_state()`, `make_chunk()` factories + `MockLLMService`, `MockVectorStore`, `MockReranker`, `MockEmbedder` + pytest fixtures

**Modified:**
- `app/main.py` — wired `configure_logging()` on startup

**Design decisions:**
- All mocks inherit from ABCs (not `MagicMock`) — ensures interface compliance. They record calls for assertions.
- Nginx disables `proxy_buffering` on `/api/` — required for SSE streaming.
- Embedder uses Ollama's `/api/embed` (not `/api/embeddings`) — correct endpoint for batch embedding.
- Auth service does JWKS fetch once, caches in memory (no disk, no Redis).

---

### [2025-07-14] — Project Skeleton

**Scope:** Full directory tree, core layer, database models, infrastructure configs.

**Created:**
- `app/core/base_node.py` — BaseNode ABC
- `app/core/state.py` — PipelineState + ChunkPayload TypedDicts
- `app/core/settings.py` — Pydantic Settings with all env vars
- `app/core/security.py` — BaseAuthService ABC + TokenClaims
- `app/core/exceptions.py` — typed exception hierarchy
- `app/core/services/{llm,vector_store,reranker,embedder}.py` — service ABCs
- `app/core/models/{base,role,user,document,audit_log,escalation_event}.py` — SQLAlchemy models
- `app/main.py` — FastAPI entry point with /health
- `app/tasks/{worker,cache_cleanup,ingestion}.py` — Celery setup + tasks
- `docker-compose.yml` — 11 services (nginx, frontend, backend, celery x2, postgres, redis, qdrant, keycloak, ollama, reranker)
- `Dockerfile` — Python 3.11 slim backend image
- `pyproject.toml` — dependencies + pytest/ruff config
- `.env.example` — all env vars documented
- `alembic.ini` + `alembic/env.py` — async migration setup
- `run_loop.sh` — loop engineering automation
- `feature_list.json` — node tracker initialized with all nodes in `next`

**Design decision:** Core-abstraction pattern (Java-style). `app/core/` contains all ABCs and shared types. Everything else inherits. Chosen for testability (mock at ABC boundary), vendor independence (CLAUDE.md §4), and alignment with contract-driven loop engineering (CLAUDE.md §11).

---

### [2025-07-14] — Sequence Diagrams (Enhanced)

**Scope:** 5 sequence diagrams created and enhanced with UML combined fragments.

**Created:**
- `diags/seq_1a_simple_rag_query.drawio` — 14 lifelines, full pipeline, Keycloak JWT validation
- `diags/seq_1b_direct_and_cache.drawio` — 10 lifelines, two short-circuit paths
- `diags/seq_1c_failure_paths.drawio` — 10 lifelines, safety-net failure paths
- `diags/seq_2_document_ingestion.drawio` — 10 lifelines, async Celery processing
- `diags/seq_3_authentication.drawio` — 5 lifelines, OIDC Authorization Code flow

**Enhancement pass:** Added `alt`, `opt`, `ref`, `loop`, `async` UML combined-fragment blocks. Added Keycloak lifeline where missing (seq_1a, seq_1b, seq_2). Normalized lifeline spacing to consistent pitch (175-210px).

---

### [2025-07-14] — Use Case Diagrams

**Scope:** 3 use case diagrams with correct actor hierarchy.

**Created:**
- `diags/uc_1a_core_query_flow_final.drawio` — Owner > Admin > User, query flow
- `diags/uc_1b_document_management.drawio` — Admin document ops, Owner overrides
- `diags/uc_1c_user_role_management.drawio` — Admin/Owner user management

**Conventions:** Owner at top (y=100-120), Admin middle, User bottom. All actors left side. Generalization arrows (hollow triangle). Include/extend stereotypes.

---

### [2025-07-14] — Class Diagrams

**Scope:** 8 class diagrams covering the full system architecture.

**Created:** `diags/class_A_*.drawio` through `diags/class_H_*.drawio`

All diagrams follow black strokes, white fill, no colors — per CLAUDE.md §12.
