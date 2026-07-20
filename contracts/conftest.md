# Contract — Shared Test Infrastructure (conftest.py)
Status: APPROVED
Last verified: —

## Interface

`tests/conftest.py` — shared fixtures and factories imported by ALL node tests.
No node test creates its own state dicts or mock patterns. Everything comes from here.

## State Factories

### `make_state(**overrides) → PipelineState`
Returns a valid base pipeline state with all required input fields populated.
Any key can be overridden via kwargs.

Default values:
```python
{
    "query": "What is the company leave policy?",
    "user_id": "<uuid>",
    "user_role": "developer",
    "user_email": "test@whitecape.fr",
}
```

### `make_chunk(**overrides) → ChunkPayload`
Returns a valid chunk payload with all fields populated.

Default values:
```python
{
    "chunk_id": "<uuid>",
    "text": "Sample chunk text for testing.",
    "document_id": "<uuid>",
    "original_filename": "test_doc.pdf",
    "category": "technical",
    "page_number": 1,
    "chunk_index": 0,
    "score": 0.85,
}
```

### Stage-specific factories

Each builds on `make_state()` with additional keys set to represent pipeline state at that stage:

#### `make_post_classifier_state(classification="SIMPLE_RAG", **overrides) → PipelineState`
State after Node 01 completes. Sets `classification`.
If classification is "DIRECT", also sets `direct_response`.

#### `make_post_retrieval_state(num_chunks=5, **overrides) → PipelineState`
State after Node 03 completes. Includes `classification`, `rewritten_query`, and `retrieved_chunks` (list of `num_chunks` ChunkPayloads).

#### `make_post_rerank_state(num_chunks=3, **overrides) → PipelineState`
State after Node 04 completes. Includes everything from post-retrieval plus `reranked_chunks` (sorted by score descending).

#### `make_post_generation_state(**overrides) → PipelineState`
State after Node 06 completes. Includes everything from post-rerank plus `generated_answer` and `relevance_pass=True`.

## Mock Services

### `MockLLMService(BaseLLMService)`
- Constructor: `__init__(self, response: str = "Mock LLM response.")`
- Tracks calls in `self.calls: list[dict]` — each entry has `model`, `messages`, `temperature`
- Returns `self._response` on every call to `complete()`
- Mock contract: return type is always `str`, never None, never raises

### `MockVectorStore(BaseVectorStore)`
- Constructor: `__init__(self, results: list[ChunkPayload] | None = None)`
- Defaults to `[make_chunk()]` if no results provided
- Tracks `search_calls`, `upsert_calls`, `delete_calls`
- Mock contract: `search()` returns `list[ChunkPayload]`, may be empty list, never None

### `MockReranker(BaseReranker)`
- Constructor: `__init__(self, scores: list[float] | None = None)`
- Defaults to `[0.9, 0.7, 0.3]`
- Tracks `self.calls: list[dict]`
- Mock contract: `rerank()` returns `list[float]`, length matches input passages

### `MockEmbedder(BaseEmbedder)`
- Constructor: `__init__(self, vector_dim: int = 1024)`
- Returns deterministic vectors `[0.1] * dim` for every input
- Tracks `self.calls: list[list[str]]`
- Mock contract: `embed()` returns `list[list[float]]`, length matches input texts. `embed_single()` returns `list[float]` of length `vector_dim`.

## Pytest Fixtures

All mock services and factories are exposed as fixtures:

| Fixture | Returns |
|---|---|
| `mock_llm` | `MockLLMService()` |
| `mock_vector_store` | `MockVectorStore()` |
| `mock_reranker` | `MockReranker()` |
| `mock_embedder` | `MockEmbedder()` |
| `sample_state` | `make_state()` |
| `sample_chunks` | 5 chunks with decreasing scores |

## Assertions

1. `make_state()` returns a dict with keys `query`, `user_id`, `user_role`, `user_email`
2. `make_chunk()` returns a dict with keys `chunk_id`, `text`, `document_id`, `original_filename`, `category`, `page_number`, `chunk_index`, `score`
3. `make_post_classifier_state()` includes all base state keys plus `classification`
4. `make_post_classifier_state(classification="DIRECT")` also includes `direct_response`
5. `make_post_retrieval_state(num_chunks=3)` includes `retrieved_chunks` with exactly 3 items
6. `make_post_rerank_state()` includes `reranked_chunks` sorted by score descending
7. `make_post_generation_state()` includes `generated_answer` as a non-empty string
8. All mock services track their calls (inspectable in tests)
9. Override kwargs in any factory replace the default value for that key

## Forbidden

- No mock may raise an exception by default. Nodes under test control error paths by configuring mock return values, not by mocks that throw.
- No factory may return None for any field. All fields are always populated with valid test data.
- No mock may import or depend on concrete service implementations.
- No test file may construct PipelineState dicts directly — always use factories.
