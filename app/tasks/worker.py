from celery import Celery

from app.core.settings import settings

celery_app = Celery(
    "whitecape",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    # D25 fix. Without this the worker registers ZERO project tasks: `ingest_document`
    # is unreachable (an upload's row stays 'pending' forever) and so is
    # `purge_expired_cache`, which beat_schedule below names by dotted string without
    # importing it — beat publishes, the worker answers NotRegistered.
    #
    # `include=` and not a module-level `import app.tasks.ingestion`: that module does
    # `from app.tasks.worker import celery_app`, so importing it here is circular.
    # Celery imports these after the app object exists, which is what breaks the cycle.
    include=[
        "app.tasks.ingestion",
        "app.tasks.cache_cleanup",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "purge-expired-cache": {
            "task": "app.tasks.cache_cleanup.purge_expired_cache",
            "schedule": 3600.0,
        },
    },
)
