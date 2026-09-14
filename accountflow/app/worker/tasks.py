"""
Core Celery tasks — the Gmail → Claude → Action pipeline.

Every task takes tenant_id as its FIRST argument and runs inside that
tenant's row-level-security context (ADR-001). Nothing here derives the tenant
from a row it has just read: the queue message carries it, and under RLS a
stale or mispaired message resolves to zero rows rather than another clinic's
mailbox. Cross-tenant sweeps read `tenants` (not under RLS) and then work per
tenant with the context set, one tenant's failure never ending the sweep.

Flow per tenant:
  poll_all_tenants                     (iterates tenants, sets the context)
    └── process_tenant_inbox (per active integration)
         └── for each new message:
              process_single_email
                ├── classify_and_draft (Claude)
                ├── draft_reply → save Draft (pending_review or auto_sent if confidence > threshold)
                ├── create_appointment → save Task + Google Calendar event
                ├── create_task → save Task
                └── flag_human → save EmailThread with status=flagged
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
import uuid

import sentry_sdk
from celery import Task

from app.worker.celery_app import celery_app
from app.config import get_settings
from app.core.logging import get_logger

settings = get_settings()
log = get_logger(__name__)


def run_async(coro, tenant_id: Optional[str] = None):
    """Run an async coroutine from a sync Celery task.

    Each task gets a fresh event loop, so the shared async engine's pooled
    connections must be disposed before the loop closes — otherwise asyncpg
    connections leak across loops and raise 'attached to a different loop'.

    `tenant_id` binds the row-level-security context (ADR-001) around the run.
    asyncio copies the current context when it wraps the coroutine in a Task,
    so entering the scope here means every transaction the task opens carries
    `app.tenant_id`, and it is released when the task ends — the next task on
    this worker starts unbound. Tasks that are genuinely cross-tenant
    (poll_all_tenants and the sweeps) pass nothing and set the context per
    tenant inside their own loops.
    """
    from app.database import tenant_scope

    async def _wrapped():
        from app.database import engine
        try:
            return await coro
        finally:
            await engine.dispose()

    loop = asyncio.new_event_loop()
    try:
        if tenant_id is None:
            return loop.run_until_complete(_wrapped())
        with tenant_scope(tenant_id):
            return loop.run_until_complete(_wrapped())
    finally:
        loop.close()


def _warn_if_unscoped(where: str) -> None:
    """Shout if a tenant-scoped worker path runs with no tenant context.

    Under row-level security an unset context makes every query return zero
    rows, so the task finishes cleanly having done nothing — indistinguishable
    from an empty inbox (ADR-001, "Consequences"). This makes it searchable.
    """
    from app.database import current_tenant_id

    if current_tenant_id() is None:
        log.error("tenant_context_missing", where=where)


@celery_app.task(
    name="app.worker.tasks.poll_all_tenants",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def poll_all_tenants(self: Task):
    """
    Triggered every 120 seconds by Celery Beat.
    Iterates active tenants and fans out one task per active integration.
    """
    return run_async(_poll_all_tenants_async())


async def _poll_all_tenants_async():
    """Tenant-first fan-out.

    This used to select every integration in one cross-tenant query and send
    the worker a bare integration_id, leaving the tenant to be re-derived from
    the row at the other end. Now the loop is what knows the tenant: it reads
    `tenants` (not under RLS), then lists each tenant's integrations with that
    tenant's context set, so the ids in a queue message provably came from the
    same scope.
    """
    from sqlalchemy import select
    from app.database import AsyncSessionLocal, apply_tenant_context, tenant_scope
    from app.models.integration import Integration
    from app.models.tenant import Tenant

    fan_out: list[tuple[str, str]] = []

    async with AsyncSessionLocal() as db:
        tenant_ids = (
            await db.execute(select(Tenant.id).where(Tenant.is_active == True))  # noqa: E712
        ).scalars().all()

        for tenant_id in tenant_ids:
            try:
                with tenant_scope(tenant_id):
                    # The session's transaction is already open, so after_begin
                    # will not fire again — write the context in explicitly.
                    await apply_tenant_context(db)
                    integration_ids = (
                        await db.execute(
                            select(Integration.id).where(
                                Integration.tenant_id == tenant_id,
                                Integration.is_active == True,  # noqa: E712
                            )
                        )
                    ).scalars().all()
            except Exception as e:
                # One tenant's bad row must not stop every other clinic's mail
                # from being polled for the rest of the cycle. The rollback is
                # the part that makes that true for a DATABASE error: all
                # tenants share one session, and an aborted transaction fails
                # every subsequent statement on it until it is rolled back.
                await db.rollback()
                log.error("poll_tenant_failed", tenant_id=str(tenant_id), error=str(e))
                sentry_sdk.capture_exception(e)
                continue
            fan_out.extend((str(tenant_id), str(i)) for i in integration_ids)

    log.info("polling_tenants", tenants=len(tenant_ids), integrations=len(fan_out))

    for tenant_id, integration_id in fan_out:
        try:
            process_tenant_inbox.delay(tenant_id, integration_id)
        except Exception as e:
            # Broker full (noeviction) or unreachable. Missing one poll cycle
            # is recoverable — the next one re-reads the same mailbox — but a
            # single publish failure must not strand the remaining tenants.
            log.error("poll_dispatch_failed", integration_id=integration_id, error=str(e))
            sentry_sdk.capture_exception(e)


@celery_app.task(
    name="app.worker.tasks.process_tenant_inbox",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def process_tenant_inbox(self: Task, tenant_id: str, integration_id: str):
    """Process all new emails for a single tenant integration."""
    try:
        return run_async(
            _process_tenant_inbox_async(tenant_id, integration_id), tenant_id=tenant_id
        )
    except Exception as exc:
        log.error("process_tenant_inbox_failed", integration_id=integration_id, error=str(exc))
        sentry_sdk.capture_exception(exc)
        raise self.retry(exc=exc)


async def _process_tenant_inbox_async(tenant_id: str, integration_id: str):
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from app.core.policy import monthly_period_needs_reset
    from app.database import AsyncSessionLocal
    from app.models.integration import Integration
    from app.models.tenant import Tenant
    from app.models.email_thread import EmailThread
    from app.services.mail_provider import fetch_new_messages
    from app.services.sendgrid import send_email

    _warn_if_unscoped("process_tenant_inbox")

    async with AsyncSessionLocal() as db:
        # `tenants` is deliberately outside RLS (login reads it before any
        # context can exist), so this one is filtered by hand.
        tenant_result = await db.execute(
            select(Tenant).where(Tenant.id == uuid.UUID(tenant_id))
        )
        tenant = tenant_result.scalar_one_or_none()
        if not tenant or not tenant.is_active:
            return

        # Under RLS this matches nothing unless the integration belongs to the
        # tenant whose context is set, so a stale message pairing one tenant's
        # id with another's mailbox reads as "not found" and stops here.
        result = await db.execute(
            select(Integration).where(
                Integration.id == uuid.UUID(integration_id),
                Integration.tenant_id == tenant.id,
            )
        )
        integration = result.scalar_one_or_none()
        if not integration:
            log.warning("integration_not_found", integration_id=integration_id)
            return

        now = datetime.now(timezone.utc)

        # Lazy monthly reset: derive the period at poll time so a Beat outage
        # on the 1st cannot leave a tenant capped for the rest of the month.
        if tenant.monthly_reset_at is None:
            tenant.monthly_reset_at = now
        elif monthly_period_needs_reset(tenant.monthly_reset_at, now):
            log.info(
                "monthly_count_reset_lazy",
                tenant_id=str(tenant.id),
                previous=tenant.monthly_email_count,
            )
            tenant.monthly_email_count = 0
            tenant.monthly_reset_at = now

        # Stamped BEFORE the cap check and whether or not mail is found: a
        # capped tenant is healthy, and without this stamp the staleness alert
        # would tell them their inbox connection is broken.
        integration.last_poll_at = now

        cap = settings.plan_caps.get(tenant.plan, 500)
        if tenant.monthly_email_count >= cap:
            log.warning(
                "monthly_cap_reached",
                tenant_id=str(tenant.id),
                plan=tenant.plan,
                cap=cap,
                count=tenant.monthly_email_count,
            )
            await db.commit()
            return

        messages = await fetch_new_messages(db, integration)
        log.info("fetched_messages", count=len(messages), tenant_id=str(tenant.id))

        to_dispatch = []
        own_address = (integration.mailbox_address or "").lower()

        for msg in messages:
            # Never process mail sent by the connected mailbox itself —
            # with message-level dedup our own replies would otherwise loop.
            if own_address and (msg["sender_email"] or "").lower() == own_address:
                continue

            # Message-level dedup (a follow-up in a known thread is NEW work —
            # the old thread-level skip silently ignored customer replies)
            existing = await db.execute(
                select(EmailThread.id).where(
                    EmailThread.gmail_message_id == msg["message_id"],
                    EmailThread.tenant_id == tenant.id,
                )
            )
            if existing.scalar_one_or_none():
                continue

            # Cap check per message
            if tenant.monthly_email_count >= cap:
                break

            # Save inside a savepoint: the DB unique constraint
            # (tenant_id, gmail_message_id) is the race-proof backstop.
            thread = EmailThread(
                tenant_id=tenant.id,
                # The mailbox this arrived through, recorded now rather than
                # guessed at send time (ADR-002).
                integration_id=integration.id,
                gmail_thread_id=msg["thread_id"],
                gmail_message_id=msg["message_id"],
                rfc_message_id=msg.get("rfc_message_id"),
                rfc_references=msg.get("rfc_references"),
                sender_email=msg["sender_email"],
                sender_name=msg["sender_name"],
                subject=msg["subject"],
                body_text=msg["body_text"],
                received_at=msg["received_at"],
                status="processing",
            )
            try:
                async with db.begin_nested():
                    db.add(thread)
                    await db.flush()
            except IntegrityError:
                # A concurrent poller inserted the same message first
                continue

            tenant.monthly_email_count += 1
            to_dispatch.append(str(thread.id))

        # The cap was not reached when this poll began; if it is now, this
        # cycle crossed it. Capture plain values before commit (see the
        # MissingGreenlet note in _process_single_email_async).
        cap_notice = None
        if tenant.monthly_email_count >= cap:
            cap_notice = {
                "tenant_id": str(tenant.id),
                "to_email": tenant.email,
                "business_name": tenant.name,
                "plan": tenant.plan,
                "cap": cap,
            }

        await db.commit()

    # Dispatch AFTER commit — otherwise the worker can pick a task up
    # before the thread row is visible and fail with "not found".
    for thread_id in to_dispatch:
        try:
            process_single_email.delay(tenant_id, thread_id, integration_id)
        except Exception as e:
            # Broker full (noeviction) or unreachable. The row is committed as
            # `processing`, so the sweeper will re-dispatch it; keep going so
            # one publish failure strands neither the rest of the batch nor
            # the cap notice below.
            log.error("dispatch_failed", thread_id=thread_id, error=str(e))
            sentry_sdk.capture_exception(e)

    # Tell the owner ONCE, with the right message. Before this existed, the
    # only signal a capped tenant ever received was the staleness alert
    # claiming their inbox connection had died.
    if cap_notice:
        try:
            message_id = await asyncio.to_thread(
                send_email,
                to_email=cap_notice["to_email"],
                subject="AccountFlow: you've reached this month's email limit",
                body=(
                    f"Hi {cap_notice['business_name']},\n\n"
                    f"AccountFlow has processed {cap_notice['cap']} emails for you this "
                    f"month, which is the limit of your {cap_notice['plan'].title()} plan.\n\n"
                    f"Your mailbox connection is fine and nothing has been lost — but new "
                    f"customer emails will NOT be drafted or actioned until the 1st of next "
                    f"month, so please handle the inbox manually until then.\n\n"
                    f"To lift the limit now, reply to this email and we'll move you to a "
                    f"larger plan.\n\n"
                    f"— AccountFlow"
                ),
            )
        except Exception as e:
            message_id = None
            sentry_sdk.capture_exception(e)
        # send_email swallows SendGrid errors and returns None, so the return
        # value — not the absence of an exception — is the success signal.
        if message_id:
            log.info("monthly_cap_email_sent", tenant_id=cap_notice["tenant_id"])
        else:
            log.error("monthly_cap_email_failed", tenant_id=cap_notice["tenant_id"])


@celery_app.task(
    name="app.worker.tasks.process_single_email",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def process_single_email(self: Task, tenant_id: str, thread_id: str, integration_id: str):
    """Run Claude classification + action for one email thread."""
    try:
        return run_async(
            _process_single_email_async(tenant_id, thread_id, integration_id),
            tenant_id=tenant_id,
        )
    except Exception as exc:
        log.error("process_single_email_failed", thread_id=thread_id, error=str(exc))
        sentry_sdk.capture_exception(exc)
        if self.request.retries >= self.max_retries:
            # Out of retries — record the failure so the dashboard and the
            # hourly error-rate alert can see it, then surface the error.
            run_async(
                _mark_thread_failed(tenant_id, thread_id, str(exc)), tenant_id=tenant_id
            )
            raise
        raise self.retry(exc=exc)


async def _mark_thread_failed(tenant_id: str, thread_id: str, error: str):
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.email_thread import EmailThread

    _warn_if_unscoped("mark_thread_failed")

    async with AsyncSessionLocal() as db:
        # RLS would filter this too, but the explicit predicate is the
        # invariant (CLAUDE.md 1) and this is a blind UPDATE by id: without it,
        # a wrong id under a disabled backstop would fail another tenant's row.
        result = await db.execute(
            select(EmailThread).where(
                EmailThread.id == uuid.UUID(thread_id),
                EmailThread.tenant_id == uuid.UUID(tenant_id),
            )
        )
        thread = result.scalar_one_or_none()
        if thread:
            thread.status = "failed"
            thread.error_message = error[:2000]
            await db.commit()


async def _process_single_email_async(tenant_id: str, thread_id: str, integration_id: str):
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.integration import Integration
    from app.models.email_thread import EmailThread
    from app.models.draft import Draft
    from app.models.task import Task
    from app.models.tenant import Tenant
    from app.core.policy import calendar_send_updates, should_auto_send
    from app.core.security import create_review_token
    from app.services.claude import classify_and_draft
    from app.services.mail_provider import (
        check_availability,
        create_appointment,
        send_reply,
    )
    from app.services.sendgrid import send_draft_review_email
    from dateutil.parser import parse as parse_dt

    # Populated when a draft needs owner approval; the notification is sent
    # AFTER commit so the review link can never point at an uncommitted row.
    notify: Optional[dict] = None

    _warn_if_unscoped("process_single_email")

    async with AsyncSessionLocal() as db:
        # Row lock, held until commit. A duplicate dispatch — the sweeper
        # racing the original at --concurrency >= 2 — blocks here, then reads
        # the terminal status the first run committed and returns below.
        # Without the lock the status check is read-then-act and only holds
        # because production happens to run a single worker slot.
        # Both predicates are explicit as well as RLS-enforced: the backstop
        # is meant to back these up, not to replace them (CLAUDE.md 1, ADR-001).
        thread_result = await db.execute(
            select(EmailThread)
            .where(
                EmailThread.id == uuid.UUID(thread_id),
                EmailThread.tenant_id == uuid.UUID(tenant_id),
            )
            .with_for_update()
        )
        thread = thread_result.scalar_one_or_none()

        integration_result = await db.execute(
            select(Integration).where(
                Integration.id == uuid.UUID(integration_id),
                Integration.tenant_id == uuid.UUID(tenant_id),
            )
        )
        integration = integration_result.scalar_one_or_none()

        if not thread or not integration:
            log.warning("thread_or_integration_missing", thread_id=thread_id)
            return

        if thread.status != "processing":
            # Already handled — a sweeper re-dispatch raced the original task,
            # or a retry arrived after the commit. Never classify twice.
            log.info("thread_already_handled", thread_id=thread_id, status=thread.status)
            return

        if integration.tenant_id != thread.tenant_id:
            # The pairing is correct by construction today; a stale or replayed
            # queue message is the only way here. Never send from another
            # tenant's mailbox or show their prompt this email.
            thread.status = "failed"
            thread.error_message = "Integration does not belong to this thread's tenant."
            await db.commit()
            log.error(
                "thread_integration_tenant_mismatch",
                thread_id=thread_id,
                tenant_id=str(thread.tenant_id),
                integration_id=integration_id,
            )
            return

        tenant_result = await db.execute(
            select(Tenant).where(Tenant.id == thread.tenant_id)
        )
        tenant = tenant_result.scalar_one_or_none()
        if not tenant:
            log.warning("tenant_missing", thread_id=thread_id)
            return

        if not tenant.is_active:
            # Suspended or offboarded between ingest and processing: their
            # customers' mail must not reach Claude.
            thread.status = "failed"
            thread.error_message = "Tenant inactive at processing time."
            await db.commit()
            log.info("thread_skipped_tenant_inactive", thread_id=thread_id, tenant_id=str(tenant.id))
            return

        # --- AI Classification ---
        ai_result = await classify_and_draft(
            sender_email=thread.sender_email,
            sender_name=thread.sender_name or "",
            subject=thread.subject or "",
            body=thread.body_text or "",
            business_name=tenant.name,
            custom_system_prompt=integration.system_prompt,
        )

        # Persist AI result on thread (incl. token usage for unit economics)
        thread.ai_action = ai_result["action"]
        thread.ai_confidence = ai_result["confidence"]
        thread.ai_model_used = ai_result["model_used"]
        thread.ai_input_tokens = ai_result.get("input_tokens")
        thread.ai_output_tokens = ai_result.get("output_tokens")
        thread.processed_at = datetime.now(timezone.utc)

        action = ai_result["action"]
        confidence = ai_result["confidence"]

        # ── Action: Draft Reply ────────────────────────────────────────────────
        if action == "draft_reply" and ai_result.get("draft_body"):
            # Auto-send is OPT-IN per tenant (pilot-safe: default off, so
            # every draft is human-reviewed until the owner enables it).
            auto_send = should_auto_send(
                tenant.auto_send_enabled, tenant.auto_send_threshold, confidence
            )
            draft_status = "auto_sent" if auto_send else "pending_review"

            draft = Draft(
                tenant_id=tenant.id,
                thread_id=thread.id,
                # Fixed at draft time so approval cannot choose a different
                # mailbox than the one the email arrived on (ADR-002).
                integration_id=thread.integration_id,
                to_email=thread.sender_email,
                subject=f"Re: {thread.subject or ''}",
                body=ai_result["draft_body"],
                status=draft_status,
                ai_confidence=confidence,
            )
            db.add(draft)
            await db.flush()

            if auto_send:
                # send_reply fetches its own credentials — no need to pre-load
                msg_id = await send_reply(
                    db=db,
                    integration=integration,
                    thread_id=thread.gmail_thread_id,
                    # Graph replies are an operation on the original message,
                    # so the provider id is required; Gmail ignores it.
                    message_id=thread.gmail_message_id,
                    to_email=thread.sender_email,
                    subject=thread.subject or "",
                    body=ai_result["draft_body"],
                    in_reply_to=thread.rfc_message_id,
                    references=thread.rfc_references,
                )
                if msg_id:
                    draft.sent_message_id = msg_id
                    draft.sent_at = datetime.now(timezone.utc)
                else:
                    draft.status = "pending_review"
                    log.warning("auto_send_failed_fallback_to_review", draft_id=str(draft.id))

            # Read the status AFTER the auto-send attempt: a failed send falls
            # back to pending_review and must still notify the owner.
            if draft.status == "pending_review":
                # Capture plain values now. Once the session commits, touching
                # an expired ORM attribute triggers a lazy refresh, which
                # raises MissingGreenlet in async context.
                notify = {
                    "to_email": tenant.email,
                    "business_name": tenant.name,
                    "customer_email": thread.sender_email or "",
                    "subject": thread.subject or "(no subject)",
                    "draft_body": ai_result["draft_body"],
                    "review_url": (
                        f"{settings.app_base_url.rstrip('/')}"
                        f"/api/review/{create_review_token(str(tenant.id), str(draft.id))}"
                    ),
                }

            thread.status = "actioned"

        # ── Action: Create Appointment ─────────────────────────────────────────
        elif action == "create_appointment":
            appt = ai_result.get("appointment_details") or {}
            task = Task(
                tenant_id=tenant.id,
                thread_id=thread.id,
                title=appt.get("title", f"Appointment — {thread.sender_name or thread.sender_email}"),
                description=appt.get("notes", ""),
                status="open",
            )

            # Try to parse a proposed datetime and create a calendar event
            proposed_dt_str = appt.get("proposed_datetime")
            slot_conflict = False
            if proposed_dt_str:
                try:
                    proposed_dt = parse_dt(proposed_dt_str)
                    duration = appt.get("duration_minutes") or 60

                    # Never book over an existing event. check_availability
                    # fails closed, so an API error also lands here and the
                    # owner books by hand rather than the AI double-booking.
                    slot_free = await check_availability(
                        db=db,
                        integration=integration,
                        start_datetime=proposed_dt,
                        end_datetime=proposed_dt + timedelta(minutes=duration),
                    )

                    if slot_free:
                        event_id = await create_appointment(
                            db=db,
                            integration=integration,
                            title=task.title,
                            start_datetime=proposed_dt,
                            duration_minutes=duration,
                            attendee_email=thread.sender_email,
                            notes=appt.get("notes"),
                            send_updates=calendar_send_updates(tenant.auto_send_enabled),
                        )
                        if event_id:
                            task.google_event_id = event_id
                    else:
                        slot_conflict = True
                        log.info(
                            "calendar_slot_unavailable",
                            thread_id=thread_id,
                            requested=proposed_dt_str,
                        )
                        task.title = f"Clash — {task.title}"
                        task.description = (
                            f"The customer asked for {proposed_dt_str}, but that slot "
                            f"is not free. Offer them another time.\n\n"
                            f"{task.description or ''}"
                        ).strip()
                except Exception as e:
                    log.warning("calendar_create_skipped", reason=str(e))

            db.add(task)
            # A clash needs a human, so don't mark the thread as handled.
            thread.status = "flagged" if slot_conflict else "actioned"

        # ── Action: Create Task ────────────────────────────────────────────────
        elif action == "create_task":
            task_details = ai_result.get("task_details") or {}
            task = Task(
                tenant_id=tenant.id,
                thread_id=thread.id,
                title=task_details.get("title", f"Follow-up: {thread.subject or 'Email'}"),
                description=task_details.get("description", ""),
                status="open",
            )
            if task_details.get("due_date"):
                try:
                    task.due_date = parse_dt(task_details["due_date"])
                except Exception:
                    pass
            db.add(task)
            thread.status = "actioned"

        # ── Action: Flag Human ─────────────────────────────────────────────────
        elif action == "flag_human":
            thread.status = "flagged"

        # ── Action: Ignore ─────────────────────────────────────────────────────
        elif action == "ignore":
            thread.status = "ignored"

        else:
            thread.status = "flagged"

        # Read before commit — afterwards this attribute may be expired, and
        # refreshing it would lazy-load inside an async session.
        final_status = thread.status
        await db.commit()
        log.info(
            "email_processed",
            thread_id=thread_id,
            action=action,
            confidence=confidence,
            status=final_status,
        )

    # Notify the owner outside the session, once the draft is durably stored.
    # A failure here must not lose the draft — it is safely in the database and
    # still reachable from the dashboard, so we log and move on.
    if notify:
        try:
            await asyncio.to_thread(send_draft_review_email, **notify)
            log.info("draft_review_email_sent", thread_id=thread_id)
        except Exception as e:
            log.error("draft_review_email_failed", thread_id=thread_id, error=str(e))
            sentry_sdk.capture_exception(e)


@celery_app.task(name="app.worker.tasks.reset_monthly_email_counts")
def reset_monthly_email_counts():
    """Reset all tenants' monthly_email_count on the 1st of each month."""
    return run_async(_reset_monthly_counts_async())


