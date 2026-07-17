# CLAUDE.md — Whitecape Internal Knowledge Assistant
**Primary handoff document. Read completely before starting any task.**
**Every decision below is final unless explicitly marked OPEN.**

---

## 0. How to read this file

Sections 1–6 are architecture and design — read once, refer back as needed.
Section 7 is the pipeline specification — the contract source of truth for every node.
Section 8 is the ingestion pipeline — chunking, metadata, async processing.
Section 9 is the semantic cache design — Qdrant-based, role-scoped, with invalidation.
Section 10 is the database schema — do not deviate from it.
Section 11 is the loop engineering setup — how code gets written and verified.
Section 12 is the hard constraint list — read before touching any code.
Section 17 is the frontend conventions — component-based HTMX architecture.
Section 18 is the git branching strategy — branch model, naming, workflow.

---

## 1. What this system is

A RAG-based internal search assistant deployed inside Whitecape Technologies.
Employees type a natural language question. The system retrieves relevant chunks
from indexed internal company documents, passes them through a multi-stage
LangGraph pipeline, and streams a cited, role-aware answer back to the user via SSE.

No external search. Every answer is grounded in and traceable to a source document.
Hallucinations are caught by a dedicated faithfulness checker before anything reaches
the user.

---

## 2. Roles and permissions

Roles are **not hardcoded**. The system supports an arbitrary number of primary
roles managed via a `Role` lookup table in PostgreSQL and mirrored in Keycloak.
Developer and QA Engineer are the initial seed roles, not a fixed set.
Adding a new role is an INSERT + Keycloak realm config — no code change, no migration.

**Admin is not a role** — it is an additional flag (`is_admin = true`) granted
on top of any primary role. An admin who is a Developer still gets
developer-framed answers, but also has access to admin operations.

**Owner is not a role** — it is a second flag (`is_owner = true`) granted on
top of any primary role. Owner is seeded at deployment time (one account,
configured in environment or init script). Owner status is not manageable
through the application UI — it cannot be granted or revoked by any user,
including other owners. Owner inherits all Admin capabilities automatically.

| Concept | What it controls |
|---|---|
| Primary role (from Role table) | Retrieval scope (Qdrant filter), answer framing (role persona prompt) |
| is_admin flag | Upload, delete, role management, audit log access, system config, re-ingestion |
| is_owner flag | Admin assignment/revocation, document category restrictions, global ingestion config override |

| Admin flag | Additional capabilities (on top of primary role) |
|---|---|
| is_admin = true | Upload documents, delete documents, manage user roles (primary role only), view full organizational audit log, view system metrics, trigger manual re-ingestion, configure system settings |

| Owner flag | Additional capabilities (on top of Admin) |
|---|---|
| is_owner = true | Assign/revoke is_admin flag on any user, assign document category access restrictions, override ingestion configuration globally, view all Admin accounts and their active status, view Admin assignment history |

**Non-negotiable:**
- Keycloak authentication is required for every action without exception.
  Auth is session-based (HTTP-only cookie), not Bearer token.
  FastAPI manages the OIDC flow server-side via authlib.
- Document upload requires is_admin. Primary role alone never grants upload.
- Admin is additive — it never overrides or replaces the primary role.
- Owner is additive — it never overrides or replaces the primary role or admin flag.
- Owner is seeded at deployment. Not assignable through the UI. Not manageable by Admin.
- Admin cannot assign or revoke is_admin — only Owner can.
- Role enforcement happens at two layers — see Section 5.
- No role logic may hardcode role names. Filter by user's role string, not by `if role == "developer"`.

---

## 3. Technology stack

| Layer | Technology | Status |
|---|---|---|
| Frontend | HTMX + Jinja2 (server-rendered, served by FastAPI) | Final |
| TLS proxy (prod only) | Caddy — auto-managed HTTPS, optional | Final |
| Backend API | FastAPI (Python) — serves UI + API + SSE | Final |
| Auth | Keycloak 24+ — OIDC Authorization Code, session cookie | Final |
| Agent orchestration | LangGraph StateGraph | Final |
| Model abstraction | LiteLLM — all LLM calls go through this | Final |
| Embeddings | BGE-M3 (self-hosted via Ollama) | Final |
| Vector store | Qdrant — hybrid search | Final |
| Reranker | bge-reranker-v2-m3 via Hugging Face TEI (CPU) | Final |
| Relational DB | PostgreSQL | Final |
| Async task queue | Celery + Redis | Final |
| Logging | structlog (from day one) | Final |
| Observability | Prometheus + Grafana | Post-MVP only |
| Containerization | Docker Compose, private internal network | Final |
| Streaming | SSE via FastAPI EventSourceResponse + HTMX sse extension | Final |
| Local model runtime | Ollama | Final |
| Session management | Server-side sessions (Redis-backed), HTTP-only cookie | Final |
| CSRF protection | Starlette CSRF middleware, token in forms | Final |

---

## 4. Model configuration — no vendor lock

All LLM calls go through LiteLLM. No node imports anthropic or openai directly.
Provider and model are set entirely in .env. Switching provider requires zero code changes.

