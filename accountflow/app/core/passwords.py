"""
Password hashing with bcrypt (used directly — passlib 1.7.x is incompatible
with bcrypt >= 4 and unmaintained).

bcrypt truncates input at 72 bytes; we reject longer passwords instead of
silently truncating them.
"""
import bcrypt

_BCRYPT_MAX_BYTES = 72
MIN_PASSWORD_LENGTH = 12


class PasswordPolicyError(ValueError):
    pass


def validate_password_policy(plain: str) -> None:
    if len(plain) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if len(plain.encode("utf-8")) > _BCRYPT_MAX_BYTES:
        raise PasswordPolicyError("Password must be at most 72 bytes")


def hash_password(plain: str) -> str:
    """Hash a password with bcrypt (cost 12). CPU-bound ~100ms — call via
    asyncio.to_thread from async request handlers."""
    validate_password_policy(plain)
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time bcrypt verification. Never raises on bad input."""
    if not plain or not hashed:
        return False
    if len(plain.encode("utf-8")) > _BCRYPT_MAX_BYTES:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False
