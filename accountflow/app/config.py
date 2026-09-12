from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str

    # Redis
    redis_url: str

    # Security
    fernet_key: str
    dashboard_api_key: str          # internal ADMIN/ops key (tenant CRUD only)
    jwt_secret_key: str             # HS256 signing key for tenant JWTs
    access_token_minutes: int = 15
    refresh_token_days: int = 30

    # Anthropic
    anthropic_api_key: str
    ai_confidence_threshold: float = 0.85

    # Google OAuth
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str

    # Microsoft 365 / Outlook (Graph). Optional — blank on a Google-only
    # deployment, which is why these carry defaults and the Google ones do not.
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_redirect_uri: str = ""
    # "common" lets any Microsoft 365 tenant consent. Pin to a tenant id for a
    # single-organisation deployment.
    microsoft_authority: str = "https://login.microsoftonline.com/common"

    # SendGrid
    sendgrid_api_key: str
    sendgrid_from_email: str
    sendgrid_from_name: str = "AccountFlow"

    # Sentry
    sentry_dsn: str = ""

    # App
    environment: str = "production"
    gmail_poll_interval: int = 120
    app_base_url: str = "https://localhost"

    # How long an emailed draft-review link stays valid. Long enough to survive
    # a weekend and a holiday; short enough that a forwarded email doesn't stay
    # actionable forever.
    review_link_days: int = 7

    # Gmail poll batch size per tenant
    gmail_max_results: int = 50

    # Plan email caps
    # Monthly email-thread caps — must match the published pricing table
    # (Starter S$59 / Growth S$139 / Scale S$299-unlimited)
    plan_caps: dict = {
        "starter": 500,
        "growth": 3000,
        "pro": 10000,       # legacy tier — kept for backwards compatibility
        "scale": 999999,
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()
