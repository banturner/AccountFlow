"""Password hashing (app/core/passwords.py)."""
import pytest

from app.core.passwords import (
    PasswordPolicyError,
    hash_password,
    validate_password_policy,
    verify_password,
)

GOOD = "correct-horse-battery"


def test_hash_and_verify():
    h = hash_password(GOOD)
    assert h != GOOD and h.startswith("$2")
    assert verify_password(GOOD, h) is True
    assert verify_password("wrong-password-123", h) is False


def test_short_password_rejected():
    with pytest.raises(PasswordPolicyError):
        hash_password("short")


def test_over_72_bytes_rejected_not_truncated():
    long_pw = "x" * 80
    with pytest.raises(PasswordPolicyError):
        validate_password_policy(long_pw)
    # verification with an over-long candidate never succeeds silently
    h = hash_password(GOOD)
    assert verify_password(GOOD + "x" * 80, h) is False


def test_bad_inputs_never_raise():
    h = hash_password(GOOD)
    assert verify_password("", h) is False
    assert verify_password(GOOD, "") is False
    assert verify_password(GOOD, "not-a-bcrypt-hash") is False


def test_unique_salts():
    assert hash_password(GOOD) != hash_password(GOOD)
