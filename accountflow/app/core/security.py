"""
Security utilities:
- Fernet encryption for OAuth tokens stored in PostgreSQL
- API key verification (Phase 1 — JWT in Phase 2)
"""
import secrets
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

settings = get_settings()
_fernet = Fernet(settings.fernet_key.encode())


def encrypt_token(plain: str) -> str:
    """Encrypt an OAuth token before storing in DB."""
    return _fernet.encrypt(plain.encode()).decode()


def decrypt_token(cipher: str) -> str:
    """Decrypt an OAuth token retrieved from DB."""
    return _fernet.decrypt(cipher.encode()).decode()


def verify_api_key(api_key: str) -> bool:
    """Constant-time comparison against the dashboard API key."""
    return secrets.compare_digest(api_key, settings.dashboard_api_key)


def generate_api_key(length: int = 48) -> str:
    """Generate a cryptographically secure random API key."""
    return secrets.token_urlsafe(length)


# Namespaces draft-review tokens against OAuth state tokens, which are sealed
# with the same Fernet key and would otherwise be interchangeable.
_REVIEW_PREFIX = "draft-review:"


def create_state_token(payload: str) -> str:
    """Create an encrypted, tamper-proof OAuth state token.

    Prevents forged /auth/google/callback requests from binding an
    attacker-controlled Gmail account to an arbitrary tenant (CSRF).
    """
    return _fernet.encrypt(payload.encode()).decode()


def verify_state_token(token: str, max_age_seconds: int = 600) -> Optional[str]:
    """Verify and decode a state token. Returns None if invalid or older
    than max_age_seconds (default 10 minutes)."""
    try:
        raw = _fernet.decrypt(token.encode(), ttl=max_age_seconds).decode()
    except (InvalidToken, ValueError):
        return None
    # Both token kinds are sealed with the same key, so reject a draft-review
    # token replayed here rather than relying on the caller's UUID parse.
    if raw.startswith(_REVIEW_PREFIX):
        return None
    return raw


# ── Draft review links ────────────────────────────────────────────────────
# Emailed to the tenant owner so they can approve a draft without logging in.


def create_review_token(draft_id: str) -> str:
    """Create an encrypted, expiring token authorising review of ONE draft.

    Bearer-style: possession of the link is the authorisation, so it must be
    unguessable (Fernet ciphertext), scoped to a single draft, and expiring.
    """
    return _fernet.encrypt((_REVIEW_PREFIX + draft_id).encode()).decode()


def verify_review_token(token: str, max_age_seconds: int = 7 * 86400) -> Optional[str]:
    """Verify a draft-review token. Returns the draft id, or None if the token
    is invalid, expired, or is not a review token."""
    try:
        raw = _fernet.decrypt(token.encode(), ttl=max_age_seconds).decode()
    except (InvalidToken, ValueError):
        return None
    if not raw.startswith(_REVIEW_PREFIX):
        return None
    return raw[len(_REVIEW_PREFIX):] or None