```python
# app/core/settings.py — the only place models are named
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    CLASSIFIER_MODEL: str
    REWRITER_MODEL: str
    GENERATOR_MODEL: str
    FAITHFULNESS_MODEL: str | None = None  # MVP: token overlap, no model needed

    class Config:
        env_file = ".env"

settings = Settings()
```

```python
# Every pipeline node calls LLM like this — no exceptions
from litellm import acompletion
from app.core.settings import settings

response = await acompletion(
    model=settings.CLASSIFIER_MODEL,
    messages=[{"role": "user", "content": prompt}]
)
```

### Default .env (MVP — all local via Ollama)

```bash
# Classifier — fast, fits in VRAM
CLASSIFIER_MODEL=ollama/qwen2.5:7b
# CLASSIFIER_MODEL=manual                       # manual classification mode

# Rewriter + Decomposer — needs instruction-following quality
REWRITER_MODEL=ollama/qwen2.5:14b
# REWRITER_MODEL=ollama/qwen2.5:7b              # faster, lower rewrite quality

# Generator — largest local model, buffered output (speed less critical)
GENERATOR_MODEL=ollama/qwen2.5:32b
# GENERATOR_MODEL=ollama/qwen2.5:14b            # faster fallback, lower quality

# Faithfulness — MVP uses token overlap heuristic, no LLM call needed
# FAITHFULNESS_MODEL=ollama/qwen2.5:7b          # post-MVP LLM-based checking
```

### Manual classification mode

When CLASSIFIER_MODEL=manual, the classifier node skips the LLM call
and routes to a human decision point (CLI prompt or admin UI toggle).
Use during development to test downstream nodes in isolation.

```python
if settings.CLASSIFIER_MODEL == "manual":
    return ManualClassifier()
else:
    return LLMClassifier(model=settings.CLASSIFIER_MODEL)
```

### Local model hardware guide (24GB RAM, RTX 4060 8GB VRAM)

The binding constraint is VRAM. Use partial GPU offload (-ngl flag in llama.cpp,
handled automatically by Ollama) to run models larger than 8GB VRAM.

| Model | Fits in VRAM | RAM offload needed | Best role |
|---|---|---|---|
| qwen2.5:7b | Yes (~4.5GB) | None | Classifier, faithfulness |
| mistral:7b | Yes (~4.5GB) | None | Classifier, faithfulness |
| qwen2.5:14b | Partial | ~8GB RAM | Rewriter, faithfulness |
| qwen2.5:32b | Partial | ~20GB RAM | Generator (slow: 3-7 tok/s) |

Ollama handles layer splitting automatically. Run `ollama run qwen2.5:14b`
and it uses available VRAM first, offloads remainder to RAM.

Recommendation for MVP: qwen2.5:32b as generator is slow (~3-7 tok/s) but
acceptable because output is now BUFFERED — the user sees nothing until
faithfulness check passes, so per-token speed is less critical than total
latency. If 32B proves too slow in testing, fall back to qwen2.5:14b
(faster, slightly lower quality). All models run local — no external API.

### Reranker runtime — Hugging Face TEI

The reranker (bge-reranker-v2-m3) is a **cross-encoder** — it scores (query, passage)
pairs jointly, not independently. Ollama cannot run cross-encoders because it only
supports causal/embedding architectures. The reranker runs as a separate Docker service
via Hugging Face Text Embeddings Inference (TEI).

```yaml
# docker-compose.yml (relevant service)
reranker:
  image: ghcr.io/huggingface/text-embeddings-inference:cpu-latest
  command: --model-id BAAI/bge-reranker-v2-m3 --port 8082
  ports:
    - "8082:8082"
  volumes:
    - tei_cache:/data
```

Node 4 calls the reranker via HTTP POST:

```python
import httpx

async def rerank(query: str, passages: list[str]) -> list[float]:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://reranker:8082/rerank",
            json={"query": query, "texts": passages},
        )
        response.raise_for_status()
        return [r["score"] for r in response.json()]
```

---

## 5. Role-based security — two layers, both mandatory

### Layer 1 — Qdrant filter at query time (hard security boundary)

Applied inside node 3 on every search call. The LLM physically never
sees chunks the user's role cannot access. Not bypassable by prompt injection.

```python
models.FieldCondition(
    key="allowed_roles",
    match=models.MatchAny(any=[user_role, "all"])
)
```

### Layer 2 — Role persona in generation prompt (UX layer only)

System prompt passed to the generator includes a role persona that shapes
tone and framing per the user's primary role. Each role in the Role table
has an associated persona prompt template stored in the database or config.
Adding a new role's persona is a config change, not a code change.

This is UX. It is not a security boundary. It does not replace Layer 1.
Prompt-level enforcement alone is injectable. Never treat it as the only gate.

---

## 6. Full pipeline — canonical architecture

