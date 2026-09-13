"""
Structured logging with structlog.
- JSON renderer in production
- ConsoleRenderer locally
"""
import logging
import sys

import structlog

from app.config import get_settings

_HANDLER_NAME = "accountflow-structlog"


def configure_logging() -> None:
    settings = get_settings()

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.environment == "production":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    root_logger = logging.getLogger()
    # Idempotent: the API calls this at import and Celery via the
    # setup_logging signal. A second call must not add a second stdout
    # handler, or every line prints twice.
    if any(getattr(h, "name", None) == _HANDLER_NAME for h in root_logger.handlers):
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)

    # Quiet noisy libraries
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("celery").setLevel(logging.INFO)


def get_logger(name: str = __name__):
    return structlog.get_logger(name)
