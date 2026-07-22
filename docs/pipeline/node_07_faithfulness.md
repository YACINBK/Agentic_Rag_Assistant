# Node 07 — Faithfulness Checker

**File:** `app/pipeline/nodes/node_07_faithfulness.py` · **Class:** `FaithfulnessNode` · **LLM:** none (token-overlap heuristic)

---

## 1. Role

The final safety barrier. It verifies that the buffered answer is actually grounded in
the retrieved chunks before anything is streamed to the user. If the answer drifts from
the evidence — a hallucination — the verdict is unfaithful and the answer is discarded in
favour of a safe fallback (and an escalation event). This is what makes "every answer is
traceable to a source" a guarantee rather than a hope.

MVP uses a zero-cost token-overlap heuristic. An LLM-based checker is post-MVP
(CLAUDE.md §14, §16).

## 2. Interface

```python
FaithfulnessNode(settings: Settings)
name -> "faithfulness"
```

## 3. State contract

| Reads | Writes |
|---|---|
| `generated_answer`, `reranked_chunks` | `is_faithful: bool`, `faithfulness_score: float` |

## 4. Behaviour

1. If the answer is empty **or** there are no chunks → `is_faithful = False`,
   `faithfulness_score = 0.0`.
2. Build the chunk vocabulary: the union of tokenised words across all chunks
   (lowercased, punctuation stripped, words of length > 1).
3. Split the answer into claims on sentence boundaries (`.`, `!`, `?`).
4. For each claim, compute overlap = |claim words ∩ chunk vocab| / |claim words|. The
   claim is **grounded** if overlap `>= 0.3`.
5. `faithfulness_score = grounded_claims / total_claims`.
6. `is_faithful = faithfulness_score >= FAITHFULNESS_THRESHOLD` (default **0.5**).

## 5. Routing implications

`route_faithfulness` in `graph.py` always returns `END` — the graph terminates here. The
verdict (`is_faithful`) is surfaced in state, and the **SSE orchestrator** (Phase 3)
decides: faithful → stream the buffered answer; unfaithful → discard it, return the safe
fallback, and write an `EscalationEvent`. Keeping that branch in the orchestrator (not
the graph) keeps the graph a pure state machine.

## 6. Error handling & edge cases

Pure computation — nothing to raise. Fails to a **safe negative**:

- **Empty answer** → unfaithful, score 0.0.
- **No chunks** → unfaithful, score 0.0 (nothing to be grounded in).
- **No token overlap at all** → score 0.0, unfaithful.

## 7. Design decisions

| Decision | Rationale |
|---|---|
| Token overlap, no LLM (MVP) | Zero cost, <5 ms; catches the egregious hallucinations (an answer about topics in no chunk). LLM-based check is post-MVP (CLAUDE.md §16). |
| Per-claim 0.3 overlap | Empirically catches ungrounded sentences without over-penalising natural paraphrase. Currently a code constant. |
| Overall 0.5 threshold | Majority of claims must be grounded for the answer to ship; tunable via settings. |
| Runs on the complete buffered answer | You cannot verify a partial answer — this is why Node 06 buffers (CLAUDE.md §12). |
| Fail to unfaithful | When in doubt, don't ship — safety over availability for the final gate. |

## 8. Constraints honoured

- No LLM call — token-overlap heuristic for MVP (CLAUDE.md §14, §16).
- Runs on the complete buffered answer before any SSE output (CLAUDE.md §6, §12).
- Immutable state update.

## 9. Test coverage (`tests/unit/test_node_07_faithfulness.py`)

| Test | Proves |
|---|---|
| `test_fully_grounded_answer_is_faithful` | Answer echoing the chunk → faithful, score > 0.5 |
| `test_hallucinated_answer_is_not_faithful` | Answer unrelated to chunks → unfaithful, score < 0.5 |
| `test_empty_answer_returns_unfaithful` | Empty answer → unfaithful, 0.0 |
| `test_empty_chunks_returns_unfaithful` | No chunks → unfaithful, 0.0 |
| `test_preserves_existing_state_keys` | Prior state intact + verdict fields |
| `test_score_is_zero_when_no_overlap` | Disjoint vocab → 0.0, unfaithful |