```
User Query
    │
    ▼
[0. Semantic Cache Check]            ~50ms, no LLM cost
    compute BGE-M3 query embedding
    search semantic_cache collection (Qdrant) with role filter + 0.92 threshold
    cache hit  → return cached answer via SSE, skip nodes 1–7
    cache miss → proceed to Classifier
              │
              ▼
[1. Classifier]                      ~200ms, 1 LLM call (CLASSIFIER_MODEL)
    ├── DIRECT      → rigid fallback, no retrieval, no LLM gen call
    │                 chitchat → polite redirect
    │                 technical but out-of-scope → honest decline
    │                 ("I can only answer from Whitecape's indexed documents.")
    ├── SIMPLE_RAG  → single retrieval path (MVP)
    └── COMPLEX_RAG → full agentic path with decomposition (post-MVP)

    MVP: binary classification — DIRECT vs SIMPLE_RAG only.
    COMPLEX_RAG requires 3-way classifier + decomposer + parallel search.
              │
              ▼
[2. Rewriter + Decomposer]           ~400ms, 1 LLM call (REWRITER_MODEL)
    One call, two jobs:
    - rewrites query for retrieval quality
    - decomposes into max 3 sub-queries (COMPLEX_RAG only, post-MVP)
    SIMPLE_RAG (MVP): rewrite only, no decomposition
              │
              ▼
[3. Parallel Qdrant Search]          ~100ms, no LLM cost
    asyncio.gather() fires all sub-queries concurrently
    BGE-M3 embeddings
    Qdrant FieldCondition role filter — HARD SECURITY BOUNDARY
              │
              ▼
[4. bge-reranker-v2-m3 via TEI]       ~150ms, no LLM cost
    cross-encoder reranker — scores (query, passage) pairs jointly
    hosted as Docker Compose service via Hugging Face TEI, CPU
    NOT Ollama — Ollama cannot run cross-encoder architectures
    scores and prunes all chunks across all sub-queries
              │
              ▼
[5. Relevance Gate]                  ~5ms, no LLM cost
    pure Python threshold check on reranker scores
              │
    pass ─────┴───── fail
      │                │
      │         [5b. Single Retry]
      │               broaden query, one attempt only
      │                │
      │         pass → Generator
      │         fail → honest fallback
      │               ("insufficient information in available documents")
      ▼
[6. Generator]                       ~1-3s BUFFERED, 1 LLM call (GENERATOR_MODEL)
    input: top-ranked chunks + original query + role persona system prompt
    output: BUFFERED complete answer + inline citations
    answer is held in memory — NOT streamed during generation
              │
              ▼
[7. Faithfulness Checker]            ~400ms, no LLM cost (token overlap heuristic MVP)
    verifies every claim is grounded in retrieved chunks
    runs on the COMPLETE buffered answer before any SSE output
              │
    faithful     → stream buffered answer to user via SSE
    not faithful → buffered answer DISCARDED
                   safe fallback + EscalationEvent written to PostgreSQL
```

**Total latency before stream starts (SIMPLE_RAG path): ~2.3–4.3s**
(buffered generation ~1-3s + faithfulness ~400ms + prior nodes ~900ms)
**DIRECT path: <50ms — canned response, no pipeline traversal.**
**Cache hit: ~50ms — cached answer returned via SSE, pipeline skipped.**

---

## 7. Node implementation map

| Node | File | Model env var | Notes |
|---|---|---|---|
| 0. Cache Check | app/pipeline/nodes/node_00_cache_check.py | — | Qdrant semantic_cache, role-scoped, 0.92 threshold |
| 1. Classifier | app/pipeline/nodes/node_01_classifier.py | CLASSIFIER_MODEL | Supports manual mode. MVP: binary DIRECT/SIMPLE_RAG |
| 2. Rewriter + Decomposer | app/pipeline/nodes/node_02_rewriter_decomposer.py | REWRITER_MODEL | MVP: rewrite only, no decomposition |
| 3. Parallel Qdrant Search | app/pipeline/nodes/node_03_parallel_qdrant_search.py | — | asyncio.gather, role filter |
| 4. Reranker | app/pipeline/nodes/node_04_reranker.py | — | bge-reranker-v2-m3 via TEI, CPU, POST /rerank |
| 5. Relevance Gate | app/pipeline/nodes/node_05_relevance_gate.py | — | Pure Python threshold |
| 5b. Retry | app/pipeline/nodes/node_05b_retry.py | — | One shot only |
| 6. Generator | app/pipeline/nodes/node_06_generator.py | GENERATOR_MODEL | Buffered output, SSE after faithfulness |
| 7. Faithfulness | app/pipeline/nodes/node_07_faithfulness.py | — | Token overlap heuristic MVP, no LLM model |
| Orchestration | app/pipeline/graph.py | — | LangGraph StateGraph |

---

## 8. Ingestion pipeline

Triggered by Admin. FastAPI publishes task to Redis, returns 202 immediately.
Celery picks up and runs ingestion independently.

**Chunking:** 800 tokens, 200 overlap, recursive splitting with BGE-M3 tokenizer.

Token counting uses BGE-M3's actual tokenizer, not character length.
Character count is a weak proxy — an 800-character chunk of dense prose and
an 800-character chunk of sparse bullet points tokenize to very different lengths.
Since BGE-M3 embeds the actual token sequence, inconsistent chunk sizes degrade
retrieval quality downstream.

```python
from transformers import AutoTokenizer
tokenizer = AutoTokenizer.from_pretrained("BAAI/bge-m3")
length_function = lambda text: len(tokenizer.encode(text))
# Pass length_function to RecursiveCharacterTextSplitter
```

