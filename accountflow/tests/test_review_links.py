"""Tests for emailed draft-review links.

Possession of one of these links is enough to send an email from a clinic's
own mailbox, so the token is a credential and is tested like one. Since
ADR-001 it also carries the tenant id: the review route has no JWT, so the
token is the only thing that can tell it which tenant's context to bind before
it reads the drafts table.
"""
import time
import uuid

from app.core.security import (
    _REVIEW_PREFIX,
    _fernet,
    create_review_token,
    create_state_token,
    verify_review_token,
    verify_state_token,
)


def _ids():
    return str(uuid.uuid4()), str(uuid.uuid4())


def test_roundtrip_returns_the_tenant_and_draft_id():
    tenant_id, draft_id = _ids()
    token = create_review_token(tenant_id, draft_id)
    assert verify_review_token(token) == (tenant_id, draft_id)


def test_token_leaks_neither_id():
    tenant_id, draft_id = _ids()
    token = create_review_token(tenant_id, draft_id)
    assert draft_id not in token
    assert tenant_id not in token


def test_tokens_are_unguessable_and_non_repeating():
    tenant_id, draft_id = _ids()
    # Fernet embeds a random IV, so the same draft yields a different token
    # each time — a token cannot be derived from a known draft id.
    assert create_review_token(tenant_id, draft_id) != create_review_token(tenant_id, draft_id)


def test_tampered_token_rejected():
    token = create_review_token(*_ids())
    assert verify_review_token(token[:-4] + "AAAA") is None
    assert verify_review_token("garbage") is None
    assert verify_review_token("") is None


def test_raw_ids_are_not_accepted():
    # Guessing or enumerating UUIDs must not be enough to approve a draft.
    assert verify_review_token(str(uuid.uuid4())) is None
    assert verify_review_token(f"{uuid.uuid4()}:{uuid.uuid4()}") is None


def test_expired_token_rejected():
    token = create_review_token(*_ids())
    time.sleep(1)
    assert verify_review_token(token, max_age_seconds=0) is None


def test_legacy_token_without_a_tenant_id_is_rejected():
    # Pre-ADR-001 tokens carried the draft id alone. None was ever emailed to
    # a live tenant, and honouring one would mean reading a draft with no
    # tenant context to bind — so they are dead, not grandfathered.
    legacy = _fernet.encrypt(f"{_REVIEW_PREFIX}{uuid.uuid4()}".encode()).decode()
    assert verify_review_token(legacy) is None


def test_token_with_a_missing_half_is_rejected():
    draft_id, tenant_id = _ids()
    for payload in (
        f"{_REVIEW_PREFIX}:{draft_id}",
        f"{_REVIEW_PREFIX}{tenant_id}:",
        f"{_REVIEW_PREFIX}:",
        f"{_REVIEW_PREFIX}{tenant_id}:{draft_id}:extra",
    ):
        token = _fernet.encrypt(payload.encode()).decode()
        assert verify_review_token(token) is None, payload


def test_oauth_state_token_cannot_be_used_as_a_review_link():
    # Both are sealed with the same Fernet key, so they must be namespaced or
    # an OAuth state value would authorise sending mail.
    state = create_state_token(str(uuid.uuid4()))
    assert verify_review_token(state) is None


def test_review_token_cannot_be_used_as_an_oauth_state():
    review = create_review_token(*_ids())
    assert verify_state_token(review) is None
