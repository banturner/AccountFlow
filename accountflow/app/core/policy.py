"""Pure decision rules for the email pipeline.

These deliberately take plain values and return plain values — no database,
no Gmail, no Anthropic. The rules that decide whether something reaches a
customer used to live inline in a 200-line async worker function where they
could not be exercised without the whole stack standing up, which is why they
were never tested. Keeping them here makes the risky decisions unit-testable.
"""
from datetime import timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

DEFAULT_AUTO_SEND_THRESHOLD = 0.95

SGT = ZoneInfo("Asia/Singapore")

# A thread committed as `processing` whose task never completed. Younger than
# the window: a Claude call may legitimately still be running. Inside the
# window: its queue message was probably lost — re-dispatch once. The window
# is exactly one sweep interval wide (the sweeper runs every 10 minutes), so a
# thread cannot be re-dispatched twice and end up processed concurrently.
# Past the fail line: give up and surface it through the error-rate alert.
STUCK_REDISPATCH_WINDOW = (timedelta(minutes=15), timedelta(minutes=25))
STUCK_FAIL_AFTER = timedelta(minutes=60)


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


def _aware(dt):
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def monthly_period_needs_reset(monthly_reset_at, now) -> bool:
    """True if the tenant's monthly counter was last reset in an earlier
    Singapore calendar month than `now`.

    The counter used to depend solely on a Beat task firing at midnight on the
    1st; if Beat was down, a capped tenant stayed capped for the whole month
    and nothing could tell. Deriving the period at poll time makes the reset
    lazy and impossible to miss. A never-stamped tenant is not reset — the
    caller stamps it and the next month's boundary takes over.
    """
    if monthly_reset_at is None:
        return False
    last = _aware(monthly_reset_at).astimezone(SGT)
    current = _aware(now).astimezone(SGT)
    return (last.year, last.month) != (current.year, current.month)


def stuck_thread_action(created_at, now) -> Optional[str]:
    """What the sweeper should do with a thread still in `processing`.

    Returns "redispatch", "fail", or None (leave it alone for now).
    """
    age = _aware(now) - _aware(created_at)
    if age >= STUCK_FAIL_AFTER:
        return "fail"
    lower, upper = STUCK_REDISPATCH_WINDOW
    if lower <= age < upper:
        return "redispatch"
    return None