async def _reset_monthly_counts_async():
    """Scheduled reset. The poller also resets lazily (policy.
    monthly_period_needs_reset), so a missed Beat run no longer strands a
    capped tenant; this stays as the prompt path on the 1st."""
    from app.database import AsyncSessionLocal
    from app.models.tenant import Tenant
    from sqlalchemy import update

    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Tenant).values(
                monthly_email_count=0,
                monthly_reset_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()
    log.info("monthly_email_counts_reset")


@celery_app.task(name="app.worker.tasks.sweep_stuck_threads")
def sweep_stuck_threads():
    """Every 10 minutes: re-dispatch or fail threads stuck in `processing`."""
    return run_async(_sweep_stuck_threads_async())


async def _sweep_stuck_threads_async():
    """A thread is committed as `processing` before its task is queued. If the
    queue message is lost — broker eviction, worker killed before ack — nothing
    else ever touches the row, and the error-rate alert only counts `failed`,
    so the patient's email vanishes while the product reports itself healthy.
    This is the only path that turns that silent drop into a visible one.
    """
    from sqlalchemy import select, update
    from app.core.policy import (
        STUCK_REDISPATCH_AFTER,
        STUCK_SWEEP_BATCH,
        stuck_thread_action,
    )
    from app.database import AsyncSessionLocal, apply_tenant_context, tenant_scope
    from app.models.email_thread import EmailThread
    from app.models.tenant import Tenant

    now = datetime.now(timezone.utc)
    # (tenant_id, thread_id, integration_id) — the same triple the thread was
    # created with, so a re-dispatch is indistinguishable from the original.
    to_redispatch: list[tuple[str, str, str]] = []
    failed = 0
    found = 0

    async with AsyncSessionLocal() as db:

        async def _fail(thread_id, reason):
            nonlocal failed
            # Conditional on status so a thread that completed between the
            # select and this update keeps its real outcome.
            await db.execute(
                update(EmailThread)
                .where(EmailThread.id == thread_id, EmailThread.status == "processing")
                .values(status="failed", error_message=reason)
            )
            failed += 1

        # Cross-tenant, so it reads `tenants` (not under RLS) and then works
        # inside each tenant's context. A single unscoped query over
        # email_threads would match zero rows under RLS and report a clean
        # sweep having examined nothing — the exact silent success this task
        # exists to prevent.
        tenants = (await db.execute(select(Tenant.id, Tenant.is_active))).all()

        for tenant_id, tenant_active in tenants:
            try:
                with tenant_scope(tenant_id):
                    await apply_tenant_context(db)

                    # Columns only, bounded, oldest first: after an outage
                    # there may be thousands of these, each carrying a full
                    # patient email in body_text, and this runs inside the
                    # 768 MB worker next to the Claude call. Per tenant, so
                    # one noisy tenant cannot crowd the others out of a sweep.
                    rows = (
                        await db.execute(
                            select(
                                EmailThread.id,
                                EmailThread.created_at,
                                EmailThread.integration_id,
                            )
                            .where(
                                EmailThread.tenant_id == tenant_id,
                                EmailThread.status == "processing",
                                EmailThread.created_at <= now - STUCK_REDISPATCH_AFTER,
                            )
                            .order_by(EmailThread.created_at)
                            .limit(STUCK_SWEEP_BATCH)
                        )
                    ).all()
                    found += len(rows)

                    for thread_id, created_at, integration_id in rows:
                        if not tenant_active:
                            await _fail(thread_id, "Tenant inactive; not processed.")
                            log.warning(
                                "stuck_thread_failed_tenant_inactive",
                                thread_id=str(thread_id),
                                tenant_id=str(tenant_id),
                            )
                            continue

                        action = stuck_thread_action(created_at, now)
                        if action == "fail":
                            await _fail(
                                thread_id,
                                "Stuck in processing for over an hour; no worker "
                                "completed it.",
                            )
                            log.warning(
                                "stuck_thread_failed",
                                thread_id=str(thread_id),
                                tenant_id=str(tenant_id),
                            )
                            continue
                        if action != "redispatch":
                            continue

                        # The mailbox is on the row (ADR-002): no guessing, and
                        # a tenant with two mailboxes is no longer failed for it.
                        to_redispatch.append(
                            (str(tenant_id), str(thread_id), str(integration_id))
                        )
                        log.warning(
                            "stuck_thread_redispatched",
                            thread_id=str(thread_id),
                            tenant_id=str(tenant_id),
                        )

                # Committed per tenant, while still inside its own scope. One
                # shared commit at the end would mean a database error on the
                # last tenant discarding every `failed` mark already made for
                # the others AND raising before the re-dispatch loop runs —
                # losing exactly the threads this task exists to rescue.
                await db.commit()
            except Exception as e:
                # Whatever is wrong with this tenant, the others still have
                # patient emails sitting in `processing`. Roll back first: the
                # session is shared, and an aborted transaction poisons every
                # statement after it.
                await db.rollback()
                log.error("stuck_sweep_tenant_failed", tenant_id=str(tenant_id), error=str(e))
                sentry_sdk.capture_exception(e)
                continue

    for tenant_id, thread_id, integration_id in to_redispatch:
        try:
            process_single_email.delay(tenant_id, thread_id, integration_id)
        except Exception as e:
            # Broker still full or down — exactly the condition this sweep
            # exists to recover from. The thread stays `processing` and the
            # next sweep tries again; one publish failure must not end the run.
            log.error("stuck_thread_redispatch_failed", thread_id=thread_id, error=str(e))
            sentry_sdk.capture_exception(e)

    if found:
        log.info(
            "stuck_thread_sweep",
            found=found,
            redispatched=len(to_redispatch),
            failed=failed,
        )


@celery_app.task(name="app.worker.tasks.send_weekly_digest_all")
def send_weekly_digest_all():
    """Send the weekly digest to all active tenants."""
    return run_async(_send_weekly_digest_async())


async def _digest_counts(db, tenant_id, week_ago) -> dict:
    """The four numbers in one tenant's digest. Every table read here is under
    RLS, so the caller must already be inside that tenant's context."""
    from sqlalchemy import select, func
    from app.models.email_thread import EmailThread
    from app.models.draft import Draft
    from app.models.task import Task

    emails = await db.execute(
        select(func.count(EmailThread.id)).where(
            EmailThread.tenant_id == tenant_id,
            EmailThread.created_at >= week_ago,
        )
    )
    drafts_sent = await db.execute(
        select(func.count(Draft.id)).where(
            Draft.tenant_id == tenant_id,
            Draft.status.in_(["sent", "auto_sent"]),
            Draft.created_at >= week_ago,
        )
    )
    tasks_created = await db.execute(
        select(func.count(Task.id)).where(
            Task.tenant_id == tenant_id,
            Task.created_at >= week_ago,
        )
    )
    appointments = await db.execute(
        select(func.count(Task.id)).where(
            Task.tenant_id == tenant_id,
            Task.google_event_id.is_not(None),
            Task.created_at >= week_ago,
        )
    )
    return {
        "emails_processed": emails.scalar() or 0,
        "drafts_sent": drafts_sent.scalar() or 0,
        "tasks_created": tasks_created.scalar() or 0,
        "appointments_booked": appointments.scalar() or 0,
    }


async def _send_weekly_digest_async():
    from sqlalchemy import select
    from app.database import AsyncSessionLocal, apply_tenant_context, tenant_scope
    from app.models.tenant import Tenant
    from app.services.sendgrid import send_weekly_digest

    week_ago = datetime.now(timezone.utc) - timedelta(days=7)

    async with AsyncSessionLocal() as db:
        tenants_result = await db.execute(
            select(Tenant).where(Tenant.is_active == True)
        )
        tenants = tenants_result.scalars().all()

        for tenant in tenants:
            try:
                # Cross-tenant sweep: every count is on an RLS-protected table,
                # so each iteration sets its own context. Unscoped, the digest
                # would cheerfully report zeros to every customer.
                with tenant_scope(tenant.id):
                    await apply_tenant_context(db)
                    counts = await _digest_counts(db, tenant.id, week_ago)

                await asyncio.to_thread(
                    send_weekly_digest,
                    to_email=tenant.email,
                    business_name=tenant.name,
                    **counts,
                )
            except Exception as e:
                # One tenant's failure must not cost every later tenant their
                # digest — the loop is ordered, so they would all be lost. The
                # rollback matters even though this loop only reads: a failed
                # statement aborts the shared transaction, and without it every
                # remaining tenant would fail too.
                await db.rollback()
                log.error("weekly_digest_failed", tenant_id=str(tenant.id), error=str(e))
                sentry_sdk.capture_exception(e)


@celery_app.task(name="app.worker.tasks.check_tenant_error_rates")
def check_tenant_error_rates():
    """Hourly: alert tenant owners whose inbox processing is failing.

    Alert condition: >= 3 failed threads AND failure rate >= 10% over the
    last 24h. Throttled to one alert email per tenant per 24h.
    """
    return run_async(_check_tenant_error_rates_async())


async def _check_tenant_error_rates_async():
    """Hourly health sweep across every active tenant.

    Two distinct failure modes, because they look nothing alike in the data:

      1. Emails arrive but processing fails  → failed/total ratio.
      2. The inbox connection itself is dead → no emails arrive at all, so
         mode 1 can never fire (0/0). This is the dangerous one: a revoked
         OAuth grant looks exactly like a quiet week, and historically the
         first thing anyone noticed was a customer asking why the AI had
         stopped replying.
    """
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models.tenant import Tenant
    from app.services.sendgrid import send_email

    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)

    async with AsyncSessionLocal() as db:
        tenants_result = await db.execute(select(Tenant).where(Tenant.is_active == True))  # noqa: E712
        tenants = tenants_result.scalars().all()

        for tenant in tenants:
            # Throttle: max one alert per tenant per 24h
            if tenant.last_error_alert_at and tenant.last_error_alert_at > day_ago:
                continue

            try:
                subject, body = await _tenant_health_alert(db, tenant, now, day_ago)
                if subject is None:
                    continue

                await asyncio.to_thread(
                    send_email, to_email=tenant.email, subject=subject, body=body
                )
                tenant.last_error_alert_at = now
                # Committed per tenant, immediately after the email goes out.
                # Deferring to one commit at the end meant a later tenant's
                # database error rolled back the throttle stamps of everyone
                # already emailed — so the next hourly run emailed them all
                # again, and again, for as long as the error persisted.
                await db.commit()
            except Exception as e:
                # One tenant's failure must not silence the alert for every
                # tenant after it in the loop — those are the ones whose
                # inboxes may actually be broken.
                await db.rollback()
                log.error("error_rate_check_failed", tenant_id=str(tenant.id), error=str(e))
                sentry_sdk.capture_exception(e)


