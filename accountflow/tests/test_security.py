"""Unit tests for app.core.security — token encryption and OAuth state tokens."""
import uuid

from app.core.security import (
    create_state_token,
    decrypt_token,
    encrypt_token,
    generate_api_key,
    verify_api_key,
    verify_state_token,
)


def test_encrypt_decrypt_roundtrip():
    secret = "ya29.a0AfH6SMB-example-oauth-token"
    assert decrypt_token(encrypt_token(secret)) == secret


def test_encrypted_value_differs_from_plaintext():
    assert encrypt_token("abc") != "abc"


def test_state_token_roundtrip():
    tenant_id = str(uuid.uuid4())
    token = create_state_token(tenant_id)
    assert token != tenant_id                 # not a guessable raw UUID
    assert verify_state_token(token) == tenant_id


def test_state_token_tampered_rejected():
    token = create_state_token(str(uuid.uuid4()))
    assert verify_state_token(token[:-4] + "AAAA") is None
    assert verify_state_token("garbage") is None
    assert verify_state_token(str(uuid.uuid4())) is None  # raw UUID no longer accepted


def test_api_key_verification():
    assert verify_api_key("test-dashboard-key") is True
    assert verify_api_key("wrong-key") is False
    assert verify_api_key("") is False


def test_generate_api_key_entropy():
    keys = {generate_api_key() for _ in range(50)}
    assert len(keys) == 50
    assert all(len(k) >= 48 for k in keys)