Slightly more CPU per split, but ingestion is async via Celery — nothing waits
on it synchronously, so the cost is effectively zero.

**Chunk metadata (written to Qdrant payload at ingestion time):**
```python
{
    "document_id": str,           # UUID, FK to PostgreSQL Document
    "original_filename": str,
    "category": str,              # technical|quality|projects|company
    "allowed_roles": list[str],   # ["all"] or ["admin"] or specific roles
    "page_number": int,
    "chunk_index": int,
    "doc_hash": str               # SHA256
}
```

**Redis roles:**
- Message broker: FastAPI → Redis → Celery (AMQP)
- Result backend: Celery writes task status, FastAPI polls if needed

**Semantic cache invalidation:** on re-ingestion, cache entries in the Qdrant
`semantic_cache` collection whose `chunk_ids` overlap with changed chunks are
deleted automatically. See §9 for full cache design.

---

## 9. Semantic cache

Repeat queries with the same semantic intent skip the full pipeline and return
a cached answer directly. The cache lives in Qdrant (not Redis) as a separate
collection, enabling vector similarity lookup and role-scoped filtering in a
single query.

### Collection: `semantic_cache`

```python
{
    "query_text": str,              # original user query (for debugging/audit)
    "query_embedding": vector,      # BGE-M3 embedding of the query
    "answer_text": str,             # full generated answer (what gets streamed)
    "chunk_ids": list[str],         # Qdrant point IDs of chunks used to generate this answer
    "role": str,                    # user's primary role at query time
    "created_at": float,            # Unix timestamp
    "ttl_hours": int                # default 24
}
```

### Lookup logic (Node 0)

1. Compute BGE-M3 embedding of the incoming query.
2. Search `semantic_cache` with:
   - Vector similarity threshold: **0.92** (cosine)
   - Qdrant filter: `role` matches user's current primary role
3. **Cache hit** (score >= 0.92 + role match): return `answer_text` via SSE.
   Log to AuditLog with `cache_hit = true`. Skip nodes 1–7.
4. **Cache miss**: proceed to Node 1 (Classifier). After a successful
   FAITHFUL verdict at Node 7, write the new entry to `semantic_cache`.

Role scoping ensures a Developer never receives a cached answer that was
generated with QA-framed context, even if the query text is identical.

### TTL — Celery periodic task

A Celery beat task runs hourly and deletes entries where
`created_at + (ttl_hours * 3600) < now()`. Default TTL is 24 hours.

```python
# app/tasks/cache_cleanup.py
from celery import shared_task
from qdrant_client import models
import time

@shared_task
def purge_expired_cache():
    cutoff = time.time()
    client.delete(
        collection_name="semantic_cache",
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="created_at",
                        range=models.Range(lt=cutoff - 86400),  # default 24h
                    )
                ]
            )
        ),
    )
```

### Invalidation on re-ingestion

When a document is re-ingested, its old chunk IDs are replaced with new ones.
Any cache entry whose `chunk_ids` list overlaps with the old chunk IDs is stale
— the answer was grounded in content that no longer exists in its original form.

Invalidation runs as part of the ingestion Celery task, after new chunks are
written and before the task status is set to `done`:

1. Collect old chunk IDs being replaced.
2. Query `semantic_cache` for entries where `chunk_ids` overlaps with old IDs.
3. Delete matching entries.

This is precise — only cache entries that depended on changed content are
invalidated. Unrelated cached answers survive re-ingestion.

---

## 10. PostgreSQL schema

### Role (lookup table — not hardcoded)
```
id              UUID PK
name            string unique          # e.g. "developer", "qa_engineer", "project_manager"
description     text nullable
persona_prompt  text nullable          # role-specific system prompt template for generator
created_at      timestamp
```

Seed values: `developer`, `qa_engineer`. New roles are INSERTs, not migrations.

### User
```
id              UUID PK
email           string unique
keycloak_id     string unique
role_id         FK → Role.id
is_admin        boolean default false
is_owner        boolean default false     # seeded at deployment, not UI-manageable
created_at      timestamp
last_login      timestamp
```

**Partial unique index — enforces at most one Owner at DB level:**
```sql
CREATE UNIQUE INDEX idx_single_owner ON "user" (is_owner) WHERE is_owner = true;
```

### Document
```
id              UUID PK
source_path     string
original_filename string
category        enum(technical, quality, projects, company)
restricted      boolean default false
doc_hash        SHA256 string unique
uploaded_by     FK → User.id
last_ingested_at timestamp
chunk_count     integer
ingestion_status enum(pending, running, done, failed)
```

### AuditLog — append-only. No UPDATE. No DELETE. Ever.
```
id              UUID PK
user_id         FK → User.id
query_text      text
answer_text     text
confidence_score float
was_escalated   boolean
chunks_used     JSON array of Qdrant chunk IDs
namespace_queried string
response_time_ms integer
created_at      timestamp immutable
```

### EscalationEvent
```
id              UUID PK
audit_log_id    FK → AuditLog.id
reason          enum(faithfulness_failure, relevance_failure)
notified_at     timestamp
resolved_at     timestamp nullable
resolved_by     FK → User.id nullable
resolution_note text nullable
```

