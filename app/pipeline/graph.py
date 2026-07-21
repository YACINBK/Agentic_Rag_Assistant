"""LangGraph pipeline wiring — connects all 9 nodes with conditional routing.

This module is the composition root: it creates the graph, adds all nodes,
and defines the routing logic between them. No business logic lives here —
each node is a self-contained BaseNode subclass.
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from app.core.base_node import BaseNode
from app.core.state import PipelineState

# ---------------------------------------------------------------------------
# Routing functions — pure Python, inspect state and return next node name
# ---------------------------------------------------------------------------


def route_cache(state: PipelineState) -> str:
    """Cache hit → END with cached_answer. Miss → classifier."""
    return END if state.get("cache_hit") else "classifier"


def route_classification(state: PipelineState) -> str:
    """DIRECT → END with canned response. SIMPLE_RAG → rewriter."""
    classification = state.get("classification", "")
    return END if classification == "DIRECT" else "rewriter"


def route_relevance(state: PipelineState) -> str:
    """Pass → generator. Fail → retry (first time) or END (exhausted)."""
    if state.get("relevance_pass"):
        return "generator"
    if state.get("retry_attempted"):
        return END  # retry already exhausted, honest fallback
    return "retry"


def route_faithfulness(state: PipelineState) -> str:
    """Faithful → END with answer. Not faithful → END with error."""
    return END


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_pipeline(nodes: dict[str, BaseNode]) -> StateGraph:
    """Build and compile the LangGraph pipeline.

    Args:
        nodes: dict mapping node names to BaseNode instances.
               Must contain all 9 nodes: cache_check, classifier, rewriter,
               qdrant_search, reranker, relevance_gate, retry, generator,
               faithfulness.

    Returns:
        Compiled StateGraph ready for ainvoke().
    """
    graph = StateGraph(PipelineState)

    # --- Add all nodes ---
    graph.add_node("cache_check", nodes["cache_check"].execute)
    graph.add_node("classifier", nodes["classifier"].execute)
    graph.add_node("rewriter", nodes["rewriter"].execute)
    graph.add_node("qdrant_search", nodes["qdrant_search"].execute)
    graph.add_node("reranker", nodes["reranker"].execute)
    graph.add_node("relevance_gate", nodes["relevance_gate"].execute)
    graph.add_node("retry", nodes["retry"].execute)
    graph.add_node("generator", nodes["generator"].execute)
    graph.add_node("faithfulness", nodes["faithfulness"].execute)

    # --- Entry point ---
    graph.set_entry_point("cache_check")

    # --- Edges ---
    # Cache check: hit → END, miss → classifier
    graph.add_conditional_edges("cache_check", route_cache, {
        "classifier": "classifier",
        END: END,
    })

    # Classifier: DIRECT → END, SIMPLE_RAG → rewriter
    graph.add_conditional_edges("classifier", route_classification, {
        "rewriter": "rewriter",
        END: END,
    })

    # SIMPLE_RAG chain: rewriter → search → rerank → gate (always sequential)
    graph.add_edge("rewriter", "qdrant_search")
    graph.add_edge("qdrant_search", "reranker")
    graph.add_edge("reranker", "relevance_gate")

    # Relevance gate: pass → generator, fail → retry or END
    graph.add_conditional_edges("relevance_gate", route_relevance, {
        "generator": "generator",
        "retry": "retry",
        END: END,
    })

    # Retry loops back to search
    graph.add_edge("retry", "qdrant_search")

    # Generator → faithfulness (always)
    graph.add_edge("generator", "faithfulness")

    # Faithfulness: always END (error handling done by orchestrator)
    graph.add_conditional_edges("faithfulness", route_faithfulness, {
        END: END,
    })

    return graph.compile()
