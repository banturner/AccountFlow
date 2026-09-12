"""Tests for emailed draft-review links.

Possession of one of these links is enough to send an email from a clinic's
own Gmail account, so the token is a credential and is tested like one.
"""
import time
import uuid

from app.core.security import (
    create_review_token,
    create_state_token,
    verify_review_token,
    verify_state_token,
)


def test_roundtrip_returns_the_draft_id():
    draft_id = str(uuid.uuid4())
    token = create_review_token(draft_id)
    assert verify_review_token(token) == draft_id


def test_token_does_not_leak_the_draft_id():
    draft_id = str(uuid.uuid4())
    assert draft_id not in create_review_token(draft_id)


def test_tokens_are_unguessable_and_non_repeating():
    draft_id = str(uuid.uuid4())
    # Fernet embeds a random IV, so the same draft yields a different token
    # each time — a token cannot be derived from a known draft id.
    assert create_review_token(draft_id) != create_review_token(draft_id)


def test_tampered_token_rejected():
    token = create_review_token(str(uuid.uuid4()))
    assert verify_review_token(token[:-4] + "AAAA") is None
    assert verify_review_token("garbage") is None
    assert verify_review_token("") is None


def test_raw_draft_id_is_not_accepted():
    # Guessing or enumerating a UUID must not be enough to approve a draft.
    assert verify_review_token(str(uuid.uuid4())) is None


def test_expired_token_rejected():
    token = create_review_token(str(uuid.uuid4()))
    time.sleep(1)
    assert verify_review_token(token, max_age_seconds=0) is None


def test_oauth_state_token_cannot_be_used_as_a_review_link():
    # Both are sealed with the same Fernet key, so they must be namespaced or
    # an OAuth state value would authorise sending mail.
    state = create_state_token(str(uuid.uuid4()))
    assert verify_review_token(state) is None


def test_review_token_cannot_be_used_as_an_oauth_state():
    review = create_review_token(str(uuid.uuid4()))
    assert verify_state_token(review) is None
