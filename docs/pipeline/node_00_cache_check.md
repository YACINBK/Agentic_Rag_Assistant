# Node 00 — Cache Check

**File:** `app/pipeline/nodes/node_00_cache_check.py` · **Class:** `CacheCheckNode` · **LLM:** none

---

## 1. Role

The first node in the pipeline. It checks whether a semantically-equivalent query has
already been answered for this role, and if so returns the cached answer directly —
skipping nodes 1–7 entirely. This is the cheapest possible path (~50 ms, no LLM call).

## 2. Interface

```python
CacheCheckNode(embedder: BaseEmbedder, vector_store: BaseVectorStore, settings: Settings)
name -> "cache_check"
```

## 3. State contract

| Reads | Writes |
|---|---|
| `query`, `user_role` | `cache_hit: bool`, `cached_answer: str` (hit only) |

## 4. Behaviour

1. Embed the **original** query with BGE-M3 (not the rewritten one — rewriting happens
   later, at Node 02).
2. Search the `semantic_cache` Qdrant collection, filtered to the user's role, `limit=1`.
3. If the top result's score `>= CACHE_SIMILARITY_THRESHOLD` (default **0.92**, cosine) →
   **cache hit**: write `cache_hit=True` and `cached_answer`.
4. Otherwise → **cache miss**: write `cache_hit=False`.

## 5. Routing implications

`route_cache` in `graph.py`: `cache_hit` true → `END` (answer already in state);
false → `classifier`. A hit means nodes 1–7 never run.

## 6. Error handling & edge cases

Fail-open — a cache is an optimisation, never a dependency. Both failure modes degrade
gracefully to a miss so the real pipeline still runs:

- **Embedder failure** (Ollama down/slow) → `cache_hit=False`.
- **Vector-store failure** (Qdrant down/timeout) → `cache_hit=False`.

> This second guard was added during the 2026-07-22 review. Previously a Qdrant failure
> here propagated uncaught and crashed the whole pipeline at the very first node.

- **No results / score below threshold** → `cache_hit=False`, `cached_answer` unset.

## 7. Design decisions

| Decision | Rationale |
|---|---|
| Cache lives in Qdrant, not Redis | Enables vector-similarity lookup + role filtering in one query (CLAUDE.md §9). |
| Role-scoped filter | A Developer must never receive a QA-framed cached answer, even for identical text (CLAUDE.md §9). |
| Embed the original query | The cache is keyed on user intent, which the raw query expresses; rewriting is a retrieval concern. |
| 0.92 threshold | High enough that only genuine semantic duplicates hit; tunable via settings. |

## 8. Constraints honoured

- BGE-M3 embeddings via injected `BaseEmbedder` (CLAUDE.md §12).
- Role scoping enforced at query time in Qdrant, not in Python (CLAUDE.md §5).
- Immutable state update.

## 9. Test coverage (`tests/unit/test_node_00_cache.py`)

| Test | Proves |
|---|---|
| `test_cache_hit_returns_cached_answer` | Score ≥ threshold → hit + answer surfaced |
| `test_cache_miss_when_score_below_threshold` | Below threshold → miss, no answer |
| `test_cache_miss_when_no_results` | Empty result set → miss |
| `test_embedder_failure_returns_miss` | Embed exception → miss (fail-open) |
| `test_vector_store_failure_returns_miss` | Search exception → miss (fail-open) |
| `test_role_scoped_cache_search` | Filters by role, targets `semantic_cache` |