**Schema decisions — do not change without explicit instruction:**
- Roles are a lookup table, not an enum. Adding a role is an INSERT + Keycloak config.
  No code may hardcode role names — always resolve from the Role table or JWT claims.
- chunks_used is JSON, not a junction table. MVP simplification. Queryable in PostgreSQL.
- EscalationEvent is a separate table. Most AuditLog rows never escalate.
- AuditLog is append-only. Compliance requirement.
- Keycloak sync is lazy on login. No webhook infrastructure.

---

## 11. Loop engineering setup

Three roles. Three system prompts. Never mixed.

| Role | Model | Tool | Responsibility |
|---|---|---|---|
| Planner | Opus 4.8 | Claude Code | Writes contracts, decomposes tasks, verifies output |
| Generator | — | OpenCode | Reads contract, writes code and tests, runs tests during dev |
| Evaluator | Sonnet 4.6 | Claude Code | Reads test results + diff, checks assertions against contract, no fixes |

### State files (always on disk, never in context)

```
CLAUDE.md               this file — project harness
contracts/              one .md file per node — written by Planner
reviews/                generator summary per node — written by Generator
test_results/           pytest output per node — written by run_loop.sh
feature_list.json       done / in_progress / next / blocked
progress.md             current sprint, active contract, last verified state
log.md                  append-only — ## [date] op | node | PASS/FAIL
```

### Prerequisite — conftest.py contract (write before any node contract)

Multiple nodes share the same LangGraph state type, the same Qdrant client,
the same LiteLLM mock pattern, and the same TEI mock. Without a single source
of truth, each node's tests reinvent state shapes independently and they drift —
Node 3 writes `query`, Node 4 reads `original_query`, each test passes alone,
integration fails.

The Planner writes `contracts/conftest.md` first. It specifies:

- **PipelineState type:** every key name + type used across all nodes
- **State factory functions:** one per pipeline stage (empty, post-classifier,
  post-retrieval, post-rerank, post-generation) — tests import these instead
  of constructing state dicts by hand
- **Mock fixtures:**
  - `mock_litellm` — patches `acompletion`, returns configurable response shape
  - `mock_qdrant` — patches Qdrant client search, returns configurable hits
  - `mock_tei_rerank` — patches TEI `/rerank` call, returns configurable scores
- **Mock contract:** each mock specifies what it must preserve (return shape,
  required fields) — the content is test-provided, the structure is fixed

Generator implements `tests/conftest.py` from this contract before touching
any node. All node tests import from conftest — no node test creates its own
state dicts or mock patterns.

### Loop cycle per node

```
1. Planner (Opus) writes contract → contracts/node_NN_name.md
2. Generator (OpenCode) reads contract → implements node + tests
   runs tests during development for feedback
   writes summary → reviews/node_NN_name_summary.md
3. run_loop.sh runs pytest in clean state → test_results/node_NN.txt
   (neither agent runs this — the script is the neutral executor)
4. Evaluator (Sonnet) reads contract + diff + test_results/node_NN.txt
   checks each assertion: PASS or FAIL
   appends result → log.md
5a. PASS → update feature_list.json, move to next node
5b. FAIL → Generator gets one retry with failed assertions only
5c. Second FAIL → contract is wrong, Planner rewrites it, restart from 1
```

### Integration test contract — graph.py

Unit contracts verify nodes in isolation. The real failure mode in a LangGraph
pipeline is at the edges: state passing, conditional routing, and the SSE wrapper.
The Planner writes `contracts/graph_integration.md` with end-to-end assertions:

- DIRECT query never hits nodes 2–7, returns canned response
- SIMPLE_RAG query passes through nodes 0→1→2→3→4→5→6→7 in order
- Cache hit at Node 0 skips nodes 1–7, returns cached answer via SSE
- Faithfulness failure produces EscalationEvent, does not stream the answer
- Relevance gate failure + retry failure returns honest fallback
- State keys are preserved between nodes — no silent drops or renames
- Generator output is buffered, verified by faithfulness checker, then streamed via SSE

This contract is written after all node contracts are VERIFIED, not before.
It tests the wiring, not the node logic.

### Automation — run_loop.sh

