"""The worker pipeline end to end against a real PostgreSQL.

Claude, the mail providers and SendGrid are replaced with recorders — what is
under test is the pipeline's own behaviour: that each task binds its tenant
before its first query, that a thread records the mailbox it arrived through,
and above all that a mispaired (thread, integration) sends nothing.

That last one is the assumption the architecture review called the
cross-tenant foot-gun: before ADR-001 the thread and the integration were
loaded independently by id with no tenant predicate, so a stale queue message
could have replied to clinic A's patient from clinic B's mailbox.

Skipped when no database is reachable; mandatory in CI (REQUIRE_POSTGRES=1).
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.database import AsyncSessionLocal, tenant_scope
from app.models.draft import Draft
from app.models.email_thread import EmailThread
from app.services import claude as claude_service
from app.services import mail_provider, sendgrid as sendgrid_service
from app.worker import tasks as worker
from tests.conftest import seed_draft, seed_tenant, seed_thread

pytestmark = pytest.mark.asyncio


class Recorder:
    """Collects calls instead of making them."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result

    async def acall(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result

    @property
    def called(self):
        return bool(self.calls)


def _ai(action="draft_reply", confidence=0.9, draft_body="We can see you Friday at 3pm."):
    return {
        "action": action,
        "confidence": confidence,
        "reasoning": "test",
        "draft_body": draft_body,
        "appointment_details": None,
        "task_details": None,
        "model_used": "claude-haiku-4-5",
        "input_tokens": 100,
        "output_tokens": 40,
    }


@pytest.fixture
def pipeline(monkeypatch):
    """Stub every outbound call the pipeline can make and hand back the
    recorders, so a test can assert on what would have left the building."""
    classify = Recorder(_ai())
    send_reply = Recorder("provider-message-id")
    review_email = Recorder("sendgrid-id")
    plain_email = Recorder("sendgrid-id")
    dispatch = Recorder(None)

    monkeypatch.setattr(claude_service, "classify_and_draft", classify.acall)
    monkeypatch.setattr(mail_provider, "send_reply", send_reply.acall)
    monkeypatch.setattr(sendgrid_service, "send_draft_review_email", review_email)
    monkeypatch.setattr(sendgrid_service, "send_email", plain_email)
    monkeypatch.setattr(worker.process_single_email, "delay", dispatch)
    monkeypatch.setattr(worker.process_tenant_inbox, "delay", dispatch)

    return {
        "classify": classify,
        "send_reply": send_reply,
        "review_email": review_email,
        "plain_email": plain_email,
        "dispatch": dispatch,
    }


async def _thread_status(tenant, thread_id):
    async with AsyncSessionLocal() as db:
        with tenant_scope(tenant.id):
            return await db.scalar(
                select(EmailThread.status).where(EmailThread.id == thread_id)
            )


# ── the cross-tenant foot-gun ──────────────────────────────────────────────


async def test_a_mispaired_thread_and_integration_sends_nothing(two_tenants, pipeline):
    """Clinic A's thread, clinic B's mailbox id — the stale-queue-message case.

    Under RLS the integration lookup runs in A's context and matches nothing,
    so the task stops before Claude is called and before anything can be sent.
    No draft, no reply, no notification. The thread stays `processing`, which
    is correct: the sweeper owns it from here.
    """
    a, b = two_tenants
    thread_id = a.thread_id

    async with AsyncSessionLocal() as db:
        with tenant_scope(a.id):
            thread = await db.scalar(select(EmailThread).where(EmailThread.id == thread_id))
            thread.status = "processing"
            await db.commit()

    with tenant_scope(a.id):
        await worker._process_single_email_async(
            str(a.id), str(thread_id), str(b.integration_id)
        )

    assert not pipeline["classify"].called, "a mispaired message reached Claude"
    assert not pipeline["send_reply"].called, "a mispaired message sent a reply"
    assert not pipeline["review_email"].called

    async with AsyncSessionLocal() as db:
        with tenant_scope(a.id):
            assert await db.scalar(select(func.count()).select_from(Draft).where(
                Draft.thread_id == thread_id
            )) == 0
    assert await _thread_status(a, thread_id) == "processing"


async def test_a_message_naming_the_wrong_tenant_sends_nothing(two_tenants, pipeline):
    """The other half of the same mispairing: the right integration, but the
    queue message claims it belongs to the other tenant. The thread lookup is
    the one that comes back empty this time."""
    a, b = two_tenants

    with tenant_scope(b.id):
        await worker._process_single_email_async(
            str(b.id), str(a.thread_id), str(b.integration_id)
        )

    assert not pipeline["classify"].called
    assert not pipeline["send_reply"].called


# ── happy path ─────────────────────────────────────────────────────────────


async def test_processing_records_the_mailbox_on_the_draft(two_tenants, pipeline):
    a, _ = two_tenants
    thread_id = a.thread_id
    async with AsyncSessionLocal() as db:
        with tenant_scope(a.id):
            thread = await db.scalar(select(EmailThread).where(EmailThread.id == thread_id))
            thread.status = "processing"
            await db.commit()

    with tenant_scope(a.id):
        await worker._process_single_email_async(
            str(a.id), str(thread_id), str(a.integration_id)
        )

    async with AsyncSessionLocal() as db:
        with tenant_scope(a.id):
            draft = await db.scalar(select(Draft).where(Draft.thread_id == thread_id))
            assert draft is not None
            assert draft.tenant_id == a.id
            # ADR-002: the reply's mailbox is data, not a later lookup.
            assert draft.integration_id == a.integration_id
            assert draft.status == "pending_review"

    assert await _thread_status(a, thread_id) == "actioned"
    # Auto-send is off by default, so the owner is notified rather than the
    # patient emailed.
    assert not pipeline["send_reply"].called
    assert pipeline["review_email"].called


async def test_the_review_link_carries_the_tenant(two_tenants, pipeline):
    """ADR-001: the review route has no JWT, so the token is where its tenant
    context comes from."""
    from app.core.security import verify_review_token

    a, _ = two_tenants
    thread_id = a.thread_id
    async with AsyncSessionLocal() as db:
        with tenant_scope(a.id):
            thread = await db.scalar(select(EmailThread).where(EmailThread.id == thread_id))
            thread.status = "processing"
            await db.commit()

    with tenant_scope(a.id):
        await worker._process_single_email_async(
            str(a.id), str(thread_id), str(a.integration_id)
        )

    (_, kwargs), = pipeline["review_email"].calls
    token = kwargs["review_url"].rsplit("/", 1)[-1]
    tenant_id, draft_id = verify_review_token(token)
    assert tenant_id == str(a.id)

    async with AsyncSessionLocal() as db:
        with tenant_scope(a.id):
            draft = await db.scalar(select(Draft).where(Draft.thread_id == thread_id))
            assert draft_id == str(draft.id)


async def test_an_inactive_tenants_mail_is_not_processed(clean_db, pipeline):
    owner_engine = clean_db
    t = await seed_tenant(
        owner_engine, name="Closed Clinic", mailbox="x@closed.example", is_active=False
    )
    thread_id = await seed_thread(owner_engine, t)

    with tenant_scope(t.id):
        await worker._process_single_email_async(
            str(t.id), str(thread_id), str(t.integration_id)
        )

    assert not pipeline["classify"].called
    assert await _thread_status(t, thread_id) == "failed"


# ── ingest ─────────────────────────────────────────────────────────────────


async def test_the_poller_stamps_the_mailbox_on_every_thread(clean_db, pipeline, monkeypatch):
    owner_engine = clean_db
    t = await seed_tenant(owner_engine, name="Busy Clinic", mailbox="busy@clinic.example")

    async def fake_fetch(db, integration, max_results=50):
        return [
            {
                "message_id": "m-new-1",
                "thread_id": "t-new-1",
                "sender_email": "patient@example.com",
                "sender_name": "A Patient",
                "subject": "Are you open Saturday?",
                "body_text": "Are you open Saturday?",
                "rfc_message_id": "<new-1@example.com>",
                "rfc_references": None,
                "received_at": datetime.now(timezone.utc),
            }
        ]

    monkeypatch.setattr(mail_provider, "fetch_new_messages", fake_fetch)

    with tenant_scope(t.id):
        await worker._process_tenant_inbox_async(str(t.id), str(t.integration_id))

    async with AsyncSessionLocal() as db:
        with tenant_scope(t.id):
            thread = await db.scalar(
                select(EmailThread).where(EmailThread.gmail_message_id == "m-new-1")
            )
            assert thread is not None
            assert thread.integration_id == t.integration_id
            assert thread.status == "processing"

    # Dispatched as (tenant_id, thread_id, integration_id).
    (args, _), = pipeline["dispatch"].calls
    assert args[0] == str(t.id)
    assert args[2] == str(t.integration_id)


async def test_the_poller_refuses_another_tenants_mailbox(two_tenants, pipeline, monkeypatch):
    """A queue message pairing tenant A with tenant B's integration must not
    poll anything — the integration simply is not visible in A's context."""
    a, b = two_tenants
    fetch = Recorder([])
    monkeypatch.setattr(mail_provider, "fetch_new_messages", fetch.acall)

    with tenant_scope(a.id):
        await worker._process_tenant_inbox_async(str(a.id), str(b.integration_id))

    assert not fetch.called
    assert not pipeline["dispatch"].called


async def test_the_fan_out_pairs_every_tenant_with_its_own_mailbox(two_tenants, pipeline):
    a, b = two_tenants

    await worker._poll_all_tenants_async()

    dispatched = {args[0]: args[1] for args, _ in pipeline["dispatch"].calls}
    assert dispatched == {
        str(a.id): str(a.integration_id),
        str(b.id): str(b.integration_id),
    }


# ── the stuck-thread sweeper, now per tenant ───────────────────────────────


async def test_the_sweeper_leaves_fresh_threads_redispatches_stale_and_fails_old(
    clean_db, pipeline
):
    """Three ages, three outcomes — and all of them found, which a single
    cross-tenant query could not do once RLS is on."""
    owner_engine = clean_db
    t = await seed_tenant(owner_engine, name="Sweep Clinic", mailbox="s@clinic.example")
    fresh = await seed_thread(owner_engine, t, age=timedelta(minutes=5))
    stale = await seed_thread(owner_engine, t, age=timedelta(minutes=20))
    ancient = await seed_thread(owner_engine, t, age=timedelta(minutes=70))

    await worker._sweep_stuck_threads_async()

    redispatched = [args[1] for args, _ in pipeline["dispatch"].calls]
    assert redispatched == [str(stale)]
    (args, _), = pipeline["dispatch"].calls
    assert args[0] == str(t.id)
    assert args[2] == str(t.integration_id), "re-dispatch must reuse the thread's own mailbox"

    assert await _thread_status(t, fresh) == "processing"
    assert await _thread_status(t, stale) == "processing"
    assert await _thread_status(t, ancient) == "failed"


async def test_the_sweeper_covers_every_tenant(clean_db, pipeline):
    """The regression this guards: an unscoped sweep sees zero rows under RLS
    and reports a clean run having examined nothing."""
    owner_engine = clean_db
    a = await seed_tenant(owner_engine, name="Sweep A", mailbox="a@s.example")
    b = await seed_tenant(owner_engine, name="Sweep B", mailbox="b@s.example")
    stuck_a = await seed_thread(owner_engine, a, age=timedelta(minutes=20))
    stuck_b = await seed_thread(owner_engine, b, age=timedelta(minutes=20))

    await worker._sweep_stuck_threads_async()

    pairs = {(args[0], args[1]) for args, _ in pipeline["dispatch"].calls}
    assert pairs == {(str(a.id), str(stuck_a)), (str(b.id), str(stuck_b))}


async def test_the_sweeper_fails_an_inactive_tenants_threads(clean_db, pipeline):
    owner_engine = clean_db
    t = await seed_tenant(
        owner_engine, name="Gone Clinic", mailbox="g@clinic.example", is_active=False
    )
    thread_id = await seed_thread(owner_engine, t, age=timedelta(minutes=20))

    await worker._sweep_stuck_threads_async()

    assert not pipeline["dispatch"].called
    assert await _thread_status(t, thread_id) == "failed"


async def test_a_redispatched_thread_is_not_processed_twice(two_tenants, pipeline):
    """The sweeper can re-dispatch a thread the original task then finishes.
    The second run must find the terminal status and stop before Claude."""
    a, _ = two_tenants
    thread_id = a.thread_id  # seeded as "actioned"

    with tenant_scope(a.id):
        await worker._process_single_email_async(
            str(a.id), str(thread_id), str(a.integration_id)
        )

    assert not pipeline["classify"].called
    assert not pipeline["send_reply"].called


# ── approval ───────────────────────────────────────────────────────────────


async def test_approve_sends_from_the_drafts_own_mailbox(two_tenants, pipeline):
    from app.services.draft_actions import approve_and_send

    a, _ = two_tenants
    async with AsyncSessionLocal() as db:
        with tenant_scope(a.id):
            draft = await db.scalar(
                select(Draft).options(selectinload(Draft.thread)).where(Draft.tenant_id == a.id)
            )
            await approve_and_send(db, draft)

    (_, kwargs), = pipeline["send_reply"].calls
    assert kwargs["integration"].id == a.integration_id

    async with AsyncSessionLocal() as db:
        with tenant_scope(a.id):
            draft = await db.scalar(select(Draft).where(Draft.tenant_id == a.id))
            assert draft.status == "sent"
            assert draft.sent_message_id == "provider-message-id"


async def test_approve_refuses_when_the_drafts_mailbox_belongs_elsewhere(
    two_tenants, clean_db, pipeline
):
    """A draft pointed at another tenant's integration must not fall back to
    "any active mailbox" — that fallback was the original defect."""
    from app.services.draft_actions import DraftActionError, approve_and_send

    a, b = two_tenants
    # A draft of A's that names B's mailbox: only reachable through a stale
    # row or a bad backfill, and it must refuse rather than substitute.
    bad_draft_id = await seed_draft(
        clean_db, a, a.thread_id, integration_id=b.integration_id
    )

    async with AsyncSessionLocal() as db:
        with tenant_scope(a.id):
            draft = await db.scalar(
                select(Draft).options(selectinload(Draft.thread)).where(Draft.id == bad_draft_id)
            )
            with pytest.raises(DraftActionError) as exc:
                await approve_and_send(db, draft)
            assert exc.value.status_code == 422

    assert not pipeline["send_reply"].called
