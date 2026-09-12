"""JWT (app/core/tokens.py) — signature, expiry, type and tamper handling."""
import base64
import json
import time

from app.core.tokens import _b64url_decode, _b64url_encode, create_token, decode_token

SECRET = "test-secret-key-for-jwt"


def test_roundtrip():
    token = create_token("tenant-123", "access", SECRET, 900)
    payload = decode_token(token, SECRET, expected_type="access")
    assert payload is not None
    assert payload["sub"] == "tenant-123"
    assert payload["type"] == "access"
    assert payload["exp"] > time.time()


def test_wrong_secret_rejected():
    token = create_token("tenant-123", "access", SECRET, 900)
    assert decode_token(token, "other-secret", expected_type="access") is None


def test_expired_rejected():
    token = create_token("tenant-123", "access", SECRET, -1)
    assert decode_token(token, SECRET) is None


def test_refresh_not_usable_as_access():
    token = create_token("tenant-123", "refresh", SECRET, 900)
    assert decode_token(token, SECRET, expected_type="access") is None
    assert decode_token(token, SECRET, expected_type="refresh") is not None


def test_tampered_payload_rejected():
    token = create_token("tenant-123", "access", SECRET, 900)
    header, payload_b64, sig = token.split(".")
    payload = json.loads(_b64url_decode(payload_b64))
    payload["sub"] = "tenant-999"  # privilege escalation attempt
    forged = f"{header}.{_b64url_encode(json.dumps(payload).encode())}.{sig}"
    assert decode_token(forged, SECRET) is None


def test_alg_none_confusion_rejected():
    """A forged token with alg=none and empty signature must be rejected —
    verification always recomputes HS256 and ignores the header's alg."""
    header = _b64url_encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url_encode(
        json.dumps({"sub": "x", "type": "access", "exp": int(time.time()) + 900}).encode()
    )
    assert decode_token(f"{header}.{payload}.", SECRET) is None
    assert decode_token(f"{header}.{payload}.{_b64url_encode(b'')}", SECRET) is None


def test_garbage_rejected():
    for bad in ["", "a.b", "a.b.c.d", "not-a-token", None]:
        assert decode_token(bad, SECRET) is None


def test_unique_jti():
    t1 = create_token("t", "access", SECRET, 900)
    t2 = create_token("t", "access", SECRET, 900)
    assert t1 != t2
