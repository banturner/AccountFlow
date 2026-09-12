"""
Minimal, hardened HS256 JWT implementation (stdlib only).

Why not python-jose? It is effectively unmaintained (known CVE history) and
pulls in a large dependency tree. For a single fixed algorithm (HS256) the
implementation below is small enough to audit line-by-line and avoids the
classic JWT pitfalls:

- The algorithm is HARD-CODED. The token's own header is never trusted to
  choose the verification algorithm (prevents alg=none / RS256->HS256 confusion).
- Signature comparison uses hmac.compare_digest (constant time).
- exp is always enforced; iat and a random jti are always set.
- Access and refresh tokens carry an explicit "type" claim so one can never
  be used in place of the other.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional

_HEADER_B64 = None  # computed once below


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _header_b64() -> str:
    global _HEADER_B64
    if _HEADER_B64 is None:
        _HEADER_B64 = _b64url_encode(
            json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode()
        )
    return _HEADER_B64


def _sign(signing_input: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()


def create_token(
    subject: str,
    token_type: str,
    secret: str,
    expires_in_seconds: int,
    extra_claims: Optional[dict] = None,
) -> str:
    """Create a signed HS256 JWT. token_type is 'access' or 'refresh'."""
    now = int(time.time())
    payload = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_in_seconds,
        "jti": secrets.token_hex(8),
        **(extra_claims or {}),
    }
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{_header_b64()}.{payload_b64}".encode("ascii")
    return f"{_header_b64()}.{payload_b64}.{_b64url_encode(_sign(signing_input, secret))}"


def decode_token(
    token: str,
    secret: str,
    expected_type: Optional[str] = None,
) -> Optional[dict]:
    """
    Verify and decode a JWT. Returns the payload dict, or None if the token
    is malformed, has a bad signature, is expired, or has the wrong type.
    """
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except (ValueError, AttributeError):
        return None

    # Recompute the signature over the received header+payload. The header's
    # declared algorithm is deliberately IGNORED — we only ever verify HS256.
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii", errors="replace")
    try:
        expected_sig = _sign(signing_input, secret)
        actual_sig = _b64url_decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected_sig, actual_sig):
        return None

    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None

    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or time.time() >= exp:
        return None

    if expected_type is not None and payload.get("type") != expected_type:
        return None

    return payload
