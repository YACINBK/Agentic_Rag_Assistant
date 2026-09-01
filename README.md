# Whitecape Knowledge Assistant

An internal, role-aware RAG assistant for Whitecape Technologies. Employees ask
questions in natural language; the system retrieves matching passages from the
indexed company corpus, streams a **cited** answer over SSE, and refuses to
answer when the evidence isn't there. Every answer is traceable to a source
document — and every user only ever sees documents their role and privilege
allow.

**This is the full MVP.** All four build phases are complete and verified.

| What you get | In one line |
|---|---|
| Grounded answers | Streamed over SSE with inline `[N]` citations — hover a marker to read the exact source passage, its breadcrumb, and its figures |
| Role-aware retrieval | Security enforced **inside the vector search** (§5 two-dimension filter) — out-of-scope content never reaches the model, so it cannot leak |
| Honest failure | The faithfulness checker verifies the complete answer before anything is streamed; failures are declined, logged, and escalated — never guessed |
| Repeat questions in ~1s | A semantic cache serves verified answers 25×+ faster, with citations and images intact |
| Async ingestion | Upload an HTML export → Celery runs extract/chunk/enrich/index → the admin list tracks `pending → running → done` live |
| Full admin surface | Upload, delete, re-ingest, user management, role assignment — owner-only where §2 says so |

---

## Features and their state

### Delivered (MVP, verified end-to-end)

- **Query pipeline** — 9-node LangGraph graph: semantic cache check → binary
  classification (DIRECT / SIMPLE_RAG) → query rewrite → role-filtered Qdrant
  search → cross-encoder rerank (TEI) → relevance gate with one retry →
  **buffered** generation → token-overlap faithfulness check → SSE stream.
  The answer is held until the faithfulness verdict — nothing unverified is
  ever shown.
- **Citation UX** — inline markers resolve to hover cards (Alpine.js) carrying
  the chunk text, section breadcrumb, deep-link anchor and extracted images,
  served through a junction-checked, authenticated image route.
- **Two-layer role security** — Qdrant filter (`allowed_roles` + `admin_only`)
  as the hard boundary; role persona prompts as the UX layer. Roles are a
  lookup table, not hardcoded — adding one is an INSERT.
- **First-login flow** — Keycloak proves identity; PostgreSQL owns access. A
  first-time user picks their primary role once (gated app-wide by middleware);
  an owner can pre-empt the picker by assigning through the Users page.
- **Semantic cache** — faithful answers are written back (with citations,
  `admin_only`, `document_ids`) and invalidated on re-ingestion; repeat
  questions skip the pipeline.
- **Audit & escalation** — every completed query lands one append-only
  AuditLog row; faithfulness/relevance failures additionally raise an
  EscalationEvent.
- **Rate limiting** — per-user fixed-window Redis budget on search (429 over).
- **Ingestion pipeline** — Celery-driven stages: extract (BookStack HTML,
  content-addressed images) → chunk (BGE-M3 tokenizer, 800/200) → enrich
  (LLM contextual headers) → index (dense 1024-d, payload-indexed filters) →
  cache invalidation.
- **Admin UI** — documents (upload with dedup/size ceiling, delete with
  three-store cleanup, re-ingest with a 409 in-flight guard, live list
  self-refresh) and users (owner-only: admin grant/revoke with session purge,
  role assignment).
- **Auth** — Keycloak 24 OIDC (PKCE + `scope=openid`), server-side Redis
  sessions, HTTP-only cookies, CSRF middleware, RP-initiated logout.

### Roadmap (post-MVP, deliberately deferred)

- Image captioning stage (Stage D — needs a local vision model)
- Hybrid retrieval: sparse/lexical channel alongside dense BGE-M3
- Keycloak role mirroring (realignment with the original §2 design)
- LLM-based faithfulness checking · Prometheus + Grafana · Slack channel

---

## Architecture at a glance

- **Interface:** HTMX + Jinja2, server-rendered by FastAPI — no separate
  frontend process, no npm.
