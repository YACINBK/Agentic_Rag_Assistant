# Node 04 — Reranker

**File:** `app/pipeline/nodes/node_04_reranker.py` · **Class:** `RerankerNode` · **LLM:** none (cross-encoder)

---

## 1. Role

Re-scores the retrieved candidates with a **cross-encoder** (bge-reranker-v2-m3), which
reads the query and each passage *together* and judges true relevance — far more accurate
than the vector-similarity score from search. It then sorts the chunks by that score,
descending, so the best evidence is first.

## 2. Interface

```python
RerankerNode(reranker: BaseReranker, settings: Settings)
name -> "reranker"
```

## 3. State contract

| Reads | Writes |
|---|---|
| `rewritten_query`, `retrieved_chunks` | `reranked_chunks: list[ChunkPayload]` (sorted desc by `score`) |

## 4. Behaviour

1. Validate `rewritten_query` is present and non-empty → else raise `RetrievalError`.
2. If `retrieved_chunks` is empty → return `reranked_chunks = []` (skip the reranker call).
3. Send `(query, passages)` to the reranker; get one score per passage.
4. Validate `len(scores) == len(chunks)` → else raise `RetrievalError`.
5. Attach each score to its chunk, sort descending, return `reranked_chunks`.

## 5. Routing implications

None directly — `reranker → relevance_gate` is unconditional. But the **top** score it
produces is exactly what the Relevance Gate thresholds against.

## 6. Error handling & edge cases

Safety barrier — fails **closed** with typed errors:

- **Missing/empty `rewritten_query`** → `RetrievalError`.
- **Reranker call failure** → `RetrievalError` (wraps the cause).
- **Score/chunk count mismatch** → `RetrievalError`. This guard is important: a naive
  `zip()` would silently drop chunks if the service returned fewer scores, corrupting the
  ranking invisibly.
- **Empty input chunks** → `[]`, no reranker call (nothing to score).

## 7. Design decisions

| Decision | Rationale |
|---|---|
| Cross-encoder via TEI, not Ollama | Ollama cannot run cross-encoder architectures; TEI is the correct runtime (CLAUDE.md §4). |
| Explicit length validation | Prevents silent chunk loss from a malformed reranker response. |
| Skip the call on empty input | No passages → no work; avoids a pointless HTTP round-trip. |
| Sort in the node | Downstream (gate, generator) can assume `reranked_chunks[0]` is the best. |

## 8. Constraints honoured

- Reranker is bge-reranker-v2-m3 via TEI, accessed through injected `BaseReranker`
  (CLAUDE.md §4, §12).
- Immutable state update.

## 9. Test coverage (`tests/unit/test_node_04_reranker.py`)

| Test | Proves |
|---|---|
| `test_reranks_and_sorts_by_score` | Output sorted descending by reranker score |
| `test_empty_chunks_skips_reranker` | Empty input → `[]`, 0 reranker calls |
| `test_missing_rewritten_query_raises_error` | Missing query → `RetrievalError` |
| `test_preserves_existing_state_keys` | Prior state intact + `reranked_chunks` |
| `test_reranker_called_with_correct_inputs` | Passes the query and all passages |
