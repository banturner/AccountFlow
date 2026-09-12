"""app.config refuses to start in production with placeholder or blank secrets."""
import pytest
from pydantic import ValidationError

from app.config import Settings, _looks_like_placeholder


def _env(**overrides):
    base = dict(
        database_url="postgresql+asyncpg://u:p@db:5432/accountflow",
        redis_url="redis://:p@redis:6379/0",
        fernet_key="c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0LXNlY3JldA==",
        dashboard_api_key="a" * 32,
        jwt_secret_key="b" * 64,
        anthropic_api_key="sk-ant-api03-real-looking-key",
        google_client_id="123.apps.googleusercontent.com",
        google_client_secret="GOCSPX-realsecret",
        google_redirect_uri="https://app.example.com/api/auth/google/callback",
        sendgrid_api_key="SG.realkey",
        sendgrid_from_email="noreply@example.com",
        environment="production",
    )
    base.update(overrides)
    return base


def test_real_looking_production_settings_pass():
    Settings(_env_file=None, **_env())


@pytest.mark.parametrize(
    "field, value",
    [
        ("fernet_key", "your_fernet_key_here"),
        ("dashboard_api_key", "changeme_admin_key"),
        ("jwt_secret_key", "changeme_jwt_secret"),
        ("anthropic_api_key", "sk-ant-..."),
        ("database_url", "postgresql+asyncpg://accountflow:changeme_strong_password@db:5432/accountflow"),
        ("sendgrid_api_key", "SG.xxx"),
        ("sendgrid_api_key", "not-set-yet"),
        ("sentry_dsn", "https://xxx@sentry.io/xxx"),
        ("fernet_key", ""),
        ("jwt_secret_key", "   "),
    ],
)
def test_placeholder_secret_refused_in_production(field, value):
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, **_env(**{field: value}))
    message = str(exc.value)
    assert field.upper() in message
    if value.strip():
        assert value not in message  # names are reported, values never are


def test_placeholders_allowed_outside_production():
    Settings(_env_file=None, **_env(environment="development", fernet_key="your_fernet_key_here"))
    Settings(_env_file=None, **_env(environment="test", sendgrid_api_key="optional_not_configured"))


def test_blank_optional_secrets_are_fine():
    Settings(_env_file=None, **_env(sentry_dsn="", microsoft_client_secret=""))


def test_placeholder_detector_examples():
    assert _looks_like_placeholder("changeme_strong_password")
    assert _looks_like_placeholder("your_google_client_id")
    assert _looks_like_placeholder("SG.xxx")
    assert not _looks_like_placeholder("GOCSPX-2f9a8b7c")
    assert not _looks_like_placeholder("")