```bash
#!/bin/bash
set -euo pipefail
NODE=$1
TIMESTAMP=$(date +%s)

# Step 1: Generator implements node + tests
opencode "Read contracts/${NODE}.md, contracts/conftest.md, \
and CLAUDE.md sections 6,7,11. \
Implement app/pipeline/nodes/${NODE}.py and tests/unit/test_${NODE}.py. \
Import all state factories and mock fixtures from tests/conftest.py. \
Write summary to reviews/${NODE}_summary.md. Do not touch any other file."

# Step 2: Verify expected files exist and were modified
for f in "app/pipeline/nodes/${NODE}.py" "tests/unit/test_${NODE}.py" \
         "reviews/${NODE}_summary.md"; do
    if [ ! -f "$f" ]; then
        echo "ABORT: Generator did not produce $f" >&2
        exit 1
    fi
    file_mtime=$(stat -c %Y "$f")
    if [ "$file_mtime" -lt "$TIMESTAMP" ]; then
        echo "ABORT: $f was not modified during this run" >&2
        exit 1
    fi
done

# Step 3: Run tests in clean state (neutral executor — neither agent)
mkdir -p test_results
pytest "tests/unit/test_${NODE}.py" -v --tb=short \
    2>&1 | tee "test_results/${NODE}.txt"
TEST_EXIT=${PIPESTATUS[0]}

# Step 4: Evaluator reads contract + diff + test results
claude -p "You are Evaluator. Read contracts/${NODE}.md, \
reviews/${NODE}_summary.md, app/pipeline/nodes/${NODE}.py, \
tests/unit/test_${NODE}.py, and test_results/${NODE}.txt. \
The test results file contains actual pytest output — do not assume \
tests passed, read the output. \
Check each contract assertion: PASS or FAIL. \
If FAIL quote the exact failing line or test output. \
Append to log.md: ## [$(date +%Y-%m-%d)] verify | ${NODE} | PASS or FAIL | failed: N,M \
Write nothing else. Do not fix code."

tail -1 log.md
```

Usage: `./run_loop.sh node_03_parallel_qdrant_search`

### Contract format (all contracts follow this structure exactly)

```markdown
# Contract — Node NN: Name
Status: DRAFT | APPROVED | VERIFIED
Last verified: —

## Interface
Function signature with full type hints. One sentence description.

## Inputs
Each parameter: name, type, valid range, error behavior if invalid.

## Outputs
Exact return type. Every field named and typed.
Explicit behavior on empty results — never None, never silent [].

## Environment
External dependencies this node touches at runtime:
- Service name, what it provides, protocol (e.g., TEI /rerank POST)
- Whether tests use real services or mocks
- Mock contract: what the mock must preserve (return shape, required fields)
Nodes with no external dependencies (e.g., relevance gate) state "none."

## Assertions
Numbered. Each is one independently testable fact.
No compound assertions. No "and". No "or".
Minimum 6, maximum 10.
Evaluator checks each as PASS or FAIL.

## Forbidden
Specific things this function must not do.
Reference CLAUDE.md section 12 where relevant.

## Test cases
Minimum 3, maximum 8. Sized to the node's branching complexity.
Each has: name, setup, call, expected output.
All test cases import state factories and mock fixtures from conftest.py.
```

---

## 12. Hard constraints — never override

- SSE is final. Do not suggest WebSocket.
- Do not stream before faithfulness verdict. Generator output is buffered in full,
  faithfulness checker runs on the complete answer, SSE only begins after FAITHFUL.
- All models run local via Ollama. No external API calls for LLM inference.
- All LLM calls go through LiteLLM. Do not import anthropic or openai directly in nodes.
- Qdrant role filter is a hard security boundary. Applied at query time inside node 3.
  Never filter returned results in Python after the fact.
- Role enforcement must never rely on prompt alone.
- AuditLog is append-only. No UPDATE. No DELETE. Ever.
- chunks_used stays as JSON array for MVP. Do not normalize prematurely.
- Document upload requires is_admin flag. Primary role alone never grants upload.
- Roles are dynamic (Role lookup table). No code may hardcode role names.
  Filter by user's role string, not by `if role == "developer"`.
  Role personas are stored in the Role table, not in code.
- Owner is seeded at deployment. Not assignable through the UI.
  Admin cannot grant or revoke is_owner. Only DB/init script can set it.
- Admin assignment (is_admin) requires is_owner. Admin cannot self-promote or promote others.
- BGE-M3 is the embedding model. Do not substitute text-embedding-3-large or similar.
- Reranker (bge-reranker-v2-m3) runs via Hugging Face TEI, not Ollama.
  Ollama cannot run cross-encoder architectures — it produces embeddings, not reranker scores.
- Chunking uses BGE-M3's tokenizer for length measurement, not character count.
- Lazy Keycloak sync on login is final. No webhook-based sync.
- Generator (OpenCode) does not modify CLAUDE.md, contracts/, or log.md.
- Evaluator (Sonnet) does not write code or run tests. It reads test results and reports assertions only.
- Telegram and WhatsApp are ruled out as access channels.
- No colors in diagrams. Black strokes, white fill, plain text only.
- All diagrams in .drawio format only.
- Frontend is HTMX + Jinja2 served by FastAPI. No separate frontend process.
  No React, no Next.js, no npm/node dependency.
- Auth is session-based (server-side session, HTTP-only cookie). No Bearer tokens in browser.
  CSRF middleware is mandatory on all state-changing endpoints.
- No Nginx. Use Caddy if TLS proxy is needed. Uvicorn serves directly otherwise.

---

## 13. Deployment strategy

**Primary interface:** Server-rendered HTMX + Jinja2 UI served directly by FastAPI.
Single container serves HTML, API, SSE, and static files. No separate frontend process.

**Internal network deployment (preferred):**
Deploy on intranet at knowledge.whitecape.internal.
Workers access via browser on company network or VPN.
Data never leaves the network.
Uvicorn with 4 workers handles up to 500+ concurrent connections.
No reverse proxy needed unless corporate policy requires internal TLS.

