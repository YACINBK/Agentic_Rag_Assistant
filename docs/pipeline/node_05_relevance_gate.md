# Node 05 — Relevance Gate

**File:** `app/pipeline/nodes/node_05_relevance_gate.py` · **Class:** `RelevanceGateNode` · **LLM:** none

---

## 1. Role

A pure-Python quality gate. It asks one question: *did retrieval find anything actually
relevant?* If the best reranked chunk clears the relevance threshold, the pipeline
proceeds to generation. If not, it diverts to a single retry, and failing that, to an
honest fallback. This is what stops the system from generating an answer out of thin
evidence.

## 2. Interface

```python
RelevanceGateNode(settings: Settings)
name -> "relevance_gate"
```

## 3. State contract

| Reads | Writes |
|---|---|
| `reranked_chunks` | `relevance_pass: bool` |

## 4. Behaviour

1. If `reranked_chunks` is empty → `relevance_pass = False`.
2. Otherwise `relevance_pass = max(chunk.score) >= RELEVANCE_THRESHOLD` (default **0.5**).

The comparison is `>=`, so a score exactly at the threshold passes.

## 5. Routing implications

`route_relevance` in `graph.py`:

- `relevance_pass` true → `generator`.
- false **and** `retry_attempted` not set → `retry` (Node 05b).
- false **and** already retried → `END` (honest fallback — "insufficient information").

## 6. Error handling & edge cases

No external calls, so nothing to raise. It fails to a **safe negative** — an empty or
weak result set produces `relevance_pass = False`, which is the conservative outcome
(divert rather than generate).

- **Empty chunks** → `False`.
- **Exactly at threshold** → `True`.

## 7. Design decisions

| Decision | Rationale |
|---|---|
| Pure Python, no LLM | The decision is a numeric comparison; an LLM would add cost and latency for nothing. |
| Threshold on the **top** score | One strongly-relevant chunk is enough to ground an answer; the generator uses the whole set. |
| `>=` (inclusive) | Predictable boundary behaviour; the threshold value itself is a pass. |
| Empty → fail | No evidence must never be treated as passing. |

## 8. Constraints honoured

- Pure threshold check, no external dependencies (CLAUDE.md §6/§7).
- Immutable state update.

## 9. Test coverage (`tests/unit/test_node_05_relevance_gate.py`)

| Test | Proves |
|---|---|
| `test_pass_when_top_score_above_threshold` | Above threshold → pass |
| `test_fail_when_top_score_below_threshold` | Below threshold → fail |
| `test_fail_when_chunks_empty` | Empty → fail |
| `test_pass_at_exact_threshold` | Boundary is inclusive |
| `test_preserves_existing_state_keys` | Prior state intact + `relevance_pass` |
