from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "accountflow",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    # Serialisation
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Timezone
    timezone="Asia/Singapore",
    enable_utc=True,
    # Reliability
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    # Results
    result_expires=86400,  # 24h
    # Beat schedule — Gmail poll every 120 seconds
    beat_schedule={
        "poll-gmail-all-tenants": {
            "task": "app.worker.tasks.poll_all_tenants",
            "schedule": settings.gmail_poll_interval,  # seconds
        },
        "reset-monthly-email-counts": {
            "task": "app.worker.tasks.reset_monthly_email_counts",
            "schedule": crontab(hour=0, minute=0, day_of_month=1),
        },
        "check-tenant-error-rates": {
            "task": "app.worker.tasks.check_tenant_error_rates",
            "schedule": crontab(minute=15),  # hourly at :15
        },
        "send-weekly-digest": {
            "task": "app.worker.tasks.send_weekly_digest_all",
            # timezone is Asia/Singapore, so hour=9 == Monday 9:00 SGT
            "schedule": crontab(hour=9, minute=0, day_of_week=1),
        },
    },
)
