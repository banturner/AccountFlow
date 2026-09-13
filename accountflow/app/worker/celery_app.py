import sentry_sdk
from celery import Celery
from celery.schedules import crontab
from celery.signals import setup_logging

from app.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()

# The worker is where most failures happen. Without its own init, the
# capture_exception calls in tasks.py were silent no-ops outside the API process.
if settings.sentry_dsn and settings.environment != "development":
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        traces_sample_rate=0.1,
        send_default_pii=False,
    )

celery_app = Celery(
    "accountflow",
    broker=settings.redis_url,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    # Serialisation
    task_serializer="json",
    accept_content=["json"],
    # No result backend. Nothing reads task results, and keeping 24h of them
    # in the same Redis that is the broker competed with queued messages for
    # its 96 MB — a lost queue message is a patient email nobody answers.
    task_ignore_result=True,
    # Timezone
    timezone="Asia/Singapore",
    enable_utc=True,
    # Reliability
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    # Beat schedule — mailbox poll every 120 seconds
    beat_schedule={
        "poll-gmail-all-tenants": {
            "task": "app.worker.tasks.poll_all_tenants",
            "schedule": settings.gmail_poll_interval,  # seconds
        },
        # Threads committed as `processing` whose task never completed:
        # re-dispatched after 15 min, failed after 60 (policy.py). Duplicate
        # dispatches are harmless — process_single_email row-locks and
        # re-checks status — so the interval is about latency, not safety.
        "sweep-stuck-threads": {
            "task": "app.worker.tasks.sweep_stuck_threads",
            "schedule": 600.0,
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


@setup_logging.connect
def _configure_worker_logging(**_kwargs):
    # Celery installs its own root handlers at startup unless a receiver claims
    # this signal. Until now the worker and beat never went through structlog,
    # so production got plain-text lines instead of the JSON the API emits.
    configure_logging()
