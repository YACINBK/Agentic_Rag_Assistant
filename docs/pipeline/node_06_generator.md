# Node 06 — Generator

**File:** `app/pipeline/nodes/node_06_generator.py` · **Class:** `GeneratorNode` · **LLM:** `GENERATOR_MODEL`

---

## 1. Role

The only node that produces user-facing text. It generates a cited, role-framed answer
grounded strictly in the reranked chunks. Its output is **fully buffered** — held in
memory, not streamed — because Node 07 (Faithfulness) must verify the *complete* answer
before a single token reaches the user.

## 2. Interface

```python
GeneratorNode(llm: BaseLLMService, settings: Settings)
name -> "generator"
```

## 3. State contract

| Reads | Writes |
|---|---|
| `query`, `user_role`, `reranked_chunks` | `generated_answer: str` |

## 4. Behaviour

1. If `reranked_chunks` is empty → return the honest fallback answer, **no LLM call**.
2. Build the system prompt: a role persona (`"You answer questions from a {user_role}..."`)
   plus rules — cite sources inline, use only the provided chunks, admit when the chunks
   are insufficient, never fabricate.
3. Build the user message: the query followed by the chunks as numbered excerpts, each
   tagged with its source filename.
4. Call the LLM at `temperature=0.0`.
5. On empty output → fallback answer. On success → `generated_answer` (buffered).

**Citation format:** `[source: filename]` inline after each fact.

## 5. Routing implications

`generator → faithfulness` is unconditional. The generator never streams; the SSE
orchestrator streams only after Node 07 returns a faithful verdict (CLAUDE.md §6, §12).

## 6. Error handling & edge cases

Safety-critical node — a wrong confident answer is worse than none:

- **Empty chunks** → fallback answer, no LLM call (never prompt the model with nothing).
- **LLM failure** → raise `GenerationError` (fail-closed; no answer beats a broken one).
- **Empty LLM output** → fallback answer.

## 7. Design decisions

| Decision | Rationale |
|---|---|
| Buffer, never stream | Faithfulness runs on the whole answer; you cannot verify half a sentence (CLAUDE.md §12). |
| Fallback on empty chunks | Honest "I couldn't find this" beats a hallucinated answer. |
| Raise on LLM failure | Generator failure is fatal to the request by design. |
| Role persona in the prompt | UX framing only — **not** a security control; access is enforced at Node 03 (CLAUDE.md §5). |
| `temperature=0.0` | Factual answers should be deterministic. |

> **Note (per §5).** The current persona is a hardcoded template keyed on `user_role`.
> The full design sources the persona from the `Role.persona_prompt` column in PostgreSQL;
> wiring that through is a later step. No role *names* are hardcoded — only the template
> shape — so the CLAUDE.md §12 rule holds.

## 8. Constraints honoured

- LLM via injected `BaseLLMService`; no vendor imports (CLAUDE.md §4, §12).
- Buffered output; streaming only after faithfulness (CLAUDE.md §12).
- No hardcoded role names; role read from state.
- Immutable state update.

## 9. Test coverage (`tests/unit/test_node_06_generator.py`)

| Test | Proves |
|---|---|
| `test_generates_cited_answer_from_chunks` | Chunks → answer; correct model used |
| `test_empty_chunks_produces_fallback_answer` | No chunks → fallback, 0 LLM calls |
| `test_llm_failure_raises_generation_error` | Exception → `GenerationError` |
| `test_prompt_includes_role_and_chunks` | Role in system prompt, query + chunks in user message |
| `test_preserves_existing_state_keys` | Prior state intact + `generated_answer` |
