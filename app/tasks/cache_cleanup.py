import time

from app.tasks.worker import celery_app
from app.core.settings import settings


@celery_app.task
def purge_expired_cache():
    """Delete semantic_cache entries past their TTL.

    Runs hourly via Celery beat. Default TTL is 24 hours.
    """
    from qdrant_client import QdrantClient, models

    client = QdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)
    cutoff = time.time() - (settings.CACHE_TTL_HOURS * 3600)

    client.delete(
        collection_name=settings.QDRANT_CACHE_COLLECTION,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="created_at",
                        range=models.Range(lt=cutoff),
                    )
                ]
            )
        ),
    )
