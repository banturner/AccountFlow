"""Microsoft 365 / Outlook provider, via Microsoft Graph.

Mirrors app/services/gmail.py function-for-function so app/services/
mail_provider.py can dispatch between them without the pipeline noticing.

⚠️  NOT YET RUN AGAINST A LIVE TENANT. Written from the Graph reference; every
    call is marked with its endpoint so it can be checked against
    https://learn.microsoft.com/en-us/graph/api/overview. Work through
    OAUTH_DECISION_TESTS.md Test B before trusting any of it.

Why Graph rather than Gmail, in short: reading mail on Google is a RESTRICTED
scope requiring an annual third-party CASA audit, while the equivalent Graph
permissions need only (free) publisher verification and the customer admin's
consent. See OAUTH_DECISION_TESTS.md.

Three places Graph genuinely differs from Gmail, all handled below:

1. Threading is server-side. Graph replies are an operation ON a message
   (`/messages/{id}/createReply`) and it writes In-Reply-To/References itself,
   so we never hand-build RFC headers — which also removes the header
   injection class of bug that Gmail sending had to be hardened against.

2. Change tracking is a *delta link* (an opaque URL) rather than Gmail's
   historyId. It lives in Integration.sync_state.

3. There is no `sendUpdates` parameter. Graph emails invitations to any
   attendee on an event the moment it is created, so suppressing notification
   means creating the event WITHOUT attendees and naming the customer in the
   body instead.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.logging import get_logger
from app.core.security import decrypt_token, encrypt_token
from app.models.integration import Integration

settings = get_settings()
log = get_logger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
SGT = ZoneInfo("Asia/Singapore")

# Delegated permissions. Mail.Read + Mail.Send rather than Mail.ReadWrite:
# least privilege, and ReadWrite is blocked from end-user consent under
# Microsoft's default policy so it would force an extra admin hurdle.
SCOPES = [
    "https://graph.microsoft.com/Mail.Read",
    "https://graph.microsoft.com/Mail.Send",
    "https://graph.microsoft.com/Calendars.ReadWrite",
    "https://graph.microsoft.com/User.Read",
]

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _msal_app():
    """Build the MSAL confidential client. Imported lazily so a Google-only
    deployment never needs the dependency installed."""
    from msal import ConfidentialClientApplication

    return ConfidentialClientApplication(
        client_id=settings.microsoft_client_id,
        client_credential=settings.microsoft_client_secret,
        authority=settings.microsoft_authority,
    )


# ── Auth ──────────────────────────────────────────────────────────────────

async def exchange_code(code: str) -> Optional[dict]:
    """Swap an authorisation code for tokens. Called by the OAuth callback."""

    def _acquire():
        return _msal_app().acquire_token_by_authorization_code(
            code, scopes=SCOPES, redirect_uri=settings.microsoft_redirect_uri
        )

    result = await asyncio.to_thread(_acquire)
    if "access_token" not in result:
        log.error(
            "microsoft_token_exchange_failed",
            error=result.get("error"),
            description=result.get("error_description"),
        )
        return None
    return result


async def get_access_token(db: AsyncSession, integration: Integration) -> Optional[str]:
    """Return a valid access token, refreshing it if expired.

    Takes the same SELECT ... FOR UPDATE row lock as the Gmail path so two
    workers cannot refresh the same mailbox concurrently and race each other
    into an invalid refresh token.
    """
    result = await db.execute(
        select(Integration).where(Integration.id == integration.id).with_for_update()
    )
    locked = result.scalar_one_or_none()
    if not locked:
        return None

    expiry = locked.token_expiry
    if expiry and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    # 120s of slack so a token cannot expire mid-request.
    still_valid = expiry and expiry > datetime.now(timezone.utc) + timedelta(seconds=120)
    if still_valid and locked.access_token_enc:
        return decrypt_token(locked.access_token_enc)

    if not locked.refresh_token_enc:
        log.error("microsoft_no_refresh_token", integration_id=str(locked.id))
        locked.last_poll_error = "Microsoft sign-in expired. Reconnect the mailbox."
        return None

    refresh_token = decrypt_token(locked.refresh_token_enc)
    log.info("refreshing_microsoft_token", integration_id=str(locked.id))

    def _refresh():
        return _msal_app().acquire_token_by_refresh_token(refresh_token, scopes=SCOPES)

    result = await asyncio.to_thread(_refresh)
    if "access_token" not in result:
        log.error("microsoft_refresh_failed", error=result.get("error"))
        locked.last_poll_error = (
            f"Microsoft refused to refresh access: {result.get('error_description', '')}"[:500]
        )
        await db.flush()
        return None

    locked.access_token_enc = encrypt_token(result["access_token"])
    # Microsoft rotates refresh tokens; persist the new one or the next
    # refresh will present a revoked token.
    if result.get("refresh_token"):
        locked.refresh_token_enc = encrypt_token(result["refresh_token"])
    locked.token_expiry = datetime.now(timezone.utc) + timedelta(
        seconds=int(result.get("expires_in", 3600))
    )
    locked.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return result["access_token"]


def _headers(access_token: str, plain_text: bool = False) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    if plain_text:
        # Ask Graph for text/plain bodies. Without this every body arrives as
        # HTML and the AI ends up classifying markup.
        headers["Prefer"] = 'outlook.body-content-type="text"'
    return headers


# ── Reading mail ──────────────────────────────────────────────────────────

def parse_message(msg: dict) -> dict:
    """Normalise a Graph message into the shared pipeline shape."""
    sender = (msg.get("from") or {}).get("emailAddress") or {}
    received = msg.get("receivedDateTime")
    try:
        received_at = (
            datetime.fromisoformat(received.replace("Z", "+00:00"))
            if received
            else datetime.now(timezone.utc)
        )
    except (ValueError, AttributeError):
        received_at = datetime.now(timezone.utc)

    body = ((msg.get("body") or {}).get("content")) or msg.get("bodyPreview") or ""

    return {
        "message_id": msg.get("id"),
        # conversationId is Graph's thread identifier.
        "thread_id": msg.get("conversationId"),
        "sender_email": sender.get("address", ""),
        "sender_name": sender.get("name", ""),
        "subject": msg.get("subject") or "(no subject)",
        # Same 4k truncation as Gmail — keeps the Claude token cost predictable.
        "body_text": body[:4000],
        "rfc_message_id": msg.get("internetMessageId"),
        # Graph threads replies itself, so we never need the References chain.
        "rfc_references": None,
        "received_at": received_at,
    }


async def fetch_new_messages(
    db: AsyncSession, integration: Integration, max_results: int = 50
) -> list[dict]:
    """Fetch mail that arrived since the last poll, using a delta link.

    First poll: read recent inbox messages, then ask Graph for a delta link
    positioned at "now" ($deltatoken=latest) so the next poll returns only new
    mail. That mirrors the Gmail path's first-poll getProfile/historyId trick
    and avoids enumerating an entire mailbox on connect.
    """
    access_token = await get_access_token(db, integration)
    if not access_token:
        log.error("no_microsoft_credentials", integration_id=str(integration.id))
        integration.last_poll_error = "No usable Microsoft credentials for this mailbox."
        return []

    select_fields = (
        "id,conversationId,internetMessageId,subject,from,receivedDateTime,body,bodyPreview"
    )
    messages: list[dict] = []

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            if integration.sync_state:
                url = integration.sync_state
                # Page through until Graph hands back a fresh deltaLink.
                while url and len(messages) < max_results:
                    resp = await client.get(url, headers=_headers(access_token, plain_text=True))
                    if resp.status_code == 410:
                        # Delta token expired — reset and resync next cycle.
                        log.warning(
                            "microsoft_delta_expired", integration_id=str(integration.id)
                        )
                        integration.sync_state = None
                        integration.last_poll_error = None
                        await db.flush()
                        return []
                    resp.raise_for_status()
                    payload = resp.json()

                    for msg in payload.get("value", []):
                        # Deletions and read-state changes also arrive here.
                        if msg.get("@removed") or not msg.get("id"):
                            continue
                        messages.append(parse_message(msg))

                    if payload.get("@odata.deltaLink"):
                        integration.sync_state = payload["@odata.deltaLink"]
                        break
                    url = payload.get("@odata.nextLink")
            else:
                resp = await client.get(
                    f"{GRAPH}/me/mailFolders/inbox/messages",
                    headers=_headers(access_token, plain_text=True),
                    params={
                        "$select": select_fields,
                        "$top": max_results,
                        "$orderby": "receivedDateTime desc",
                        "$filter": "isRead eq false",
                    },
                )
                resp.raise_for_status()
                for msg in resp.json().get("value", []):
                    messages.append(parse_message(msg))

                # Park a delta link at "now" for subsequent polls.
                latest = await client.get(
                    f"{GRAPH}/me/mailFolders/inbox/messages/delta",
                    headers=_headers(access_token),
                    params={"$deltatoken": "latest"},
                )
                latest.raise_for_status()
                integration.sync_state = latest.json().get("@odata.deltaLink")

            integration.last_poll_error = None
            await db.flush()

    except httpx.HTTPStatusError as e:
        log.error(
            "graph_api_error",
            status=e.response.status_code,
            body=e.response.text[:300],
            integration_id=str(integration.id),
        )
        integration.last_poll_error = f"Graph error {e.response.status_code}"[:500]
        await db.flush()
    except httpx.HTTPError as e:
        log.error("graph_connection_error", error=str(e), integration_id=str(integration.id))
        integration.last_poll_error = str(e)[:500]
        await db.flush()

    return messages


# ── Sending ───────────────────────────────────────────────────────────────

async def send_reply(
    db: AsyncSession,
    integration: Integration,
    reply_to_message_id: Optional[str],
    to_email: str,
    subject: str,
    body: str,
) -> Optional[str]:
    """Reply in-thread from the connected mailbox. Returns the draft id.

    createReply -> PATCH body -> send, rather than the single-call
    /reply endpoint, because /reply returns 202 with an empty body and we
    would have no id to record against the draft. Graph fills in recipients
    and threading headers on the created reply, so there is nothing to
    hand-build and nothing to sanitise.
    """
    if not reply_to_message_id:
        log.error("microsoft_send_missing_message_id", to=to_email)
        return None

    access_token = await get_access_token(db, integration)
    if not access_token:
        return None

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            created = await client.post(
                f"{GRAPH}/me/messages/{reply_to_message_id}/createReply",
                headers=_headers(access_token),
            )
            created.raise_for_status()
            draft_id = created.json().get("id")
            if not draft_id:
                log.error("microsoft_createreply_no_id", to=to_email)
                return None

            patched = await client.patch(
                f"{GRAPH}/me/messages/{draft_id}",
                headers=_headers(access_token),
                json={"body": {"contentType": "Text", "content": body}},
            )
            patched.raise_for_status()

            sent = await client.post(
                f"{GRAPH}/me/messages/{draft_id}/send",
                headers=_headers(access_token),
            )
            sent.raise_for_status()

        log.info("email_sent_via_graph", message_id=draft_id, to=to_email)
        return draft_id

    except httpx.HTTPStatusError as e:
        log.error(
            "graph_send_error",
            status=e.response.status_code,
            body=e.response.text[:300],
            to=to_email,
        )
        return None
    except httpx.HTTPError as e:
        log.error("graph_send_connection_error", error=str(e), to=to_email)
        return None


# ── Calendar ──────────────────────────────────────────────────────────────

def _ensure_aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=SGT) if dt.tzinfo is None else dt


async def create_appointment(
    access_token: str,
    title: str,
    start_datetime: datetime,
    duration_minutes: int = 60,
    attendee_email: Optional[str] = None,
    notes: Optional[str] = None,
    send_updates: str = "none",
) -> Optional[str]:
    """Create an event. Returns the Graph event id, or None on failure.

    Graph has no sendUpdates switch: adding an attendee IS the notification.
    So when send_updates="none" the customer is deliberately left off the
    attendee list and named in the body instead — the event still lands on the
    owner's calendar, but nothing reaches the customer before a human approves.
    """
    start = _ensure_aware(start_datetime)
    end = start + timedelta(minutes=duration_minutes)
    notify = send_updates == "all"

    description = notes or ""
    if attendee_email and not notify:
        description = f"Requested by: {attendee_email}\n\n{description}".strip()

    event: dict = {
        "subject": title,
        "body": {"contentType": "Text", "content": description},
        "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Singapore"},
        "end": {"dateTime": end.isoformat(), "timeZone": "Asia/Singapore"},
    }
    if attendee_email and notify:
        event["attendees"] = [
            {"emailAddress": {"address": attendee_email}, "type": "required"}
        ]

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{GRAPH}/me/events", headers=_headers(access_token), json=event
            )
            resp.raise_for_status()
            event_id = resp.json().get("id")
        log.info("graph_event_created", event_id=event_id, title=title, notified=notify)
        return event_id
    except httpx.HTTPError as e:
        log.error("graph_event_error", error=str(e), title=title)
        return None


async def check_availability(
    access_token: str,
    mailbox: str,
    start_datetime: datetime,
    end_datetime: datetime,
) -> bool:
    """True if the slot is free. Fails CLOSED — see the Gmail counterpart.

    getSchedule returns availabilityView, one character per interval:
    0 free, 1 tentative, 2 busy, 3 out-of-office, 4 working-elsewhere.
    Anything other than all-zeroes means do not auto-book.
    """
    if not mailbox:
        log.error("graph_freebusy_no_mailbox")
        return False

    start = _ensure_aware(start_datetime)
    end = _ensure_aware(end_datetime)
    interval = max(int((end - start).total_seconds() // 60), 5)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{GRAPH}/me/calendar/getSchedule",
                headers=_headers(access_token),
                json={
                    "schedules": [mailbox],
                    "startTime": {"dateTime": start.isoformat(), "timeZone": "Asia/Singapore"},
                    "endTime": {"dateTime": end.isoformat(), "timeZone": "Asia/Singapore"},
                    "availabilityViewInterval": interval,
                },
            )
            resp.raise_for_status()
            schedules = resp.json().get("value") or []
            if not schedules:
                return False
            view = schedules[0].get("availabilityView") or ""
            return bool(view) and set(view) == {"0"}
    except (httpx.HTTPError, KeyError, TypeError, IndexError) as e:
        log.error("graph_freebusy_error", error=str(e))
        return False