- **Orchestration:** LangGraph `StateGraph`; all LLM calls funnel through
  **LiteLLM**, so the provider is a `.env` switch (defaults to local Ollama).
- **Embeddings:** BGE-M3 (dense, 1024-d) via Ollama · **Reranker:**
  bge-reranker-v2-m3 via Hugging Face TEI (GPU image).
- **Stores:** Qdrant (documents + semantic cache) · PostgreSQL (users, roles,
  documents, audit) · Redis (sessions, queues, rate limits).
- **Async:** Celery + Redis for ingestion and the hourly cache TTL purge.

```
Query → [0 Cache] → [1 Classify] → [2 Rewrite] → [3 Search] → [4 Rerank]
      → [5 Relevance gate] ─(fail)→ [5b Retry] → [3]
      → [6 Generate (buffered)] → [7 Faithfulness] → SSE answer
```

Cache hit → served immediately with its stored citations. DIRECT → canned
response, no retrieval. Gate/faithfulness failure → honest fallback +
EscalationEvent.

Diagrams (`.drawio`): `diags/`.

---

## Running it

Three doors, depending on who you are:

| You want to… | Read |
|---|---|
| **Demo it** (scripted, module by module) | [`DEMO.md`](DEMO.md) |
| **Operate it** (cold start, topology, troubleshooting) | [`LAUNCH.md`](LAUNCH.md) |
| **Read the spec** (every binding design decision) | [`CLAUDE.md`](CLAUDE.md) |

Quick taste — all-container topology:

```bash
docker compose up          # postgres, qdrant, keycloak, reranker, backend, worker
```

The demo machine runs a mixed host/Docker topology (host Redis + Ollama);
`LAUNCH.md` §0 is the copy-paste cold start for that. Realm accounts (password
`whitecape`): `owner.demo`, `admin.demo`, `user.one`, `user.two` — the owner
seed is `scripts/seed_owner.py` (run it **after** the owner's first login).
`scripts/reset_demo_state.py` restores first-login demo state at any time.

---

## Project layout

```
app/
├── core/          # ABCs, ORM models, PipelineState, settings, role gate
├── services/      # LiteLLM, Qdrant, Ollama embedder, Keycloak auth, audit,
│                  # cache writer, rate limit, sessions, storage
├── pipeline/      # graph.py + nodes/ (9 nodes)
├── ingestion/     # extract / chunk / enrich / index / invalidate stages
├── api/           # routes: search (+SSE), admin, onboarding, images
├── tasks/         # Celery: ingestion task, cache TTL purge
├── templates/     # pages / partials / components / macros (HTMX + Jinja2)
└── static/        # CSS / JS (csrf, upload status)
tests/             # unit/ + integration/ — 385 passing
deploy/keycloak/   # realm export (identity-only, pinned user ids)
demo_docs/         # four small mock documents for the demo corpus
scripts/           # seed_owner, reset_demo_state, cleanup_documents
```

---

## Development

Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q                                  # 385 passing
ruff check app tests scripts
```

Configuration via `.env` (see `.env.example`); `.env.local` overrides it for
host-side runs (endpoints, model overrides, provider keys). Set
`CLASSIFIER_MODEL=manual` to bypass the classifier LLM while iterating on
downstream nodes.

---

## Status

- **Phase 1 — Foundation** (ABCs, models, compose, diagrams) ✅
- **Phase 2 — Pipeline** (9 nodes + graph integration) ✅
- **Phase 3 — Auth + Frontend** (OIDC, sessions, HTMX UI, SSE) ✅
- **Phase 4 — Ingestion + Admin + Cache** (Stages A–G, admin UI, role
  authority chain, semantic cache write-back, audit, rate limiting) ✅

**MVP complete.** History: `DEVLOG.md` (what was built, when) ·
`CHANGELOG.md` (chronological) · branch model and loop process: `CLAUDE.md` §11/§18.
