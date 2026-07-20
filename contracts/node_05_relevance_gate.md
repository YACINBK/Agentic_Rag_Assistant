# Contract — Node 05: Relevance Gate
Status: APPROVED
Last verified: —

## Interface

```python
class RelevanceGateNode(BaseNode):
    """Pure Python threshold check on reranker scores. No LLM, no external calls."""

    def __init__(self, settings: Settings) -> None: ...

    @property
    def name(self) -> str:
        return "relevance_gate"

    async def execute(self, state: PipelineState) -> PipelineState: ...
```

Checks whether the reranked chunks are sufficiently relevant to answer the query.
Uses `settings.RELEVANCE_THRESHOLD` (default 0.5) as the minimum score for the
top-ranked chunk. This is the gateway to the Generator — if chunks aren't relevant
enough, the pipeline routes to retry or fallback.

## Inputs

| Parameter | Type | Valid range | Error behavior |
|---|---|---|---|
| `state` | `PipelineState` | Must contain `reranked_chunks` (list, possibly empty) | Empty list → gate fails (relevance_pass = False) |
| `self._settings` | `Settings` | Must have `RELEVANCE_THRESHOLD` (float, 0.0–1.0) | — |

## Outputs

Returns a new `PipelineState` dict (merged with input state) containing:

| Key | Type | Value |
|---|---|---|
| `relevance_pass` | `bool` | `True` if top chunk score >= threshold, `False` otherwise |

No other keys are added or modified.

## Environment

None. This node has no external dependencies. Pure computation on state data.

## Assertions

1. Returned state contains `relevance_pass` as a boolean.
2. When top chunk score >= `RELEVANCE_THRESHOLD`, `relevance_pass` is `True`.
3. When top chunk score < `RELEVANCE_THRESHOLD`, `relevance_pass` is `False`.
4. When `reranked_chunks` is an empty list, `relevance_pass` is `False`.
5. The threshold comparison uses the FIRST chunk's `score` field (list is already sorted descending from Node 04).
6. No external service is called (no LLM, no HTTP, no DB).
7. All existing state keys are preserved unchanged.
8. The node does not modify `reranked_chunks` or any chunk's score.

## Forbidden

- Do NOT call any LLM or external service — this is pure Python logic only.
- Do NOT sort `reranked_chunks` — they are already sorted by Node 04.
- Do NOT remove or filter chunks — the gate only decides pass/fail, it doesn't alter the chunk list.
- Do NOT use an average or aggregate of scores — only the top (first) chunk's score matters.
- Do NOT modify any state keys other than `relevance_pass`.

## Test cases

### 1. test_passes_when_top_score_above_threshold

**Setup:** `settings.RELEVANCE_THRESHOLD = 0.5`, `make_post_rerank_state()` (default top score 0.95)
**Call:** `await node.execute(state)`
**Expected:** `result["relevance_pass"] is True`

### 2. test_fails_when_top_score_below_threshold

**Setup:** `settings.RELEVANCE_THRESHOLD = 0.5`, `make_post_rerank_state()` but override chunks with score 0.3
**Call:** `await node.execute(state)`
**Expected:** `result["relevance_pass"] is False`

### 3. test_fails_when_chunks_empty

**Setup:** `settings.RELEVANCE_THRESHOLD = 0.5`, state with `reranked_chunks=[]`
**Call:** `await node.execute(state)`
**Expected:** `result["relevance_pass"] is False`

### 4. test_exact_threshold_passes

**Setup:** `settings.RELEVANCE_THRESHOLD = 0.5`, top chunk score exactly 0.5
**Call:** `await node.execute(state)`
**Expected:** `result["relevance_pass"] is True` (>= not >)

### 5. test_preserves_state_keys

**Setup:** `make_post_rerank_state()`
**Call:** `await node.execute(state)`
**Expected:** All original keys preserved. `result["reranked_chunks"]` unchanged (same objects, same scores).

### 6. test_does_not_modify_chunks

**Setup:** `make_post_rerank_state(num_chunks=3)`
**Call:** `await node.execute(state)`
**Expected:** `len(result["reranked_chunks"]) == 3`, scores unchanged, no filtering.

### 7. test_uses_first_chunk_score_not_average

**Setup:** `settings.RELEVANCE_THRESHOLD = 0.5`, chunks with scores `[0.6, 0.1, 0.1]` (average 0.27 < 0.5, but top 0.6 >= 0.5)
**Call:** `await node.execute(state)`
**Expected:** `result["relevance_pass"] is True` (uses first, not average)
