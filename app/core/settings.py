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
    QDRANT_SEARCH_LIMIT: int = 10
    QDRANT_CACHE_COLLECTION: str = "semantic_cache"
    # BGE-M3 dense dimension. Qdrant fixes vector size at collection-creation time,
    # so bootstrap (M2) and ingestion (M5) must read one number. 1024 is BGE-M3's
    # documented dense dim — verify with `ollama show bge-m3` before real ingestion.
    EMBEDDING_DIM: int = 1024
    CACHE_SIMILARITY_THRESHOLD: float = 0.92
    CACHE_TTL_HOURS: int = 24

    # --- Keycloak ---
    KEYCLOAK_URL: str = "http://localhost:8080"
    KEYCLOAK_REALM: str = "whitecape"
    KEYCLOAK_CLIENT_ID: str = "whitecape-app"
    KEYCLOAK_CLIENT_SECRET: str = ""

    # --- Session ---
    SECRET_KEY: str = "change-me-in-production"
    SESSION_TTL_SECONDS: int = 86400  # 24h

    # --- Dev mode (bypass Keycloak + CSRF for local testing) ---
    DEV_MODE: bool = False
    # DEV-ONLY: bypass the real pipeline (Ollama/Qdrant/TEI) with a canned answer.
    DEV_FAKE_PIPELINE: bool = False

    # --- LLM models (all via LiteLLM → Ollama) ---
    CLASSIFIER_MODEL: str = "ollama/qwen2.5:7b"
    REWRITER_MODEL: str = "ollama/qwen2.5:14b"
    GENERATOR_MODEL: str = "ollama/qwen2.5:32b"
    FAITHFULNESS_MODEL: str | None = None
    FAITHFULNESS_THRESHOLD: float = 0.5

    # --- Embeddings (BGE-M3 via Ollama) ---
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    EMBEDDING_MODEL: str = "bge-m3"

    # --- Reranker (TEI) ---
    RERANKER_URL: str = "http://localhost:8082"

    # --- Ingestion ---
    CHUNK_SIZE_TOKENS: int = 800
    CHUNK_OVERLAP_TOKENS: int = 200

    # --- File storage (M3) ---
    UPLOAD_DIR: str = "./uploads"  # Relative for dev; mount a volume in Docker

    # --- Relevance gate ---
    RELEVANCE_THRESHOLD: float = 0.5

    # `.env` carries Docker Compose service names; `.env.local` (gitignored)
    # overrides them with localhost for tooling run from the host venv. Last file
    # wins. Containers are unaffected — compose passes env_file: .env as real
    # environment variables, which outrank any dotenv file.
    model_config = {"env_file": (".env", ".env.local"), "extra": "ignore"}


settings = Settings()