**External deployment (if no intranet):**
Private VPS behind knowledge.whitecape.com.
Caddy reverse proxy for automatic HTTPS via Let's Encrypt (3 lines of config).
Access restricted to Keycloak realm users.

**No Nginx.** Caddy replaces it when TLS is needed. For internal-only deployment
without TLS requirements, Uvicorn serves directly — no proxy at all.

**Messaging platforms:**
Slack bot acceptable as secondary channel post-MVP if company uses Slack heavily.
API layer is clean enough to support it without architectural changes.
SSE streaming is lost in Slack — full answer returned at once.
Telegram and WhatsApp: ruled out. Company data must not transit consumer platform servers.

---

## 14. MVP scope

| Feature | MVP | Full |
|---|---|---|
| Classifier (binary: DIRECT/SIMPLE_RAG) | yes | 3-way: DIRECT/SIMPLE_RAG/COMPLEX_RAG |
| DIRECT path (canned responses, no retrieval) | yes | yes |
| Manual classification mode | yes | yes |
| Rewriter (rewrite only) | yes | yes |
| COMPLEX_RAG path (decomposition + parallel search) | no | yes |
| Decomposer (multi sub-query) | no | yes |
| Parallel Qdrant search | no — single search | yes |
| Reranker (TEI) | yes | yes |
| Relevance gate + retry | yes | yes |
| Generator + buffered faithfulness + SSE | yes | yes |
| Faithfulness checker (token overlap) | yes | yes |
| Faithfulness checker (LLM call) | no | yes |
| Qdrant role filter | yes | yes |
| Role persona in prompt | yes | yes |
| Audit log | yes | yes |
| Escalation event | yes | yes |
| Rate limiting (Redis token bucket) | yes | yes |
| Semantic cache (Qdrant) | yes | yes |
| Cache TTL (Celery periodic purge) | yes | yes |
| Cache invalidation on re-ingestion | yes | yes |
| structlog | yes | yes |
| Prometheus + Grafana | no | yes |
| Slack integration | no | post-MVP |
| Category lookup table | no — enum | yes |

---

## 15. Diagrams (draw.io format, editable at diagrams.net)

| Diagram | File | Status |
|---|---|---|
| Use Case | use_case_diagram.drawio | Done |
| Component | component_diagram.drawio | Done |
| Sequence | sequence_diagram.drawio | Done |
| ERD | erd.drawio | Not yet produced |

---

## 16. Open decisions

All decisions closed.

| # | Decision | Resolution |
|---|---|---|
| 1 | Default classifier for MVP | ollama/qwen2.5:7b local. All models local, no external API. |
| 2 | Faithfulness checker MVP | Token overlap heuristic. No LLM call, no model needed. |
| 3 | Semantic cache key structure | Qdrant `semantic_cache` collection, BGE-M3 embedding, 0.92 cosine threshold, role-scoped. See §9. |

---

## 17. Frontend conventions — component-based HTMX architecture

The frontend follows a **component-based structure** analogous to React's design.
HTMX + Jinja2 replaces React, but the same principles apply: reusable components,
parameterized props, layout composition, and targeted re-renders.

### Directory structure

```
app/templates/
├── base.html                  # Root layout — nav, sidebar, {% block content %}, footer
├── pages/                     # Full page renders (one per route, extends base.html)
│   ├── dashboard.html
│   ├── login.html
│   ├── search.html
│   └── admin/
│       ├── documents.html
│       └── users.html
├── partials/                  # HTMX swap targets — returned alone on hx-* requests
│   ├── search_results.html
│   ├── chat_response.html
│   ├── document_list.html
│   └── upload_status.html
├── components/                # Reusable {% include %} fragments (stateless)
│   ├── message_bubble.html
│   ├── document_card.html
│   ├── search_bar.html
│   ├── pagination.html
│   └── alert.html
└── macros/                    # Parameterized Jinja2 macros (like React utility components)
    ├── forms.html             # form_field(name, type, label, error)
    ├── buttons.html           # btn(text, variant, hx_attrs)
    └── icons.html             # icon(name, size)
```

### Concept mapping

| React concept | HTMX + Jinja2 equivalent |
|---|---|
| Component file (`Button.tsx`) | Jinja2 macro or `{% include "components/..." %}` |
| Props | Macro parameters or template context variables |
| Page / Route component | Template in `pages/` extending `base.html` |
| `useState` + re-render | HTMX `hx-get`/`hx-post` → server returns partial → swaps target div |
| Layout / App shell | `base.html` with `{% block content %}` |
| Lazy loading | `hx-trigger="revealed"` |
| SPA-like navigation | `hx-boost="true"` on `<body>` or nav links |
| Loading state | `hx-indicator` pointing to a spinner element |

### Rules (non-negotiable)

1. **Pages never contain reusable markup.** They compose from components and macros.
   If markup appears in two pages, extract it to `components/` or `macros/`.

2. **Partials are the only thing HTMX swap targets return.** A route handler
   serving an `hx-*` request returns a partial template, never a full page.

3. **Components are stateless.** Data comes from template context (set by the
   route handler), never from global state or Jinja2 globals.

4. **One partial per swap target.** If a page has 3 independently updating
   regions, that's 3 partials and 3 route handlers.

