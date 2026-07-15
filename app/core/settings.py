from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Database ---
    DATABASE_URL: str = "postgresql+asyncpg://whitecape:whitecape@localhost:5432/whitecape"

    # --- Redis ---
    REDIS_URL: str = "redis://localhost:6379/0"

    # --- Qdrant ---
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333
    QDRANT_COLLECTION: str = "documents"
    QDRANT_CACHE_COLLECTION: str = "semantic_cache"
    CACHE_SIMILARITY_THRESHOLD: float = 0.92
    CACHE_TTL_HOURS: int = 24

    # --- Keycloak ---
    KEYCLOAK_URL: str = "http://localhost:8080"
    KEYCLOAK_REALM: str = "whitecape"
    KEYCLOAK_CLIENT_ID: str = "whitecape-app"
    KEYCLOAK_CLIENT_SECRET: str = ""

    # --- LLM models (all via LiteLLM → Ollama) ---
    CLASSIFIER_MODEL: str = "ollama/qwen2.5:7b"
    REWRITER_MODEL: str = "ollama/qwen2.5:14b"
    GENERATOR_MODEL: str = "ollama/qwen2.5:32b"
    FAITHFULNESS_MODEL: str | None = None

    # --- Embeddings (BGE-M3 via Ollama) ---
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    EMBEDDING_MODEL: str = "bge-m3"

    # --- Reranker (TEI) ---
    RERANKER_URL: str = "http://localhost:8082"

    # --- Ingestion ---
    CHUNK_SIZE_TOKENS: int = 800
    CHUNK_OVERLAP_TOKENS: int = 200

    # --- Relevance gate ---
    RELEVANCE_THRESHOLD: float = 0.5

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
