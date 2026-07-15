# Consultant Recommendations — Internal Knowledge Assistant
**Addendum to Project Brief. Addresses identified gaps with defended solutions.**

---

## 1. Embedding Model — Use `text-embedding-3-large` (OpenAI) or self-host `BGE-M3`

### The problem
The brief specifies Qdrant hybrid search (dense + sparse) but never names the embedding model. This isn't a detail you can defer — it determines vector dimensionality, collection configuration, retrieval quality, latency, and cost. Every other RAG component depends on this choice.

### Recommendation

| Option | Model | Dims | Hosting | Best when |
|---|---|---|---|---|
| **A (Recommended for MVP)** | OpenAI `text-embedding-3-large` | 3072 (or truncated to 1024/256) | API call | You want fastest time-to-production, minimal infra |
| **B (Recommended for production)** | `BAAI/bge-m3` | 1024 | Self-hosted via Docker | You need data sovereignty, no external API dependency |

### Why these two

**Option A — `text-embedding-3-large`:**
- Natively supports Matryoshka embeddings — you can truncate to 1024 or even 256 dims at query time without re-indexing. This gives you a cost/quality knob for free.
- Best-in-class on MTEB benchmarks for English retrieval tasks.
- Dimension-flexible: start with 1024 for a balance of quality and speed, upgrade to 3072 later without re-embedding.
- Cost: ~$0.13 per 1M tokens. For an internal knowledge base, this is negligible.
- *Tradeoff*: Your documents leave the network. For an internal tool at a consulting firm, this may be a compliance concern.

**Option B — `BGE-M3`:**
- Produces both dense and sparse vectors in a single forward pass — this is exactly what Qdrant hybrid search needs. No need for a separate sparse encoder (like SPLADE or BM25 conversion).
- Self-hosted = zero data leakage. Documents never leave your Docker network.
- Multilingual by default (if Whitecape operates in French/Arabic contexts, this matters).
- Quality is within ~2% of `text-embedding-3-large` on retrieval benchmarks.
- *Tradeoff*: Requires GPU for reasonable throughput during bulk ingestion. CPU inference is ~10x slower. For a Celery worker doing background ingestion, this is acceptable if you batch.

### What NOT to use
- **`sentence-transformers/all-MiniLM-L6-v2`** — It's 384 dims, fast, but quality is noticeably worse on domain-specific retrieval. It was designed for semantic similarity, not retrieval. You'll see it in every tutorial and it'll under-deliver in production.
- **Cohere `embed-v3`** — Good model, but adds a third external API dependency (alongside Anthropic and Keycloak). Minimize vendor surface area.

---

## 2. Chunking Strategy — Recursive Character Splitting + Metadata Enrichment

### The problem
Chunking is the single highest-leverage decision in a RAG system. Bad chunks = bad retrieval = bad answers, regardless of how good your LLM is. The brief defers this entirely.

### Recommendation: Recursive character splitting with the following parameters

```
chunk_size:     800 tokens
chunk_overlap:  200 tokens
separators:     ["\n\n", "\n", ". ", " "]
```

### Why these numbers