5. **Full-page vs partial detection pattern** — every route that serves both
   full-page loads and HTMX partial swaps uses this:

```python
@router.post("/search")
async def search(request: Request, query: str = Form(...)):
    results = await run_pipeline(query, user=request.state.user)

    # HTMX request → return only the partial
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/search_results.html", {
            "request": request, "results": results
        })

    # Full page load (bookmark, refresh) → return complete page
    return templates.TemplateResponse("pages/search.html", {
        "request": request, "results": results
    })
```

6. **Macros are the Jinja2 equivalent of React prop-based components:**

```html
{# macros/forms.html #}
{% macro form_field(name, label, type="text", error=None, required=False) %}
<div class="form-group {{ 'has-error' if error }}">
    <label for="{{ name }}">{{ label }}</label>
    <input type="{{ type }}" id="{{ name }}" name="{{ name }}"
           {{ 'required' if required }} class="form-input">
    {% if error %}<span class="error-text">{{ error }}</span>{% endif %}
</div>
{% endmacro %}
```

7. **No inline JavaScript.** All interactivity goes through HTMX attributes
   or Alpine.js (x-data) for client-side state that doesn't need a server round-trip
   (modals, dropdowns, toggles). If Alpine is used, it lives in `app/static/js/`.

8. **CSS follows the same component structure.** One CSS file per logical group,
   not one mega-stylesheet. Use `app/static/css/` with files like `base.css`,
   `components.css`, `pages/search.css`. No CSS frameworks unless explicitly approved.

---

## 18. Git branching strategy

### Branch model

```
main ──────────────────────────── stable, deployable, supervisor-facing
  │
  └── develop ─────────────────── integration branch, all features merge here
        │
        └── feature/<slug> ────── one branch per contract/node/feature
```

### Naming convention

| Branch type | Pattern | Example |
|---|---|---|
| Integration | `develop` | `develop` |
| Pipeline node | `feature/node-NN-name` | `feature/node-01-classifier` |
| Infrastructure | `feature/infra-description` | `feature/infra-keycloak-session` |
| Frontend | `feature/frontend-scope` | `feature/frontend-search-page` |
| Auth | `feature/auth-description` | `feature/auth-keycloak` |
| Bugfix | `fix/short-description` | `fix/cache-ttl-calculation` |

### Rules (non-negotiable)

1. **Never push directly to `main`** after the skeleton phase (current state).
   All work goes through `develop` via feature branches.

2. **Feature branches come off `develop`, merge back to `develop`.**
   Never branch from `main` for feature work.

3. **One feature branch per contract/node.** Maps 1:1 to the loop engineering cycle.
   A branch is created when the contract is written, merged when VERIFIED.

4. **Squash-merge features into develop.** One commit per feature in develop's history.
   ```bash
   git checkout develop
   git merge --squash feature/node-01-classifier
   git commit -m "Node 01: Classifier — binary DIRECT/SIMPLE_RAG classification"
   ```

5. **Merge `develop → main` only at milestones.** Main represents "this works end-to-end."
   Milestones: pipeline complete, auth + frontend usable, full MVP.

6. **Delete feature branches after merge.** No stale branches.

7. **Commit messages follow this format:**
   - Feature merge into develop: `Node NN: Name — one-line summary`
   - Milestone merge into main: `Milestone: description`
   - Within feature branches: free-form, but meaningful (not "wip" or "fix")

### Implementation timeline (phased)

```
Phase 1 — Foundation [DONE]
  main: skeleton, diagrams, docker-compose, core ABCs, service concretes

Phase 2 — Pipeline nodes
  develop ← feature/conftest-and-state        (FIRST — all nodes depend on it)
  develop ← feature/node-01-classifier
  develop ← feature/node-02-rewriter
  develop ← feature/node-03-search
  develop ← feature/node-04-reranker
  develop ← feature/node-05-relevance-gate
  develop ← feature/node-06-generator
  develop ← feature/node-07-faithfulness
  develop ← feature/node-00-cache             (depends on cache collection existing)
  develop ← feature/graph-integration         (LAST — wires all nodes together)
  main ← develop                              (MILESTONE: pipeline works)

Phase 3 — Auth + Frontend
  develop ← feature/auth-keycloak
  develop ← feature/frontend-base             (base.html, macros, login page)
  develop ← feature/frontend-search           (search page, SSE streaming)
  main ← develop                              (MILESTONE: usable MVP)

Phase 4 — Ingestion + Admin
  develop ← feature/ingestion-pipeline
  develop ← feature/admin-ui
  develop ← feature/cache-and-rate-limit
  main ← develop                              (MILESTONE: full MVP)
```

### Workflow per feature (practical)

```bash
# Start
git checkout develop && git pull
git checkout -b feature/node-01-classifier

# Work (loop engineering cycle: contract → implement → test → verify)
git add -A && git commit -m "Implement classifier node with binary routing"

# Finish (after VERIFIED in log.md)
git checkout develop && git pull
git merge --squash feature/node-01-classifier
git commit -m "Node 01: Classifier — binary DIRECT/SIMPLE_RAG classification"
git push origin develop
git branch -d feature/node-01-classifier
git push origin --delete feature/node-01-classifier
```
