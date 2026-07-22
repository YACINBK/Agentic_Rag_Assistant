# Pipeline Graph — Visual Flow

```mermaid
flowchart TD
    START(("User Query"))

    START --> N0

    subgraph CACHE ["Cache Layer"]
        N0["**Node 00**<br/>Cache Check<br/>─────────<br/>BGE-M3 embed → Qdrant<br/>semantic_cache lookup<br/>role-scoped, 0.92 cosine"]
    end

    N0 -->|"cache hit"| HIT_END["✓ Return cached answer<br/>via SSE (~50ms)"]
    N0 -->|"cache miss"| N1

    subgraph CLASSIFY ["Classification"]
        N1["**Node 01**<br/>Classifier<br/>─────────<br/>CLASSIFIER_MODEL<br/>Binary: DIRECT / SIMPLE_RAG"]
    end

    N1 -->|"DIRECT"| DIRECT_END["✓ Canned response<br/>(polite redirect / honest decline)"]
    N1 -->|"SIMPLE_RAG"| N2

    subgraph RETRIEVAL ["Retrieval + Ranking"]
        N2["**Node 02**<br/>Rewriter<br/>─────────<br/>REWRITER_MODEL<br/>Optimise query for retrieval"]
        N3["**Node 03**<br/>Qdrant Search<br/>─────────<br/>BGE-M3 embed → Qdrant<br/>role filter (security boundary)"]
        N4["**Node 04**<br/>Reranker<br/>─────────<br/>bge-reranker-v2-m3<br/>via Hugging Face TEI"]
        N2 --> N3 --> N4
    end

    N4 --> N5

    subgraph GATE ["Quality Gate"]
        N5{"**Node 05**<br/>Relevance Gate<br/>─────────<br/>Score ≥ threshold?"}
    end

    N5 -->|"pass"| N6
    N5 -->|"fail<br/>(first time)"| N5B
    N5 -->|"fail<br/>(retry exhausted)"| FALLBACK_END["✓ Honest fallback<br/>(insufficient information)"]

    subgraph RETRY ["Single Retry"]
        N5B["**Node 05b**<br/>Retry<br/>─────────<br/>REWRITER_MODEL<br/>Broaden query"]
    end

    N5B -->|"broadened query"| N3

    subgraph GENERATION ["Generation + Verification"]
        N6["**Node 06**<br/>Generator<br/>─────────<br/>GENERATOR_MODEL<br/>Buffered output + citations"]
        N7{"**Node 07**<br/>Faithfulness<br/>─────────<br/>Token overlap heuristic<br/>Every claim grounded?"}
        N6 --> N7
    end

    N7 -->|"faithful"| FAITHFUL_END["✓ Stream answer<br/>via SSE"]
    N7 -->|"not faithful"| ESCALATION_END["✗ Answer discarded<br/>EscalationEvent logged"]

    %% Styling
    classDef startNode fill:#f9f9f9,stroke:#333,stroke-width:2px
    classDef nodeBox fill:#fff,stroke:#333,stroke-width:1px
    classDef decision fill:#fff,stroke:#333,stroke-width:2px
    classDef endpoint fill:#f0f0f0,stroke:#333,stroke-width:1px,stroke-dasharray: 5 5

    class START startNode
    class N0,N1,N2,N3,N4,N5B,N6 nodeBox
    class N5,N7 decision
    class HIT_END,DIRECT_END,FALLBACK_END,FAITHFUL_END,ESCALATION_END endpoint
```

## Reading the diagram

| Path | When | Latency |
|------|------|---------|
| Cache hit | Semantically equivalent query already answered for this role | ~50 ms |
| DIRECT | Chitchat, greetings, out-of-scope questions | < 50 ms |
| SIMPLE_RAG (happy) | Domain question, chunks found, answer faithful | ~2.3–4.3 s |
| Relevance retry | First search returns low-confidence chunks, broadened query retries | +1–2 s |
| Fallback | Both search attempts fail relevance threshold | ~1.5 s |
| Escalation | Generator produced ungrounded claims — answer discarded | same as happy path |

## Conditional routing logic

| After node | Condition | Next |
|------------|-----------|------|
| Cache Check | `cache_hit = True` | END (return cached answer) |
| Cache Check | `cache_hit = False` | Classifier |
| Classifier | `classification = "DIRECT"` | END (canned response) |
| Classifier | `classification = "SIMPLE_RAG"` | Rewriter |
| Relevance Gate | `relevance_pass = True` | Generator |
| Relevance Gate | `relevance_pass = False` and no retry yet | Retry |
| Relevance Gate | `relevance_pass = False` and `retry_attempted = True` | END (fallback) |
| Retry | always | Qdrant Search (re-enters retrieval) |
| Faithfulness | always | END (faithful → stream; not → escalate) |

## Key architectural notes

- **Security boundary** is at Node 03 — the Qdrant role filter ensures the LLM never sees chunks the user's role cannot access.
- **Buffered generation** — Node 06 output is held in memory, not streamed. SSE begins only after Node 07 confirms faithfulness.
- **Retry loops back to search** (Node 03), not to the rewriter. The retry node produces a broadened `rewritten_query` that re-enters the retrieval path.
- **No LLM cost** on the fast paths (cache hit, DIRECT, relevance fallback).
