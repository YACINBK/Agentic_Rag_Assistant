# Whitecape Knowledge Assistant

A RAG-based internal knowledge assistant for Whitecape Technologies. Employees ask
natural-language questions; the system retrieves relevant chunks from indexed company
documents through a multi-stage LangGraph pipeline and streams a **cited, role-aware**
answer back over SSE. Every answer is grounded in source documents and verified by a
faithfulness checker before it reaches the user — no external search, no ungrounded output.

> **Specification:** `CLAUDE.md` is the source of truth for every design decision.
> **Implementation log:** `DEVLOG.md` documents what was built; `CHANGELOG.md` is the
> chronological change log.

---

## Architecture at a glance

- **Interface:** HTMX + Jinja2, server-rendered by FastAPI (no separate frontend process).
- **Pipeline:** LangGraph `StateGraph`, 9 nodes, conditional routing.
- **Model abstraction:** all LLM calls go through LiteLLM → Ollama (local, no external API).
- **Embeddings:** BGE-M3 via Ollama. **Reranker:** bge-reranker-v2-m3 via Hugging Face TEI.
- **Stores:** Qdrant (vectors + semantic cache), PostgreSQL (users/docs/audit), Redis (sessions + Celery).
- **Auth:** Keycloak OIDC, session-based (HTTP-only cookie), CSRF-protected.

### Query pipeline (SIMPLE_RAG path)

```
Query → [0 Cache] → [1 Classify] → [2 Rewrite] → [3 Search] → [4 Rerank]
      → [5 Relevance gate] ─(fail)→ [5b Retry] ─(loop once)→ [3 Search]
      → [6 Generate (buffered)] → [7 Faithfulness] → stream via SSE
```

- **Cache hit** → cached answer returned immediately, pipeline skipped.
- **DIRECT** (chitchat / out-of-scope) → canned response, no retrieval.
- **Relevance/faithfulness failure** → honest fallback + escalation event.

See `diags/` for the full sequence, use-case, and class diagrams (`.drawio`).

---

## Project layout

```
app/
├── core/          # ABCs, ORM models, PipelineState, settings, exceptions (the contracts)
├── services/      # Concrete implementations (LiteLLM, Qdrant, Ollama, TEI, Keycloak)
├── pipeline/      # graph.py + nodes/ (9 BaseNode subclasses)
├── api/           # FastAPI routes
├── tasks/         # Celery workers (ingestion, cache cleanup)
├── templates/     # HTMX + Jinja2 (pages / partials / components / macros)
└── static/        # CSS / JS
tests/             # unit/ (per node) + integration/ (graph routing)
diags/             # architecture diagrams (.drawio)
```

---

## Development

Requires Python 3.11+.

```bash
# Install (editable, with dev tools)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run the test suite
pytest -q

# Lint
ruff check app/ tests/
```

Configuration is read from `.env` (see `.env.example` for all variables and defaults).
Models default to local Ollama (`qwen2.5` family). Set `CLASSIFIER_MODEL=manual` to
skip the classifier LLM during downstream development.

### Full stack (Docker)

```bash
docker compose up
```

This is the all-container topology. For the demo machine's mixed host/Docker
topology, use [LAUNCH.md](LAUNCH.md) instead. When running the backend in Docker,
`KEYCLOAK_URL` is the internal service URL and `KEYCLOAK_PUBLIC_URL` is the
browser-facing published URL; the backend uses the former for server-side OIDC
calls and the latter for browser redirects.

---

## Status

- **Phase 1 — Foundation:** core ABCs, ORM models, service skeletons, diagrams, Docker Compose. ✅
- **Phase 2 — Pipeline:** all 9 nodes + graph integration, 57 tests passing. ✅
- **Phase 3 — Auth + Frontend:** ✅.
- **Phase 4 — Ingestion + Admin:** in progress.