async def _tenant_health_alert(db, tenant, now, day_ago):
    """(subject, body) for one tenant's health alert, or (None, None).

    Both queries here are on RLS-protected tables, so they run inside this
    tenant's context; unscoped they would return nothing and every tenant
    would look perfectly healthy with zero of everything.
    """
    from sqlalchemy import func, select

    from app.core.policy import poll_is_stale
    from app.database import apply_tenant_context, tenant_scope
    from app.models.email_thread import EmailThread
    from app.models.integration import Integration

    with tenant_scope(tenant.id):
        await apply_tenant_context(db)

        # ── Mode 2: is the mailbox connection itself broken? ──────────────
        integrations = (
            (
                await db.execute(
                    select(Integration).where(
                        Integration.tenant_id == tenant.id,
                        Integration.is_active == True,  # noqa: E712
                    )
                )
            )
            .scalars()
            .all()
        )
        broken = [
            i for i in integrations if i.last_poll_error or poll_is_stale(i.last_poll_at, now)
        ]

        # ── Mode 1: mail arrives, but processing keeps failing ────────────
        counts = await db.execute(
            select(
                func.count(EmailThread.id).filter(EmailThread.status == "failed"),
                func.count(EmailThread.id),
            ).where(
                EmailThread.tenant_id == tenant.id,
                EmailThread.created_at >= day_ago,
            )
        )
        failed, total = counts.one()

    if broken:
        mailbox = broken[0].mailbox_address or "your mailbox"
        log.warning(
            "tenant_integration_unhealthy",
            tenant_id=str(tenant.id),
            error=broken[0].last_poll_error,
        )
        return (
            "AccountFlow: action needed — we can't read your inbox",
            f"Hi {tenant.name},\n\n"
            f"AccountFlow has stopped being able to check {mailbox}.\n\n"
            f"New customer emails are NOT being answered right now. Your "
            f"mail itself is untouched and nothing has been lost — but "
            f"please treat that inbox manually until this is fixed.\n\n"
            f"This usually means the mailbox connection expired or was "
            f"revoked. Reconnecting it from your settings normally "
            f"resolves it. We've been alerted too.\n\n"
            f"— AccountFlow",
        )

    if total and failed >= 3 and (failed / total) >= 0.10:
        log.warning(
            "tenant_error_rate_alert", tenant_id=str(tenant.id), failed=failed, total=total
        )
        return (
            "AccountFlow: some of your emails need attention",
            f"Hi {tenant.name},\n\n"
            f"In the last 24 hours, {failed} of {total} incoming emails "
            f"could not be processed automatically.\n\n"
            f"They are safe — nothing was lost — but the AI could not "
            f"handle them, so please review your inbox for anything "
            f"urgent. Our team has been notified and is investigating.\n\n"
            f"— AccountFlow",
        )

    return None, None
