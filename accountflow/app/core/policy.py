"""Pure decision rules for the email pipeline.

These deliberately take plain values and return plain values — no database,
no Gmail, no Anthropic. The rules that decide whether something reaches a
customer used to live inline in a 200-line async worker function where they
could not be exercised without the whole stack standing up, which is why they
were never tested. Keeping them here makes the risky decisions unit-testable.
"""
from typing import Optional

DEFAULT_AUTO_SEND_THRESHOLD = 0.95


def should_auto_send(
    auto_send_enabled: bool,
    auto_send_threshold: Optional[float],
    confidence: Optional[float],
) -> bool:
    """Decide whether an AI draft may be sent without human review.

    Fails closed: a missing confidence score means the AI told us nothing
    useful, so it must not be treated as a licence to email a customer.
    """
    if not auto_send_enabled:
        return False
    if confidence is None:
        return False
    threshold = (
        auto_send_threshold
        if auto_send_threshold is not None
        else DEFAULT_AUTO_SEND_THRESHOLD
    )
    return confidence >= threshold


def calendar_send_updates(auto_send_enabled: bool) -> str:
    """Google Calendar `sendUpdates` value when booking an appointment.

    While auto-send is off, the owner has promised customers that nothing
    reaches them without review. That promise has to cover calendar
    invitations too: Google emails the attendee the moment an event is
    created with sendUpdates="all", which would bypass the approval gate
    entirely. The event is still written to the owner's calendar either way.
    """
    return "all" if auto_send_enabled else "none"


def poll_is_stale(
    last_poll_at,
    now,
    max_silence_seconds: int = 3600,
) -> bool:
    """True if a connected inbox has not been polled recently enough.

    Polling runs every ~120s, so an hour of silence means the integration is
    broken (expired OAuth grant, revoked access, dead worker) rather than
    merely quiet. A never-polled integration is not stale — it is new.
    """
    if last_poll_at is None:
        return False
    return (now - last_poll_at).total_seconds() > max_silence_seconds
