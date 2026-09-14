"""Provider-agnostic mail + calendar surface.

The worker and the approval flow talk to this module, never to `gmail` or
`outlook` directly, so adding a provider means adding one file rather than
editing the pipeline.

The whole surface is five functions — that is the complete set of things
AccountFlow needs a mailbox to do:

    get_credentials      authenticate as the connected mailbox
    fetch_new_messages   what arrived since last time
    send_reply           reply, threaded, from the customer's own address
    create_appointment   put an event on their calendar
    check_availability   is that slot actually free

`Integration.provider` already carries the discriminator and the table has a
UniqueConstraint on (tenant_id, provider), so one tenant can hold a Google
and a Microsoft mailbox simultaneously.

Every provider module returns the SAME message dict shape, so the pipeline
never branches on provider:

    {message_id, thread_id, sender_email, sender_name, subject,
     body_text, rfc_message_id, rfc_references, received_at}
"""
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.integration import Integration

log = get_logger(__name__)

GOOGLE = "google"
MICROSOFT = "microsoft"
SUPPORTED_PROVIDERS = (GOOGLE, MICROSOFT)


class UnsupportedProviderError(Exception):
    pass


def _provider_of(integration: Integration) -> str:
    provider = (integration.provider or "").lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise UnsupportedProviderError(
            f"Integration {integration.id} has unknown provider {provider!r}"
        )
    return provider


async def get_credentials(db: AsyncSession, integration: Integration):
    """Return provider-native credentials, refreshing them if expired.

    Google yields a google.oauth2 Credentials object; Microsoft yields a bare
    access-token string. Callers should treat the result as opaque and hand it
    straight back to this module.
    """
    if _provider_of(integration) == GOOGLE:
        from app.services import gmail

        return await gmail.get_credentials(db, integration)

    from app.services import outlook

    return await outlook.get_access_token(db, integration)


async def fetch_new_messages(
    db: AsyncSession, integration: Integration, max_results: int = 50
) -> list[dict]:
    if _provider_of(integration) == GOOGLE:
        from app.services import gmail

        return await gmail.fetch_new_messages(db, integration, max_results=max_results)

    from app.services import outlook

    return await outlook.fetch_new_messages(db, integration, max_results=max_results)


async def send_reply(
    db: AsyncSession,
    integration: Integration,
    *,
    thread_id: str,
    message_id: Optional[str] = None,
    to_email: str,
    subject: str,
    body: str,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> Optional[str]:
    """Send a threaded reply. Returns a provider message id, or None on failure.

    `message_id` is the provider's id for the message being replied to. Gmail
    does not need it (threading is reconstructed from RFC headers), but Graph
    replies are addressed as an operation ON a message, so Outlook requires it.
    """
    if _provider_of(integration) == GOOGLE:
        from app.services import gmail

        return await gmail.send_reply(
            db=db,
            integration=integration,
            thread_id=thread_id,
            to_email=to_email,
            subject=subject,
            body=body,
            in_reply_to=in_reply_to,
            references=references,
        )

    from app.services import outlook

    return await outlook.send_reply(
        db=db,
        integration=integration,
        reply_to_message_id=message_id,
        to_email=to_email,
        subject=subject,
        body=body,
    )


async def create_appointment(
    db: AsyncSession,
    integration: Integration,
    *,
    title: str,
    start_datetime: datetime,
    duration_minutes: int = 60,
    attendee_email: Optional[str] = None,
    notes: Optional[str] = None,
    send_updates: str = "none",
) -> Optional[str]:
    """Create a calendar event. Returns the event id, or None on failure.

    send_updates="none" must not notify the customer — see
    app.core.policy.calendar_send_updates for why that matters.
    """
    creds = await get_credentials(db, integration)
    if not creds:
        return None

    if _provider_of(integration) == GOOGLE:
        from app.services.calendar import create_appointment as google_create

        return await google_create(
            creds=creds,
            title=title,
            start_datetime=start_datetime,
            duration_minutes=duration_minutes,
            attendee_email=attendee_email,
            notes=notes,
            send_updates=send_updates,
        )

    from app.services import outlook

    return await outlook.create_appointment(
        access_token=creds,
        title=title,
        start_datetime=start_datetime,
        duration_minutes=duration_minutes,
        attendee_email=attendee_email,
        notes=notes,
        send_updates=send_updates,
    )


async def check_availability(
    db: AsyncSession,
    integration: Integration,
    *,
    start_datetime: datetime,
    end_datetime: datetime,
) -> bool:
    """True if the slot is free and safe to book.

    Both providers fail CLOSED: an error returns False, so the caller flags the
    request for a human instead of risking a double booking.
    """
    creds = await get_credentials(db, integration)
    if not creds:
        return False

    if _provider_of(integration) == GOOGLE:
        from app.services.calendar import check_availability as google_check

        return await google_check(
            creds=creds,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
        )

    from app.services import outlook

    return await outlook.check_availability(
        access_token=creds,
        mailbox=integration.mailbox_address or "",
        start_datetime=start_datetime,
        end_datetime=end_datetime,
    )
