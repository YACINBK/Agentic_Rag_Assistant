"""Pipeline factory — instantiates all nodes with production services, compiles the graph.

This is the composition root for the RAG pipeline: services (LiteLLM, Ollama
embedder, Qdrant, TEI reranker) are created here and injected into the 9 nodes.
"""

from __future__ import annotations

from langgraph.graph.state import CompiledStateGraph

from app.core.base_node import BaseNode
from app.core.settings import Settings
from app.pipeline.graph import build_pipeline
from app.pipeline.nodes.node_00_cache_check import CacheCheckNode
from app.pipeline.nodes.node_01_classifier import ClassifierNode
from app.pipeline.nodes.node_02_rewriter_decomposer import RewriterNode
from app.pipeline.nodes.node_03_parallel_qdrant_search import QdrantSearchNode
from app.pipeline.nodes.node_04_reranker import RerankerNode
from app.pipeline.nodes.node_05_relevance_gate import RelevanceGateNode
from app.pipeline.nodes.node_05b_retry import RetryNode
from app.pipeline.nodes.node_06_generator import GeneratorNode
from app.pipeline.nodes.node_07_faithfulness import FaithfulnessNode
from app.services.embedder import OllamaEmbedder
from app.services.llm import LiteLLMService
from app.services.reranker import TEIReranker
from app.services.vector_store import QdrantVectorStore


def build_pipeline_nodes(settings: Settings) -> dict[str, BaseNode]:
    """Instantiate all 9 pipeline nodes with their service dependencies."""
    llm = LiteLLMService()
    embedder = OllamaEmbedder()
    vector_store = QdrantVectorStore()
    reranker = TEIReranker()

    return {
        "cache_check": CacheCheckNode(
            embedder=embedder, vector_store=vector_store, settings=settings
        ),
        "classifier": ClassifierNode(llm=llm, settings=settings),
        "rewriter": RewriterNode(llm=llm, settings=settings),
        "qdrant_search": QdrantSearchNode(
            embedder=embedder, vector_store=vector_store, settings=settings
        ),
        "reranker": RerankerNode(reranker=reranker, settings=settings),
        "relevance_gate": RelevanceGateNode(settings=settings),
        "retry": RetryNode(llm=llm, settings=settings),
        "generator": GeneratorNode(llm=llm, settings=settings),
        "faithfulness": FaithfulnessNode(settings=settings),
    }


def get_compiled_pipeline(settings: Settings) -> CompiledStateGraph:
    """Build and compile the full pipeline graph. Ready for ainvoke()."""
    # DEV-ONLY: bypass all model services with a canned answer. Never set in prod.
    if getattr(settings, "DEV_FAKE_PIPELINE", False):
        from app.pipeline.dev_fake import get_fake_pipeline

        return get_fake_pipeline()
    return build_pipeline(build_pipeline_nodes(settings))
