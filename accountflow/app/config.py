from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Fragments that only ever appear in template values (.env.example, the setup
# scripts' "fill me in later" markers, vendor docs). Matched case-insensitively.
_PLACEHOLDER_FRAGMENTS = (
    "changeme",
    "your_",
    "sk-ant-...",
    "sg.xxx",
    "xxx@sentry.io",
    "not-set-yet",
    "optional_not_configured",
)

# Secrets that must be present AND real before production will start.
_REQUIRED_SECRETS = ("FERNET_KEY", "JWT_SECRET_KEY", "DASHBOARD_API_KEY", "ANTHROPIC_API_KEY")


def _looks_like_placeholder(value: str) -> bool:
    lowered = (value or "").strip().lower()
    return bool(lowered) and any(fragment in lowered for fragment in _PLACEHOLDER_FRAGMENTS)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database. Runtime connects as the non-owner `accountflow_app` role so
    # row-level security applies (ADR-001); Alembic needs the table owner and
    # takes migration_database_url, falling back to database_url only for a
    # database that predates the role split.
    database_url: str
    migration_database_url: str = ""

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

    @model_validator(mode="after")
    def _refuse_placeholder_secrets_in_production(self):
        """Fail at startup, not at the first customer email, if .env.example
        values made it to a production box. Names only — never echo a value."""
        if self.environment != "production":
            return self

        candidates = {
            "DATABASE_URL": self.database_url,
            "MIGRATION_DATABASE_URL": self.migration_database_url,
            "REDIS_URL": self.redis_url,
            "FERNET_KEY": self.fernet_key,
            "DASHBOARD_API_KEY": self.dashboard_api_key,
            "JWT_SECRET_KEY": self.jwt_secret_key,
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
            "GOOGLE_CLIENT_ID": self.google_client_id,
            "GOOGLE_CLIENT_SECRET": self.google_client_secret,
            "MICROSOFT_CLIENT_SECRET": self.microsoft_client_secret,
            "SENDGRID_API_KEY": self.sendgrid_api_key,
            "SENTRY_DSN": self.sentry_dsn,
        }
        bad = sorted(
            name for name, value in candidates.items()
            if _looks_like_placeholder(value)
            or (name in _REQUIRED_SECRETS and not (value or "").strip())
        )
        if bad:
            raise ValueError(
                "Refusing to start with placeholder or blank secrets in production: "
                + ", ".join(bad)
                + ". Generate real values (see .env.example) or set ENVIRONMENT=development."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
