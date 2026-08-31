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
    # Browser-facing issuer URL. In Docker, KEYCLOAK_URL is the internal service
    # hostname while the browser still reaches the published host port.
    KEYCLOAK_PUBLIC_URL: str = "http://localhost:8080"
    KEYCLOAK_REALM: str = "whitecape"
    KEYCLOAK_CLIENT_ID: str = "whitecape-app"
    KEYCLOAK_CLIENT_SECRET: str = ""

    # --- Roles (PostgreSQL is authoritative — CLAUDE.md §2 MVP amendment, D40) ---
    # The Role.name a brand-new user is created with on first login. Keycloak
    # proves identity and carries no role information, so this is the only input
    # to a first-time user's role — and it comes from operator configuration, not
    # from a token, so no login can steer it.
    #
    # A reserved name (`all`/`admin`/`owner`) raises ValueError from the Role
    # validator at app/core/models/role.py:35 on the first login that needs to
    # create it. That is deliberate: a misconfigured DEFAULT_ROLE must fail loudly
    # rather than write a row that silently corrupts the §5 retrieval filter.
    DEFAULT_ROLE: str = "developer"

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
    ENRICHER_MODEL: str = "ollama/qwen2.5:14b"
    FAITHFULNESS_MODEL: str | None = None
    FAITHFULNESS_THRESHOLD: float = 0.5
    # Ceiling on every LiteLLM call. Without one, a hung Ollama request waits forever:
    # ingestion Stage C issues one sequential call per chunk (hundreds on the MVP
    # corpus), so a single stalled request pins a Celery worker slot and strands the
    # document at ingestion_status='running' with nothing to reap it. Deliberately
    # generous rather than tight — qwen2.5:32b runs 3–7 tok/s under partial GPU
    # offload and a cold model load costs 30–60 s, so a legitimate generator call can
    # take minutes. 600 s distinguishes "hung" from "slow", which is all it is for.
    LLM_TIMEOUT_SECONDS: float = 600.0

    # --- Embeddings (BGE-M3 via Ollama) ---
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    EMBEDDING_MODEL: str = "bge-m3"

    # --- Reranker (TEI) ---
    RERANKER_URL: str = "http://localhost:8082"
    # Ceiling on the TEI /rerank call. Was hardcoded 30.0 in the service, which the
    # real workload sits right on top of: bge-reranker-v2-m3 on CPU logs
    # total_time=26.7s for QDRANT_SEARCH_LIMIT=10 chunks of ~800 tokens (queue 5.8s
    # + inference 10.1s), because the Candle backend caps at batch size 4 and
    # serialises the rest. 26.7s under a 30s ceiling is a coin flip — the first UI
    # query after this was measured tripped it and surfaced as an empty-message
    # httpx.ReadTimeout ("Reranker failed: "). Same intent as
    # LLM_TIMEOUT_SECONDS: distinguish hung from slow, nothing more.
    RERANKER_TIMEOUT_SECONDS: float = 120.0

    # --- Ingestion ---
    CHUNK_SIZE_TOKENS: int = 800
    CHUNK_OVERLAP_TOKENS: int = 200
    # Ingestion is the only long-running task in the system. Stage C dominates its
    # runtime: one sequential LLM call per chunk (~2.9 s measured on qwen2.5:14b), so a
    # several-hundred-chunk document runs for tens of minutes, and a slower model or a
    # larger corpus pushes past an hour. The soft limit raises inside the task so the
    # orchestrator's except path can still record ingestion_status='failed'; the hard
    # limit is a SIGKILL backstop, set later so that write has room to land.
    INGESTION_SOFT_TIME_LIMIT: int = 7200  # 2 h
    INGESTION_TIME_LIMIT: int = 7500  # 2 h 5 min — 5 min of headroom over the soft limit

    # --- File storage (M3) ---
    UPLOAD_DIR: str = "./uploads"  # Relative for dev; mount a volume in Docker
    # Ceiling on a single admin upload, enforced by the M7 upload route only
    # (read at the call site, not baked into a default arg). 200 MiB leaves
    # headroom over the 146 MB Ask&Go export — the largest document the MVP
    # corpus is known to contain — without letting an unbounded in-memory read
    # (M7 buffers the whole body) exhaust the worker.
    MAX_UPLOAD_SIZE_BYTES: int = 209715200  # 200 MiB

    # --- Relevance gate ---
    RELEVANCE_THRESHOLD: float = 0.5

    # `.env` carries Docker Compose service names; `.env.local` (gitignored)
    # overrides them with localhost for tooling run from the host venv. Last file
    # wins. Containers are unaffected — compose passes env_file: .env as real
    # environment variables, which outrank any dotenv file.
    model_config = {"env_file": (".env", ".env.local"), "extra": "ignore"}


settings = Settings()