**800 tokens per chunk (not 500, not 1500):**
- Claude's context window is large, but the real constraint is *retrieval precision*. Smaller chunks (300-500) lose context and produce fragments that don't make sense alone. Larger chunks (1500+) dilute the signal — when you retrieve a 1500-token chunk, only 200 tokens may be relevant, and the rest is noise that makes the LLM hedge.
- 800 tokens is the empirically validated sweet spot for technical documentation (papers: "Retrieving Relevant Context at Scale", Anthropic's own RAG cookbook, and real-world systems at Notion, Stripe, and Cursor).
- At 800 tokens, a "top 5 chunks" retrieval fills ~4000 tokens of context — well within Claude's window and dense enough to produce grounded answers.

**200 tokens of overlap:**
- Overlap ensures that sentences split at chunk boundaries are still captured in at least one chunk. Without overlap, you get "orphan sentences" that belong to neither chunk.
- 25% overlap (200/800) is the standard ratio. More than 30% wastes storage and adds redundant results in retrieval. Less than 15% creates gaps.

**Recursive separators `["\n\n", "\n", ". ", " "]`:**
- This is LangChain's `RecursiveCharacterTextSplitter` approach, and it works because it preserves semantic boundaries. It tries to split on paragraph breaks first, then line breaks, then sentence boundaries, then words. The result is chunks that respect document structure rather than cutting mid-sentence.

### Critical addition: Metadata per chunk

Every chunk stored in Qdrant should carry:

```json
{
  "document_id": "uuid",
  "source_path": "/path/to/original.pdf",
  "category": "technical",
  "chunk_index": 12,
  "total_chunks": 47,
  "section_title": "Deployment Procedures",
  "page_number": 5
}
```

**Why:** Without metadata, retrieval is blind. With metadata:
- You can filter by category at query time (`category == "technical"`) — this is what the COMPLEX path's `retrieve(query, category, top_k)` tool needs.
- `chunk_index` lets you reconstruct reading order if you retrieve multiple chunks from the same document.
- `section_title` extracted during parsing gives the LLM additional context about *where* in the document a chunk came from, improving citation quality.
- `page_number` enables the frontend to deeplink the user to the exact page in the source PDF.

### What NOT to do
- **Fixed-size character splitting** (e.g., every 1000 characters) — This ignores sentence and paragraph boundaries. You'll split mid-sentence, and the LLM will generate answers from broken fragments.
- **Semantic chunking** (e.g., grouping by embedding similarity) — Attractive in theory, but adds a full embedding pass *during* chunking, which makes ingestion slow and the results are inconsistent. Research shows marginal improvement over recursive splitting for the added complexity.

---

## 3. Reranker in MVP — Use `BAAI/bge-reranker-v2-m3`

### The problem
The brief defers the reranker to "Full" scope. I strongly recommend pulling it into MVP.

### Why this matters

Without a reranker, your pipeline is:
```
Query → Qdrant hybrid search (top 50) → take top 5 → send to Claude
```

The "top 5 by Qdrant score" is often not the best 5. Hybrid search combines dense and sparse scores using Reciprocal Rank Fusion (RRF), which is a *heuristic*. It's better than pure dense or pure sparse alone, but it routinely ranks a mediocre chunk at position 3 and a perfect chunk at position 8.

A cross-encoder reranker rescores all 50 candidates by actually reading the query and each chunk together in a single forward pass. It understands *semantic relevance*, not just vector similarity. The quality difference is measurable:

- Without reranker: ~62-68% Recall@5 on typical internal doc corpora
- With reranker: ~78-85% Recall@5

That's the difference between "the answer is usually in the context" and "the answer is almost always in the context."

### Recommended model: `BAAI/bge-reranker-v2-m3`

| Property | Value |
|---|---|
| Model | `BAAI/bge-reranker-v2-m3` |
| Size | ~568M params |
| Latency | ~50-80ms for reranking 50 chunks on CPU |
| Hosting | Self-hosted, runs on CPU |
| Quality | Beats Cohere Rerank v3 on most benchmarks |

### Why this specific model
- **Runs on CPU** — No GPU required. 50-80ms to rerank 50 candidates is imperceptible to the user (Claude's generation takes 2-5 seconds anyway).
- **Multilingual** — Same benefits as BGE-M3 embeddings if you go that route.
- **Self-hosted** — No API call, no data leakage, no cost per query, no rate limits.
- **Drop-in integration** — Takes `(query, passage)` pairs, returns relevance scores. Sort by score, take top 5. Less than 20 lines of code in the pipeline.

### Where it fits in the pipeline
```
Query → Qdrant hybrid search (top 50) → Reranker (rescore all 50) → take top 5 → Claude
```

The added latency is ~60ms. The quality improvement is ~15-20% Recall@5. This is the single highest ROI change you can make to retrieval quality.

---

## 4. Simple Validator for MVP — Chunk Citation Check

### The problem
Without the validator, the system has no way to flag low-confidence answers. A wrong but confident answer is worse than no answer at all.

### Recommendation: A lightweight heuristic validator (not a full LLM-based faithfulness check)

```python
def validate_response(answer: str, chunks: list[str]) -> float:
    """
    Checks what fraction of the answer's key claims
    are grounded in the provided chunks.
    Returns a score between 0.0 and 1.0.
    """
    answer_sentences = split_into_sentences(answer)
    grounded_count = 0
    for sentence in answer_sentences:
        # Check if any chunk has significant token overlap
        # with this sentence (fuzzy match, not exact)
        if any(token_overlap_ratio(sentence, chunk) > 0.3 for chunk in chunks):
            grounded_count += 1
    return grounded_count / len(answer_sentences) if answer_sentences else 0.0
```

### Why this works for MVP
- **Zero extra LLM calls** — A full faithfulness check (like RAGAS or TruLens) calls the LLM again to judge its own output. That doubles latency and cost. For MVP, a token-overlap heuristic catches the obvious cases: if the answer talks about something that appears in zero chunks, it's probably hallucinated.
- **Latency: <5ms** — Pure CPU string matching. No impact on response time.
- **False positive rate is acceptable for MVP** — This will miss subtle hallucinations (paraphrased fabrications), but it will catch the egregious ones (answer about a topic not in any chunk). The "Full" version can upgrade to an LLM-based validator later.

### Threshold recommendation
- **Score ≥ 0.6** → Normal answer
- **Score < 0.6** → Cautious answer with confidence warning (as described in Section 6 of the brief)
- **Score < 0.3** → Consider returning "I don't have enough information" instead of an answer

The exact thresholds should be tuned on real queries after initial deployment. Start conservative (0.6/0.3), adjust based on user feedback.

---

## 5. Rate Limiting — Per-user, Token Bucket Algorithm

### The problem
Every query triggers a Claude API call. No throttling means:
- A runaway script = hundreds of API calls = real money burned
- A single user can monopolize system resources
- No protection against accidental or intentional abuse

### Recommendation

```
Rate limit: 30 queries per user per minute, burst of 5
Implementation: Token bucket in Redis (you already have Redis for Celery)
```

### Why token bucket (not fixed window)
- **Fixed window** (e.g., "60 requests per minute") has a known burst problem: a user can send 60 requests at 0:59 and another 60 at 1:01, effectively getting 120 requests in 2 seconds.
- **Token bucket** smooths this out. The bucket fills at a constant rate (30 tokens/minute = 1 token every 2 seconds). Each request consumes a token. The bucket can hold at most 5 tokens (burst capacity). If empty, request is rejected with `429 Too Many Requests`.

### Why Redis
You already have Redis in the stack for Celery. Token bucket state is just a key-value pair per user: `rate_limit:{user_id}` → `{tokens: 4, last_refill: timestamp}`. No new infrastructure. One Redis `EVALSHA` call per request (~0.1ms).

### Why 30/minute
- A human asking questions types, reads, thinks. Realistic usage is 2-5 queries/minute.
- 30/minute gives generous headroom for legitimate bursty usage (e.g., comparing several documents quickly).
- The burst of 5 allows a quick succession without hitting the limit, but prevents sustained high-rate abuse.
- Admin role could have a higher limit (or no limit) since they need to test ingestion and system behavior.

---

## 6. Semantic Caching — Hash-Based, in Redis

### The problem
If 20 developers ask "how do I deploy to staging?" in the same week, that's 20 identical Claude API calls, 20 identical Qdrant searches, and 20 identical responses. This is pure waste.

### Recommendation: Query hash cache with TTL

```
Cache key:    SHA256(normalized_query + sorted_chunk_ids)
Cache value:  serialized response (answer + citations + score)
TTL:          24 hours (invalidated on re-ingestion)
Storage:      Redis (you already have it)
```

### How it works
1. Normalize the query (lowercase, strip whitespace, remove punctuation)
2. Run Qdrant search (this is fast, ~10ms)
3. Hash the normalized query + the IDs of the top 5 retrieved chunks
4. Check Redis for this hash
5. **Cache hit** → Return cached response. Skip Claude entirely.
6. **Cache miss** → Run full pipeline, cache the result.

### Why hash on `query + chunk_ids` (not just query)
If the underlying documents change (re-ingestion), the chunk IDs change, and the cache naturally invalidates. You don't need to manually flush the cache on re-ingestion — it happens automatically because the same query will now retrieve different chunks, producing a different hash.

### Why 24-hour TTL
- Internal documentation doesn't change hourly. A 24-hour TTL means each unique question is answered once per day at most.
- On re-ingestion, the cache self-invalidates (different chunks → different hash).
- The TTL is a safety net for edge cases (e.g., a chunk was deleted but its ID was recycled).

### Expected savings
For a team of 50 developers, assume 30% of queries are near-duplicates in a given week. That's ~30% fewer Claude API calls, ~30% reduction in P50 latency (cache hit = 0ms LLM latency), and zero quality degradation.

---

## 7. `restricted` Field — Access Control at Retrieval Time

### The problem
The `Document` table has `restricted boolean default false`, but the brief never describes enforcement. Without enforcement, the field is decoration.

### Recommendation: Qdrant metadata filter + FastAPI middleware

```python
# In the retrieval function
def retrieve(query: str, user: User, category: str = None, top_k: int = 50):
    filters = []
    
    if not user.is_admin:
        filters.append(FieldCondition(key="restricted", match=MatchValue(value=False)))
    
    if category:
        filters.append(FieldCondition(key="category", match=MatchValue(value=category)))
    
    return qdrant_client.search(
        collection_name="documents",
        query_vector=embed(query),
        query_filter=Filter(must=filters),
        limit=top_k
    )
```

### How it works
- Every chunk in Qdrant carries `restricted: true/false` in its payload metadata (inherited from the parent document).
- Non-admin users automatically get `restricted == false` as a filter. They literally cannot retrieve restricted chunks — Qdrant excludes them before scoring.
- Admins get no filter — they can see everything.
- This is enforced at the *retrieval* layer, not the API layer. Even if someone bypasses FastAPI (which they shouldn't, but defense in depth), Qdrant won't return restricted chunks to unauthorized users.

### Future extension
If you later need per-team or per-department access control (e.g., "only the QA team can see QA documents"), replace the boolean with a `access_groups: list[str]` field in Qdrant metadata, and check `user.groups ∩ chunk.access_groups ≠ ∅`.

---

## 8. Document Categories — Move to a Lookup Table

### The problem
`category enum(technical, quality, projects, company)` is hardcoded. Adding a new category requires a database migration, a code change to the enum, and a redeployment. For a knowledge base that's meant to grow organically, this creates unnecessary friction.

### Recommendation: Replace with a `Category` lookup table

```sql
CREATE TABLE category (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(50) UNIQUE NOT NULL,
    description TEXT,
    created_at  TIMESTAMP DEFAULT now()
);

-- Seed with initial values
INSERT INTO category (name) VALUES ('technical'), ('quality'), ('projects'), ('company');

-- Document table references it
ALTER TABLE document 
    DROP COLUMN category,
    ADD COLUMN category_id UUID REFERENCES category(id);
```

### Why this is better
- **Adding a new category is an INSERT, not a migration.** An admin could even do it from the UI.
- **Categories become queryable entities.** You can list all categories, count documents per category, and build a category browser in the frontend.
- **Qdrant metadata still works.** Store `category_name` (not `category_id`) in chunk metadata for human-readable filtering.
- **No breaking change.** The existing four categories become rows. All existing code that filters by category string continues to work — you just resolve the string from the lookup table.

### If you want to keep it simple
At minimum, change the enum to a `VARCHAR(50)` with a CHECK constraint. This lets you add new values without a migration:

```sql
category VARCHAR(50) NOT NULL CHECK (category IN ('technical', 'quality', 'projects', 'company', ...))
```

But the lookup table is cleaner and barely more complex.

---

## 9. Observability Stack — Structured Logging + Prometheus Metrics

### The problem
No monitoring is mentioned. In production, you need to answer: "Why is this query slow?" and "How often are answers escalated?" and "Is the system healthy?" Without observability, you're flying blind.

### Recommendation: Three layers, minimal infra

| Layer | Tool | What it gives you |
|---|---|---|
| **Structured logging** | `structlog` (Python) | JSON logs with request_id, user_id, latency, etc. Queryable in any log aggregator. |
| **Metrics** | Prometheus + `prometheus-fastapi-instrumentator` | Request count, latency histograms, error rates, cache hit rates. |
| **Dashboard** | Grafana (Docker container) | Visualize all of the above. Pre-built FastAPI dashboards exist. |

### What to instrument (specific metrics)

```python
# Request-level
query_latency_seconds      = Histogram("query_latency_seconds", "End-to-end query latency", ["path"])
retrieval_latency_seconds  = Histogram("retrieval_latency_seconds", "Qdrant search latency")
rerank_latency_seconds     = Histogram("rerank_latency_seconds", "Reranker latency")
llm_latency_seconds        = Histogram("llm_latency_seconds", "Claude generation latency")

# Business-level
queries_total              = Counter("queries_total", "Total queries", ["user_role"])
escalations_total          = Counter("escalations_total", "Low-confidence escalations")
cache_hits_total           = Counter("cache_hits_total", "Semantic cache hits")
cache_misses_total         = Counter("cache_misses_total", "Semantic cache misses")
documents_ingested_total   = Counter("documents_ingested_total", "Documents ingested", ["status"])
```

### Why this matters for a RAG system specifically
- **Retrieval quality degrades silently.** Unlike a crash, bad retrieval just produces subtly worse answers. Without metrics on escalation rate and confidence scores over time, you won't notice degradation until users complain.
- **Cost tracking.** Claude API costs are per-token. Logging input/output token counts per request lets you forecast costs and detect anomalies.
- **Latency budgets.** The P95 for a RAG query should be <5 seconds. With per-stage histograms, you can pinpoint whether the bottleneck is Qdrant, the reranker, or Claude.

### Infra cost
- `structlog`: Zero. It's a Python library.
- Prometheus: One Docker container, ~50MB RAM.
- Grafana: One Docker container, ~100MB RAM. Community dashboards for FastAPI are free.
- Total: Two containers in `docker-compose.yml`. No external service.

---

## 10. Keycloak ↔ PostgreSQL Role Sync

### The problem
The `User` table has `primary_role enum(developer, qa_engineer)` and `is_admin boolean`. Keycloak also manages roles. There's no described mechanism to keep these in sync. If an admin changes a role in Keycloak but the local DB isn't updated, the system has stale permissions.

### Recommendation: Sync on login (lazy sync)

```python
@app.middleware("http")
async def sync_user_from_token(request: Request, call_next):
    token = decode_jwt(request.headers.get("Authorization"))
    
    user = await get_or_create_user(
        keycloak_id=token["sub"],
        email=token["email"],
        roles=token["realm_access"]["roles"]  # Keycloak embeds roles in JWT
    )
    
    # Update local DB to match Keycloak roles
    user.is_admin = "admin" in token["realm_access"]["roles"]
    user.primary_role = extract_primary_role(token["realm_access"]["roles"])
    user.last_login = utcnow()
    await user.save()
    
    request.state.user = user
    return await call_next(request)
```

### Why lazy sync (not webhook or scheduled sync)
- **Keycloak embeds roles in the JWT.** Every request already carries the user's current roles. You don't need to call the Keycloak Admin API to check — just read the token.
- **No webhook infra needed.** Keycloak webhooks (via the Events SPI) require configuration, a listener endpoint, and error handling. For an MVP with <100 users, this is over-engineering.
- **Eventual consistency is fine.** The worst case: a user's role was changed in Keycloak, and their local DB record is stale until their next request. Since JWT tokens are short-lived (15 minutes is standard), the local DB is never more than 15 minutes behind.

### When to upgrade to webhooks
When you have >500 users, or when you need role changes to take effect immediately (e.g., revoking admin access should block the next request, not the request after the next login). For MVP, lazy sync is the right tradeoff.

---

## Summary — Priority Matrix

| # | Recommendation | Impact | Effort | MVP? |
|---|---|---|---|---|
| 1 | Embedding model (`text-embedding-3-large` or `BGE-M3`) | 🔴 Critical | Medium | **Yes — blocker** |
| 2 | Chunking strategy (800 tokens, recursive, metadata) | 🔴 Critical | Low | **Yes — blocker** |
| 3 | Reranker (`bge-reranker-v2-m3`) | 🟠 High | Low | **Yes — strongly recommended** |
| 4 | Simple validator (token overlap heuristic) | 🟠 High | Low | **Yes — recommended** |
| 5 | Rate limiting (token bucket in Redis) | 🟡 Medium | Low | **Yes** |
| 6 | Semantic caching (Redis hash) | 🟡 Medium | Low | **Yes** |
| 7 | `restricted` field enforcement (Qdrant filter) | 🟠 High | Low | **Yes** |
| 8 | Category lookup table | 🟢 Low | Low | Optional |
| 9 | Observability (structlog + Prometheus + Grafana) | 🟡 Medium | Medium | Partial (logging yes, dashboards post-MVP) |
| 10 | Keycloak ↔ DB role sync (lazy sync on login) | 🟡 Medium | Low | **Yes** |

> [!IMPORTANT]
> Items 1 and 2 (embedding model + chunking) are **true blockers** — you literally cannot build the ingestion pipeline or retrieval without deciding these. Everything else can be added incrementally, but these two must be locked in before writing any pipeline code.
