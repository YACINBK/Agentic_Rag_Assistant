from celery import Celery

from app.core.settings import settings

celery_app = Celery(
    "whitecape",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
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
