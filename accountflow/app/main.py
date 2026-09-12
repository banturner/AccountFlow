"""
AccountFlow FastAPI application entry point.
"""
import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.core.logging import configure_logging
from app.api.routes import (
    health,
    auth,
    drafts,
    review,
    tasks,
    tenants,
    settings as settings_router,
)

# Initialise logging first
configure_logging()

_settings = get_settings()

# Sentry — only in non-development environments
if _settings.sentry_dsn and _settings.environment != "development":
    sentry_sdk.init(
        dsn=_settings.sentry_dsn,
        environment=_settings.environment,
        traces_sample_rate=0.2,
        profiles_sample_rate=0.1,
        # PDPA: never attach request bodies, cookies or user identity to events.
        send_default_pii=False,
    )

app = FastAPI(
    title="AccountFlow API",
    description="AI-powered customer email automation for Singapore SMBs",
    version="1.0.0",
    docs_url="/api/docs" if _settings.environment != "production" else None,
    redoc_url="/api/redoc" if _settings.environment != "production" else None,
)

# CORS — tighten origins once the Next.js dashboard domain is known
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _settings.environment == "development" else [_settings.app_base_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routes under /api prefix
app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(drafts.router, prefix="/api")
# Token-authorised HTML review pages — no JWT, the emailed link is the auth
app.include_router(review.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")
app.include_router(settings_router.router, prefix="/api")
app.include_router(tenants.router, prefix="/api")


@app.get("/", include_in_schema=False)
async def root():
    return {"service": "AccountFlow API", "version": "1.0.0"}
