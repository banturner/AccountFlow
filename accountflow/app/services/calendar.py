"""
Google Calendar service — create appointment events.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.core.logging import get_logger

log = get_logger(__name__)

SGT = ZoneInfo("Asia/Singapore")


def build_calendar_service(creds):
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _ensure_aware(dt: datetime) -> datetime:
    """Attach Singapore time to a naive datetime.

    The AI proposes times parsed out of free text, so they usually arrive
    naive ("2026-08-03T15:00:00"). events.insert tolerates that because it is
    sent alongside an explicit timeZone field, but freebusy.query has no such
    field and rejects a timestamp with no UTC offset — so an unnormalised
    datetime silently turns every availability check into an API error.
    """
    return dt.replace(tzinfo=SGT) if dt.tzinfo is None else dt


async def create_appointment(
    creds,
    title: str,
    start_datetime: datetime,
    duration_minutes: int = 60,
    attendee_email: Optional[str] = None,
    notes: Optional[str] = None,
    calendar_id: str = "primary",
    send_updates: str = "none",
) -> Optional[str]:
    """
    Create a Google Calendar event.
    Returns the Google Calendar event ID on success, None on failure.

    send_updates defaults to "none" on purpose. Google emails the attendee
    immediately when it is "all", which would push a booking confirmation to
    a patient without any human review — bypassing the approval gate the
    product promises customers. See app/core/policy.calendar_send_updates.
    """
    service = build_calendar_service(creds)
    start_datetime = _ensure_aware(start_datetime)
    end_datetime = start_datetime + timedelta(minutes=duration_minutes)

    event_body = {
        "summary": title,
        "description": notes or "",
        "start": {
            "dateTime": start_datetime.isoformat(),
            "timeZone": "Asia/Singapore",
        },
        "end": {
            "dateTime": end_datetime.isoformat(),
            "timeZone": "Asia/Singapore",
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "email", "minutes": 1440},  # 24h before
                {"method": "popup", "minutes": 30},
            ],
        },
    }

    if attendee_email:
        event_body["attendees"] = [{"email": attendee_email}]

    def _insert():
        return (
            service.events()
            .insert(calendarId=calendar_id, body=event_body, sendUpdates=send_updates)
            .execute()
        )

    try:
        event = await asyncio.to_thread(_insert)
        log.info("calendar_event_created", event_id=event["id"], title=title)
        return event["id"]
    except HttpError as e:
        log.error("calendar_create_error", error=str(e), title=title)
        return None


async def check_availability(
    creds,
    start_datetime: datetime,
    end_datetime: datetime,
    calendar_id: str = "primary",
) -> bool:
    """Check whether a time slot is free. True means safe to book.

    Fails CLOSED: any error returns False. We cannot distinguish "the calendar
    said busy" from "we could not ask", and for a clinic the two outcomes are
    not symmetric — a slot the owner has to book by hand costs a minute, while
    two patients booked into one chair costs the customer. The caller turns
    False into a flagged task rather than a silent booking.
    """
    service = build_calendar_service(creds)
    try:
        body = {
            "timeMin": _ensure_aware(start_datetime).isoformat(),
            "timeMax": _ensure_aware(end_datetime).isoformat(),
            "items": [{"id": calendar_id}],
        }
        result = await asyncio.to_thread(service.freebusy().query(body=body).execute)
        busy_slots = result["calendars"][calendar_id]["busy"]
        return len(busy_slots) == 0
    except (HttpError, KeyError, TypeError) as e:
        log.error("calendar_freebusy_error", error=str(e))
        return False
