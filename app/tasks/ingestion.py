from app.tasks.worker import celery_app


@celery_app.task(bind=True)
def ingest_document(self, document_id: str):
    """Async document ingestion triggered by admin upload.

    1. Read document from storage
    2. Chunk (800 tokens, 200 overlap, BGE-M3 tokenizer)
    3. Embed chunks via BGE-M3 (Ollama)
    4. Upsert to Qdrant with metadata + role filter payload
    5. Invalidate overlapping semantic_cache entries
    6. Update Document status in PostgreSQL
    """
    raise NotImplementedError("Ingestion task — implemented during loop engineering")
