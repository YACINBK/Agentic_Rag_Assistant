# Node 03 — Qdrant Search

**File:** `app/pipeline/nodes/node_03_parallel_qdrant_search.py` · **Class:** `QdrantSearchNode` · **LLM:** none

---

## 1. Role

Embeds the rewritten query and retrieves candidate chunks from Qdrant, filtered to the
documents the user's role is allowed to see. **This node is the hard security boundary of
the whole system** (CLAUDE.md §5): the role filter is applied inside the Qdrant query, so
the LLM downstream physically never sees a chunk the user cannot access.

The filename carries `parallel_` because the full system fans out over decomposed
sub-queries with `asyncio.gather`. **That is post-MVP** — the MVP performs a single search.

## 2. Interface

```python
QdrantSearchNode(embedder: BaseEmbedder, vector_store: BaseVectorStore, settings: Settings)
name -> "qdrant_search"
```

## 3. State contract

| Reads | Writes |
|---|---|
| `rewritten_query`, `user_role` | `retrieved_chunks: list[ChunkPayload]` |

## 4. Behaviour

1. Validate `rewritten_query` is present and non-empty → else raise `RetrievalError`.
2. Build the role filter: `allowed_roles = [user_role, "all"]` — the user's own role plus
   documents tagged for everyone.
3. Embed the rewritten query with BGE-M3.
4. Search the `documents` collection with the role filter and `QDRANT_SEARCH_LIMIT`.
5. Return `retrieved_chunks` (may be an empty list — that is a valid outcome, not an error).

## 5. Routing implications

None directly — `qdrant_search → reranker` is unconditional. It is also the loop-back
target for Node 05b (Retry): a broadened query re-enters here.

## 6. Error handling & edge cases

This is a **safety barrier**, so it fails **closed** with a typed error rather than
silently returning nothing:

- **Missing/empty `rewritten_query`** → `RetrievalError`.
- **Embedding failure** → `RetrievalError` (wraps the cause).
- **Search failure** → `RetrievalError` (wraps the cause).
- **Zero hits** → returns `[]` (not an error). The Relevance Gate handles the "no useful
  results" case downstream.

## 7. Design decisions

| Decision | Rationale |
|---|---|
| Role filter in the Qdrant query | Hard security boundary; never post-filter in Python (CLAUDE.md §5, §12). |
| `[user_role, "all"]` | Users see their own scope plus org-wide documents. |
| Raise on failure (fail-closed) | Retrieval is load-bearing; a silent empty result would mask an outage as "no answer". |
| Empty results are not an error | "No relevant documents" is a legitimate answer the gate handles. |

## 8. Constraints honoured

- BGE-M3 embeddings via injected `BaseEmbedder` (no substitution) (CLAUDE.md §12).
- Role filter applied at query time, never in Python (CLAUDE.md §5, §12).
- No hardcoded role names — the filter is built from `state["user_role"]`.
- Immutable state update.

## 9. Test coverage (`tests/unit/test_node_03_search.py`)

| Test | Proves |
|---|---|
| `test_embeds_and_searches_with_role_filter` | Embeds once, searches once, filter = `[role, "all"]` |
| `test_empty_results_returns_empty_list` | Zero hits → `[]`, no error |
| `test_missing_rewritten_query_raises_error` | Missing query → `RetrievalError` |
| `test_preserves_existing_state_keys` | Prior state intact + `retrieved_chunks` |
| `test_role_filter_includes_all` | Filter always includes `"all"` |
