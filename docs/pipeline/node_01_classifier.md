# Node 01 — Classifier

**File:** `app/pipeline/nodes/node_01_classifier.py` · **Class:** `ClassifierNode` · **LLM:** `CLASSIFIER_MODEL`

---

## 1. Role

The pipeline's **traffic controller**. Every query that misses the cache hits this node
first. It makes a binary routing decision — it does **not** answer, it only decides which
path the query takes.

| Verdict | Meaning | Next |
|---|---|---|
| `DIRECT` | Not answerable from our documents (chitchat, weather, out-of-scope) | Short-circuit — canned response, no retrieval, no generation |
| `SIMPLE_RAG` | Might be answerable from our documents | Continue to Node 02 (Rewriter) |

MVP is binary. The 3-way split (adding `COMPLEX_RAG` with decomposition + parallel
search) is post-MVP (CLAUDE.md §14).

## 2. Interface

```python
ClassifierNode(llm: BaseLLMService, settings: Settings)
name -> "classifier"
```

## 3. State contract

| Reads | Writes |
|---|---|
| `query` | `classification: str`, `direct_response: str` (DIRECT only) |

## 4. Behaviour

```
execute(state)
  ├── query missing/empty?              → raise ClassificationError
  ├── CLASSIFIER_MODEL == "manual"?      → return SIMPLE_RAG (no LLM call)
  ├── LLM call (system prompt + query, temperature=0.0)
  │     └── raises?                      → return SIMPLE_RAG (fail-open)
  └── _parse(response):
        ├── "DIRECT"                      → set direct_response, return DIRECT
        └── anything else / "SIMPLE_RAG"  → return SIMPLE_RAG
```

**Manual mode** (`CLASSIFIER_MODEL=manual`) is a development escape hatch: it skips the
LLM and routes everything to SIMPLE_RAG, so nodes 2–7 can be exercised while Ollama is
offline.

**Prompt strategy.** A single system+user pair at `temperature=0.0`. The system prompt
defines both categories with examples and demands exactly one word. This constrained-
output pattern plus deterministic temperature yields consistent, parseable responses.

**`_parse()` fallback ladder** (LLMs are imperfect even at t=0):

```python
cleaned = raw.strip().upper()
if cleaned == "DIRECT": return "DIRECT"          # exact
if cleaned == "SIMPLE_RAG": return "SIMPLE_RAG"   # exact
if "DIRECT" in cleaned and "SIMPLE" not in cleaned: return "DIRECT"   # substring
return "SIMPLE_RAG"                                # default: fail-open
```

## 5. Routing implications

`route_classification` in `graph.py`: `DIRECT` → `END` (with `direct_response`);
`SIMPLE_RAG` → `rewriter`.

## 6. Error handling & edge cases

- **Missing/empty query** → raises `ClassificationError`. This is the one strict check:
  an empty query is a caller bug, not a routing case, and must surface loudly.
  > Added in the 2026-07-22 review — previously an empty query silently reached the LLM
  > and a missing key raised a bare `KeyError`.
- **LLM failure / unparseable output** → fail-open to `SIMPLE_RAG`. The worst outcome is
  an unnecessary document search; silently refusing a real question is far worse.

## 7. Design decisions

| Decision | Rationale |
|---|---|
| Binary only | MVP scope; COMPLEX_RAG is post-MVP. |
| Fail-open routing | An unnecessary retrieval is cheap; a false refusal is expensive. |
| Raise only on empty query | Distinguishes a caller contract violation from an LLM hiccup. |
| System prompt as module constant | The reader sees exactly what the LLM sees. |
| `_parse` as `@staticmethod` | No `self` dependency; independently testable. |

## 8. Constraints honoured

- LLM via injected `BaseLLMService`; no vendor imports (CLAUDE.md §4, §12).
- No hardcoded role names.
- Manual mode as specified (CLAUDE.md §4, §7).
- Immutable state update.

## 9. Test coverage (`tests/unit/test_node_01_classifier.py`)

| Test | Proves |
|---|---|
| `test_chitchat_classified_as_direct` | Greeting → DIRECT + `direct_response` set |
| `test_domain_question_classified_as_simple_rag` | Real question → SIMPLE_RAG, no `direct_response` |
| `test_manual_mode_skips_llm` | Manual mode → SIMPLE_RAG, 0 LLM calls |
| `test_invalid_llm_response_defaults_to_simple_rag` | Garbage output → fail-open |
| `test_out_of_scope_classified_as_direct` | Out-of-scope → DIRECT |
| `test_missing_query_raises_classification_error` | Missing key → `ClassificationError` |
| `test_empty_query_raises_classification_error` | Blank query → `ClassificationError` |
| `test_llm_called_with_correct_model` | Uses `CLASSIFIER_MODEL`, one call |
| `test_preserves_existing_state_keys` | Prior state intact + `classification` |
