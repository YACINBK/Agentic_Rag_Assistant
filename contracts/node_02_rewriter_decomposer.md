# Contract — Node 02: Rewriter + Decomposer
Status: APPROVED
Last verified: —

## Interface

```python
class RewriterNode(BaseNode):
    """Rewrites query for retrieval quality. MVP: rewrite only, no decomposition."""

    def __init__(self, llm: BaseLLMService, settings: Settings) -> None: ...

    @property
    def name(self) -> str:
        return "rewriter"

    async def execute(self, state: PipelineState) -> PipelineState: ...
```

Takes the user's raw query and rewrites it into a form optimized for vector retrieval:
expand abbreviations, add implicit context, make it searchable. MVP does NOT decompose
into sub-queries (that's COMPLEX_RAG, post-MVP).

## Inputs

| Parameter | Type | Valid range | Error behavior |
|---|---|---|---|
| `state` | `PipelineState` | Must contain `query` (non-empty string) and `classification == "SIMPLE_RAG"` | If classification is not SIMPLE_RAG, pass through unchanged |
| `self._llm` | `BaseLLMService` | Injected at construction | — |
| `self._settings` | `Settings` | Must have `REWRITER_MODEL` | — |

## Outputs

Returns a new `PipelineState` dict (merged with input state) containing:

| Key | Type | Value |
|---|---|---|
| `rewritten_query` | `str` | The rewritten query optimized for retrieval. Non-empty. |
| `sub_queries` | `list[str]` | MVP: always `[rewritten_query]` (single-element list) |

If classification is NOT `SIMPLE_RAG` (e.g. DIRECT path somehow reaches this node),
the node returns state unchanged — no rewrite, no LLM call.

## Environment

| Dependency | Protocol | Mock contract |
|---|---|---|
| `BaseLLMService` (via `self._llm`) | `async complete(model, messages, temperature) → str` | Mock returns configurable string. The string IS the rewritten query (no JSON parsing needed). |

No external services called directly. LLM abstraction handles routing.

## Assertions

1. Returned state contains `rewritten_query` as a non-empty string when classification is SIMPLE_RAG.
2. Returned state contains `sub_queries` as a single-element list equal to `[rewritten_query]`.
3. `self._llm.complete()` is called exactly once with `model=settings.REWRITER_MODEL`.
4. If state classification is not SIMPLE_RAG, node returns state unchanged (no new keys added).
5. If state classification is not SIMPLE_RAG, LLM is never called.
6. If LLM returns an empty string, node falls back to using the original `query` as `rewritten_query`.
7. The original `query` key is preserved in the returned state (never overwritten).
8. All other existing state keys are preserved unchanged.

## Forbidden

- Do NOT decompose into multiple sub-queries — MVP is single rewrite only.
- Do NOT call LLM directly — use `BaseLLMService` only.
- Do NOT modify `query`, `classification`, or any key other than `rewritten_query` and `sub_queries`.
- Do NOT add retry logic — if LLM fails, fallback to original query is sufficient.
- Do NOT parse JSON from the LLM response — the response IS the rewritten query (strip whitespace only).

## Test cases

### 1. test_rewrites_query_for_retrieval

**Setup:** `MockLLMService(response="company annual leave policy entitlements")`, `make_post_classifier_state(classification="SIMPLE_RAG", query="What's the leave policy?")`
**Call:** `await node.execute(state)`
**Expected:** `result["rewritten_query"] == "company annual leave policy entitlements"`, `result["sub_queries"] == ["company annual leave policy entitlements"]`

### 2. test_passthrough_when_not_simple_rag

**Setup:** `MockLLMService()`, `make_post_classifier_state(classification="DIRECT")`
**Call:** `await node.execute(state)`
**Expected:** `"rewritten_query" not in result`, `"sub_queries" not in result`, `mock_llm.calls == []`

### 3. test_empty_llm_response_falls_back_to_original

**Setup:** `MockLLMService(response="")`, `make_post_classifier_state(classification="SIMPLE_RAG", query="leave policy?")`
**Call:** `await node.execute(state)`
**Expected:** `result["rewritten_query"] == "leave policy?"`, `result["sub_queries"] == ["leave policy?"]`

### 4. test_llm_called_with_correct_model

**Setup:** `MockLLMService(response="rewritten")`, `settings.REWRITER_MODEL = "ollama/qwen2.5:14b"`, `make_post_classifier_state(classification="SIMPLE_RAG")`
**Call:** `await node.execute(state)`
**Expected:** `mock_llm.calls[0]["model"] == "ollama/qwen2.5:14b"`

### 5. test_preserves_existing_state_keys

**Setup:** `MockLLMService(response="rewritten query")`, `make_post_classifier_state(classification="SIMPLE_RAG")`
**Call:** `await node.execute(state)`
**Expected:** All original keys preserved, `result["query"]` unchanged, `result["classification"] == "SIMPLE_RAG"`

### 6. test_strips_whitespace_from_llm_response

**Setup:** `MockLLMService(response="  rewritten query with spaces  \n")`, `make_post_classifier_state(classification="SIMPLE_RAG")`
**Call:** `await node.execute(state)`
**Expected:** `result["rewritten_query"] == "rewritten query with spaces"`
