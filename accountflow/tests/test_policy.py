"""Tests for the rules that decide whether something reaches a customer.

These are the decisions that cost money or reputation when they go wrong, and
until they were extracted into app/core/policy they could not be tested at all
without a database, Gmail and the Anthropic API.
"""
from datetime import datetime, timedelta, timezone

from app.core.policy import (
    DEFAULT_AUTO_SEND_THRESHOLD,
    calendar_send_updates,
    poll_is_stale,
    should_auto_send,
)


# ── should_auto_send ──────────────────────────────────────────────────────

def test_auto_send_off_never_sends_however_confident():
    # The pilot promise is "nothing goes out without approval". A 100%
    # confident draft must still wait when the tenant has not opted in.
    assert should_auto_send(False, 0.95, 1.0) is False


def test_auto_send_on_and_above_threshold():
    assert should_auto_send(True, 0.95, 0.96) is True


def test_auto_send_exactly_at_threshold_is_allowed():
    assert should_auto_send(True, 0.95, 0.95) is True


def test_auto_send_below_threshold_waits_for_review():
    assert should_auto_send(True, 0.95, 0.94) is False


def test_missing_confidence_fails_closed():
    # No score means the AI told us nothing useful — that is not a licence
    # to email a patient.
    assert should_auto_send(True, 0.95, None) is False


def test_missing_threshold_falls_back_to_default():
    assert should_auto_send(True, None, DEFAULT_AUTO_SEND_THRESHOLD) is True
    assert should_auto_send(True, None, DEFAULT_AUTO_SEND_THRESHOLD - 0.01) is False


def test_threshold_of_one_requires_total_certainty():
    assert should_auto_send(True, 1.0, 0.999) is False
    assert should_auto_send(True, 1.0, 1.0) is True


# ── calendar_send_updates ─────────────────────────────────────────────────

def test_calendar_invites_suppressed_while_review_gate_is_on():
    # Google emails the attendee immediately on "all"; that would bypass the
    # approval gate just as surely as sending a reply would.
    assert calendar_send_updates(False) == "none"


def test_calendar_invites_sent_once_auto_send_is_trusted():
    assert calendar_send_updates(True) == "all"


# ── poll_is_stale ─────────────────────────────────────────────────────────

def test_never_polled_is_not_stale():
    # A freshly connected mailbox has not failed — it has not started.
    assert poll_is_stale(None, datetime.now(timezone.utc)) is False


def test_recent_poll_is_healthy():
    now = datetime.now(timezone.utc)
    assert poll_is_stale(now - timedelta(minutes=3), now) is False


def test_hours_of_silence_is_stale():
    # Polling runs every ~120s, so this cannot be a quiet inbox.
    now = datetime.now(timezone.utc)
    assert poll_is_stale(now - timedelta(hours=6), now) is True


def test_stale_boundary_respects_custom_window():
    now = datetime.now(timezone.utc)
    ten_min_ago = now - timedelta(minutes=10)
    assert poll_is_stale(ten_min_ago, now, max_silence_seconds=1800) is False
    assert poll_is_stale(ten_min_ago, now, max_silence_seconds=300) is True
