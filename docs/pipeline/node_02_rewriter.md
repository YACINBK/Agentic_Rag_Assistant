# Node 02 — Rewriter

**File:** `app/pipeline/nodes/node_02_rewriter_decomposer.py` · **Class:** `RewriterNode` · **LLM:** `REWRITER_MODEL`

---

## 1. Role

Reformulates the raw user question into a query optimised for retrieval — expanding
abbreviations, adding domain vocabulary likely to appear in company documents, and
preserving the original intent. Runs only on the SIMPLE_RAG path.

The filename carries `_decomposer` because in the full system this node also decomposes a
complex query into sub-queries. **That is post-MVP** — for the MVP it rewrites only and
always emits `sub_queries = []`.

## 2. Interface

```python
RewriterNode(llm: BaseLLMService, settings: Settings)
name -> "rewriter"
```

## 3. State contract

| Reads | Writes |
|---|---|
| `classification`, `query` | `rewritten_query: str`, `sub_queries: list[str]` |

## 4. Behaviour

1. If `classification == "DIRECT"` → return the state unchanged (DIRECT never reaches
   retrieval, so there is nothing to rewrite).
2. Otherwise call the LLM (system prompt + original query, `temperature=0.0`).
3. On success with non-empty output → `rewritten_query = cleaned`, `sub_queries = []`.
4. On LLM exception **or** empty output → `rewritten_query = original query`,
   `sub_queries = []`.

## 5. Routing implications

None directly — the graph edge `rewriter → qdrant_search` is unconditional. But the
`rewritten_query` this node produces is what Node 03 embeds, so its quality drives
retrieval quality.

## 6. Error handling & edge cases

Fail-open: any failure falls back to the **original query**. A mediocre search on the raw
question beats no search at all. Because the fallback still populates `rewritten_query`,
downstream nodes always have the key they require.

- **DIRECT input** → passthrough (defensive; the graph shouldn't route DIRECT here, but
  the node is safe if it does).
- **LLM failure** → original query.
- **Empty/whitespace output** → original query.

## 7. Design decisions

| Decision | Rationale |
|---|---|
| Skip on DIRECT | No retrieval on the DIRECT path; nothing to optimise. |
| Fail-open to original | Retrieval robustness — never block the pipeline on a rewrite. |
| `sub_queries` always set to `[]` | Downstream contract stability; the key exists even though decomposition is post-MVP. |
| `temperature=0.0` | The rewrite must preserve intent, not invent a new question. |

## 8. Constraints honoured

- LLM via injected `BaseLLMService`; no vendor imports.
- MVP = rewrite only, no decomposition (CLAUDE.md §6, §14).
- Immutable state update.

## 9. Test coverage (`tests/unit/test_node_02_rewriter.py`)

| Test | Proves |
|---|---|
| `test_rewrites_query_for_retrieval` | Produces a non-empty, changed query; one LLM call |
| `test_direct_classification_skips_rewrite` | DIRECT → passthrough, 0 LLM calls |
| `test_llm_failure_falls_back_to_original` | Exception → original query |
| `test_empty_llm_response_falls_back` | Blank output → original query |
| `test_preserves_existing_state_keys` | Prior state intact + rewrite fields |
