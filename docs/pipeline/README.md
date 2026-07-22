# Pipeline Documentation

Reference documentation for the Whitecape Knowledge Assistant query pipeline.
One document per node, plus this overview. These docs describe **what was built** and
**why** — they are the onboarding checkpoint, the basis for the project report, and the
reference a new collaborator (or the supervisor) reads to understand the system without
reading every line of code.

> **Relationship to other docs.** `CLAUDE.md` is the binding specification (the "why it
> must be so"). These node docs are the implementation reference (the "how it is, and
> why we built it that way"). `CHANGELOG.md` is the chronological log; `DEVLOG.md` is the
> architecture handoff. The gitignored `contracts/` and `reviews/` folders are
> loop-engineering process artifacts — not deliverables.

---

## The pipeline in one picture

```
User query
   │
   ▼
[00 Cache Check] ──hit──────────────────────────────► return cached answer (SSE)
   │ miss
   ▼
[01 Classifier] ──DIRECT───────────────────────────► canned response, stop
   │ SIMPLE_RAG
   ▼
[02 Rewriter] ─► [03 Qdrant Search] ─► [04 Reranker] ─► [05 Relevance Gate]
   │                    ▲                                      │
   │                    │                              pass ───┤─── fail
   │                    │                                │         │
   │              [05b Retry] ◄───────────────────── (first) ─────┘
   │              broaden query                          │  (already retried)
   │                                                     ▼         ▼
   │                                              [06 Generator]  honest fallback, stop
   │                                                     │
   │                                                     ▼
   │                                          [07 Faithfulness] ─► stream / escalate
```

Latency budget (SIMPLE_RAG, before stream starts): **~2.3–4.3 s**.
DIRECT path: **<50 ms**. Cache hit: **~50 ms**.

---

## How to read a node doc

Every node doc follows the same structure so they are scannable and comparable:

1. **Role** — one sentence, plus where it sits in the flow.
2. **Interface** — class, constructor dependencies, `name`.
3. **State contract** — which `PipelineState` keys it *reads* and *writes*.
4. **Behaviour** — the logic, step by step.
5. **Routing implications** — how its output steers the graph (where relevant).
6. **Error handling & edge cases** — failure modes and what happens.
7. **Design decisions** — the non-obvious choices and their rationale.
8. **Constraints honoured** — the CLAUDE.md rules this node is bound by.
9. **Test coverage** — what the tests prove.

---

## The state object

A single `PipelineState` (a `TypedDict`, `total=False`) flows through every node. Each
node reads the keys it needs and returns a **new merged dict** — nodes never mutate their
input (`return {**state, ...}`). This immutability is what makes the LangGraph
checkpoint/resume semantics safe.

| Key | Set by | Type | Meaning |
|---|---|---|---|
| `query` | entry | `str` | Original user query |
| `user_id`, `user_role`, `user_email` | entry | `str` | Identity from the session |
| `cache_hit` | 00 | `bool` | Whether a semantic-cache hit occurred |
| `cached_answer` | 00 | `str` | Cached answer (hit only) |
| `classification` | 01 | `str` | `"DIRECT"` or `"SIMPLE_RAG"` |
| `direct_response` | 01 | `str` | Canned response (DIRECT only) |
| `rewritten_query` | 02, 05b | `str` | Query optimised for retrieval |
| `sub_queries` | 02 | `list[str]` | Decomposed queries (post-MVP; always `[]` now) |
| `retrieved_chunks` | 03 | `list[ChunkPayload]` | Raw search hits |
| `reranked_chunks` | 04 | `list[ChunkPayload]` | Re-scored, sorted desc |
| `relevance_pass` | 05 | `bool` | Whether top score clears the threshold |
| `retry_attempted` | 05b | `bool` | Idempotent guard against retry loops |
| `generated_answer` | 06 | `str` | Buffered answer (held until 07 passes) |
| `is_faithful` | 07 | `bool` | Whether the answer is grounded |
| `faithfulness_score` | 07 | `float` | Fraction of grounded claims |

`ChunkPayload` fields: `chunk_id`, `text`, `document_id`, `original_filename`,
`category`, `page_number`, `chunk_index`, `score`.

---

## Cross-cutting conventions (a reminder, per CLAUDE.md)

These apply to **every** node — they are not repeated in full in each doc, only referenced:

- **Interface uniformity.** Every node subclasses `BaseNode`: a `name` property and
  `async execute(state) -> PipelineState`. (CLAUDE.md §7)
- **Dependency injection.** Services (LLM, embedder, vector store, reranker) and
  `Settings` are passed to the constructor — never imported as globals. This is what lets
  tests substitute mocks at the ABC boundary. (DEVLOG §2)
- **No vendor imports in nodes.** LLM access is only ever through `BaseLLMService`. No
  node imports `litellm`, `anthropic`, or `openai`. (CLAUDE.md §4, §12)
- **No hardcoded role names.** Role is always read from `state["user_role"]`; never
  `if role == "developer"`. (CLAUDE.md §12)
- **Immutable state.** Return `{**state, ...}`; never mutate the input dict.
- **Fail-open vs fail-closed.** *Routing* barriers are generous — on any error they fall
  through to retrieval (Classifier, Rewriter, Cache). *Safety* barriers are strict — they
  raise or block (Search, Reranker, Generator raise typed errors; Relevance Gate and
  Faithfulness fail to a safe negative). See each node's error section.
- **Security boundary is Node 03.** The Qdrant role filter is the hard access-control
  gate. Role framing in the Generator prompt is UX only and never a security control.
  (CLAUDE.md §5, §12)

---

## Node index

| Node | Doc | LLM | One-line role |
|---|---|---|---|
| 00 | [Cache Check](node_00_cache_check.md) | — | Return a cached answer for semantically-equivalent repeat queries |
| 01 | [Classifier](node_01_classifier.md) | `CLASSIFIER_MODEL` | Route DIRECT vs SIMPLE_RAG |
| 02 | [Rewriter](node_02_rewriter.md) | `REWRITER_MODEL` | Reformulate the query for retrieval quality |
| 03 | [Qdrant Search](node_03_search.md) | — | Embed + role-filtered vector search |
| 04 | [Reranker](node_04_reranker.md) | — | Cross-encoder re-scoring of candidates |
| 05 | [Relevance Gate](node_05_relevance_gate.md) | — | Threshold check on the top score |
| 05b | [Retry](node_05b_retry.md) | `REWRITER_MODEL` | One-shot query broadening on gate failure |
| 06 | [Generator](node_06_generator.md) | `GENERATOR_MODEL` | Buffered, cited, role-framed answer |
| 07 | [Faithfulness](node_07_faithfulness.md) | — | Token-overlap grounding check |
