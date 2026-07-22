# Node 05b — Retry

**File:** `app/pipeline/nodes/node_05b_retry.py` · **Class:** `RetryNode` · **LLM:** `REWRITER_MODEL`

---

## 1. Role

The pipeline's single second chance. When the Relevance Gate fails on the first attempt,
this node **broadens** the failed query — removing over-specific terms, adding synonyms
and related concepts — and sends it back into search. It fires at most once; a second
gate failure ends in an honest fallback rather than an infinite loop.

## 2. Interface

```python
RetryNode(llm: BaseLLMService, settings: Settings)
name -> "retry"
```

## 3. State contract

| Reads | Writes |
|---|---|
| `query`, `rewritten_query`, `retry_attempted` | `rewritten_query: str` (broadened), `retry_attempted: True` |

## 4. Behaviour

1. **Idempotent guard:** if `retry_attempted` is already set → return immediately with
   `retry_attempted = True` and no new query. (In practice the graph prevents a second
   entry; this makes the node safe regardless.)
2. Otherwise call the LLM (`REWRITER_MODEL`, `temperature=0.3`) with the original query
   and the failed rewritten query, asking for a broader reformulation.
3. On success → `rewritten_query = broadened`, `retry_attempted = True`.
4. On failure or empty output → `rewritten_query = original query`, `retry_attempted = True`.

## 5. Routing implications

The graph edge `retry → qdrant_search` loops the broadened query back into retrieval.
That second pass runs search → rerank → gate again. Because `retry_attempted` is now set,
a second gate failure routes to `END` (fallback), not back here.

## 6. Error handling & edge cases

Fail-open, like the Rewriter — a retry is a best-effort widening, never a hard dependency:

- **Already retried** → no-op broadening, guard stays set.
- **LLM failure / empty output** → fall back to the original (unrewritten) query, which is
  itself broader than the failed rewrite.

## 7. Design decisions

| Decision | Rationale |
|---|---|
| Real LLM broadening (not a reset) | The point of a retry is a *different, wider* query; simply reusing the original rarely changes the result. |
| `REWRITER_MODEL` | Query reformulation is exactly that model's job; no separate model var exists for retry (CLAUDE.md §7). |
| `temperature=0.3` | A little variation helps produce a genuinely different phrasing, unlike the deterministic rewrite. |
| Idempotent `retry_attempted` guard | Hard stop against retry loops — one shot only (CLAUDE.md §6). |
| Fall back to the original query | Even if broadening fails, the original is broader than the failed rewrite. |

> **History.** The first implementation only reset `rewritten_query` to the original query
> — it did not broaden anything, contradicting the spec. Rewritten in the 2026-07-22
> review to broaden via the LLM. The dead `retry_pass` field it used to write was removed
> (routing keys on `relevance_pass` + `retry_attempted`, never `retry_pass`).

## 8. Constraints honoured

- LLM via injected `BaseLLMService`; no vendor imports.
- One-shot retry only (CLAUDE.md §6).
- No hardcoded role names; immutable state update.

## 9. Test coverage (`tests/unit/test_node_05b_retry.py`)

| Test | Proves |
|---|---|
| `test_first_retry_broadens_query_via_llm` | Broadens via `REWRITER_MODEL`, sets `retry_attempted` |
| `test_first_retry_falls_back_to_original_on_llm_failure` | LLM exception → original query |
| `test_second_retry_is_blocked` | Guard set → no LLM call |
| `test_preserves_existing_state_keys` | Prior state intact |
| `test_empty_llm_response_falls_back_to_original` | Blank output → original query |
