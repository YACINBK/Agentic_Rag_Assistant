# Contract — Node 01: Classifier
Status: APPROVED
Last verified: —

## Interface

```python
class ClassifierNode(BaseNode):
    """Binary classifier: routes queries to DIRECT or SIMPLE_RAG path."""

    def __init__(self, llm: BaseLLMService, settings: Settings) -> None: ...

    @property
    def name(self) -> str:
        return "classifier"

    async def execute(self, state: PipelineState) -> PipelineState: ...
```

Classifies the incoming user query as either DIRECT (no retrieval needed) or
SIMPLE_RAG (needs document retrieval). MVP: binary only. COMPLEX_RAG is post-MVP.

## Inputs

| Parameter | Type | Valid range | Error behavior |
|---|---|---|---|
| `state` | `PipelineState` | Must contain `query` (non-empty string) | Raise `ClassificationError` if query missing or empty |
| `self._llm` | `BaseLLMService` | Injected at construction | — |
| `self._settings` | `Settings` | Must have `CLASSIFIER_MODEL` | — |

## Outputs

Returns a new `PipelineState` dict (merged with input state) containing:

| Key | Type | Value |
|---|---|---|
| `classification` | `str` | Exactly `"DIRECT"` or `"SIMPLE_RAG"` — no other values |
| `direct_response` | `str` | Non-empty canned response (ONLY set when classification is DIRECT) |

When classification is `SIMPLE_RAG`, `direct_response` is NOT present in the returned state.

## Environment

| Dependency | Protocol | Mock contract |
|---|---|---|
| `BaseLLMService` (via `self._llm`) | `async complete(model, messages, temperature) → str` | Mock returns configurable string. Must preserve: single string return, no side effects. |

No external services called directly. LLM abstraction handles Ollama/LiteLLM routing.

**Manual mode:** When `settings.CLASSIFIER_MODEL == "manual"`, the node skips the LLM call
entirely and returns `SIMPLE_RAG` as the default classification. This allows testing
downstream nodes without needing a running Ollama instance.

## Assertions

1. Returned state always contains `classification` set to exactly `"DIRECT"` or `"SIMPLE_RAG"`.
2. A chitchat query (e.g. "hey how are you") is classified as `DIRECT`.
3. A domain-relevant query (e.g. "What is our deployment process?") is classified as `SIMPLE_RAG`.
4. When classification is `DIRECT`, returned state also contains `direct_response` as a non-empty string.
5. When classification is `SIMPLE_RAG`, returned state does NOT contain `direct_response`.
6. When `settings.CLASSIFIER_MODEL == "manual"`, `self._llm.complete()` is never called.
7. When NOT in manual mode, `self._llm.complete()` is called exactly once with `model=settings.CLASSIFIER_MODEL`.
8. If LLM returns an unparseable or invalid response (not "DIRECT" or "SIMPLE_RAG"), the node defaults to `SIMPLE_RAG` (fail-open to retrieval).

## Forbidden

- Do NOT hardcode role names or check user role in classification logic (CLAUDE.md §12).
- Do NOT import `anthropic`, `openai`, or call LLM directly — use `BaseLLMService` only (CLAUDE.md §12).
- Do NOT return `COMPLEX_RAG` — MVP is binary only.
- Do NOT stream any output — this node returns state, it does not produce user-facing content.
- Do NOT modify any state keys other than `classification` and `direct_response`.

## Test cases

### 1. test_chitchat_classified_as_direct

**Setup:** `MockLLMService(response="DIRECT")`, `make_state(query="hello!")`
**Call:** `await node.execute(state)`
**Expected:** `result["classification"] == "DIRECT"`, `result["direct_response"]` is non-empty string

### 2. test_domain_question_classified_as_simple_rag

**Setup:** `MockLLMService(response="SIMPLE_RAG")`, `make_state(query="What is our deployment process?")`
**Call:** `await node.execute(state)`
**Expected:** `result["classification"] == "SIMPLE_RAG"`, `"direct_response" not in result`

### 3. test_manual_mode_skips_llm

**Setup:** `MockLLMService()`, `settings.CLASSIFIER_MODEL = "manual"`, `make_state()`
**Call:** `await node.execute(state)`
**Expected:** `result["classification"] == "SIMPLE_RAG"`, `mock_llm.calls == []`

### 4. test_invalid_llm_response_defaults_to_simple_rag

**Setup:** `MockLLMService(response="I don't understand the question")`, `make_state()`
**Call:** `await node.execute(state)`
**Expected:** `result["classification"] == "SIMPLE_RAG"`

### 5. test_out_of_scope_classified_as_direct

**Setup:** `MockLLMService(response="DIRECT")`, `make_state(query="What's the weather in Paris?")`
**Call:** `await node.execute(state)`
**Expected:** `result["classification"] == "DIRECT"`, `result["direct_response"]` is non-empty string

### 6. test_missing_query_raises_classification_error

**Setup:** `MockLLMService()`, `make_state()` then remove `query` key
**Call:** `await node.execute(state)`
**Expected:** Raises `ClassificationError`

### 7. test_llm_called_with_correct_model

**Setup:** `MockLLMService(response="SIMPLE_RAG")`, `settings.CLASSIFIER_MODEL = "ollama/qwen2.5:7b"`, `make_state()`
**Call:** `await node.execute(state)`
**Expected:** `mock_llm.calls[0]["model"] == "ollama/qwen2.5:7b"`

### 8. test_preserves_existing_state_keys

**Setup:** `MockLLMService(response="SIMPLE_RAG")`, `make_state(query="test", user_role="developer")`
**Call:** `await node.execute(state)`
**Expected:** `result["query"] == "test"`, `result["user_role"] == "developer"`, `result["classification"] == "SIMPLE_RAG"`
