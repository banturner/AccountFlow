"""Row-level security, against a real PostgreSQL (ADR-001).

Every assertion here runs as the `accountflow_app` role through the
application's own engine and session factory, so it exercises the real
`after_begin` listener rather than a test double. The `rls_enforced` fixture
refuses to let these pass for the wrong reason: if the app role turned out to
be the table owner, or to hold BYPASSRLS, or a table were not FORCE-enabled,
the suite fails instead of quietly proving nothing.

Skipped when no database is reachable; mandatory in CI (REQUIRE_POSTGRES=1).
"""
import uuid

import pytest
from sqlalchemy import func, select, text

from app.database import (
    AsyncSessionLocal,
    apply_tenant_context,
    bind_tenant,
    tenant_scope,
)
from app.models.draft import Draft
from app.models.email_thread import EmailThread
from app.models.integration import Integration
from app.models.task import Task
from app.models.tenant import Tenant
from tests.conftest import RLS_TABLES, seed_tenant

pytestmark = pytest.mark.asyncio

# The four protected models, in the order migration 010 lists their tables.
PROTECTED = (Integration, EmailThread, Draft, Task)


async def _count(db, model):
    return await db.scalar(select(func.count()).select_from(model))


async def test_no_context_means_no_rows(two_tenants):
    """The default is fail-closed: forget the context and you get nothing.

    This is the whole point of the design. A query that loses its WHERE clause
    returns zero rows instead of every clinic's mail.
    """
    async with AsyncSessionLocal() as db:
        for model in PROTECTED:
            assert await _count(db, model) == 0, model.__tablename__


async def test_owner_can_see_the_rows_the_app_role_cannot(two_tenants, clean_db):
    """Proves the previous test is about RLS, not about an empty database."""
    async with clean_db.connect() as conn:
        for table in RLS_TABLES:
            total = await conn.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608 - fixed list
            assert total == 2, table


async def test_one_tenants_context_hides_the_other(two_tenants):
    a, b = two_tenants
    async with AsyncSessionLocal() as db:
        with tenant_scope(a.id):
            for model in PROTECTED:
                rows = (await db.execute(select(model))).scalars().all()
                assert len(rows) == 1, model.__tablename__
                assert rows[0].tenant_id == a.id, model.__tablename__


async def test_reading_the_other_tenants_row_by_id_returns_nothing(two_tenants):
    """Knowing the UUID is not enough — the id is not the credential."""
    a, b = two_tenants
    async with AsyncSessionLocal() as db:
        with tenant_scope(a.id):
            assert await db.scalar(select(Draft).where(Draft.tenant_id == b.id)) is None
            assert (
                await db.scalar(select(EmailThread).where(EmailThread.id == b.thread_id))
                is None
            )
            assert (
                await db.scalar(
                    select(Integration).where(Integration.id == b.integration_id)
                )
                is None
            )


async def test_context_does_not_leak_to_the_next_user_of_a_pooled_connection(two_tenants):
    """`set_config(..., true)` is transaction-local.

    asyncpg connections are pooled and reused across requests, so a session
    variable that outlived its transaction would hand the next request the
    previous tenant's context. Run a scoped query, then an unscoped one, and
    the second must see nothing — this is also the case that made the policy
    use NULLIF: an expired transaction-local value reads back as '' rather
    than NULL, and ''::uuid would raise instead of matching no rows.
    """
    a, _ = two_tenants
    async with AsyncSessionLocal() as db:
        with tenant_scope(a.id):
            assert await _count(db, Draft) == 1
        await db.commit()

    async with AsyncSessionLocal() as db:
        for model in PROTECTED:
            assert await _count(db, model) == 0, model.__tablename__


async def test_context_survives_a_mid_request_commit(two_tenants):
    """settings.py and draft_actions.py both commit mid-request.

    A commit ends the transaction and discards the transaction-local variable,
    so without the after_begin listener every query after the first commit
    would silently return zero rows.
    """
    a, _ = two_tenants
    async with AsyncSessionLocal() as db:
        with tenant_scope(a.id):
            assert await _count(db, Draft) == 1
            await db.commit()
            assert await _count(db, Draft) == 1
            await db.commit()
            assert await _count(db, EmailThread) == 1


async def test_rollback_does_not_strand_the_context(two_tenants):
    a, _ = two_tenants
    async with AsyncSessionLocal() as db:
        with tenant_scope(a.id):
            assert await _count(db, Draft) == 1
            await db.rollback()
            assert await _count(db, Draft) == 1


async def test_writing_a_row_for_another_tenant_is_refused(two_tenants):
    """WITH CHECK: the context constrains writes, not just reads.

    Without it, a compromised or buggy path could still plant a row in another
    clinic's data — invisible to the writer, visible to the victim.
    """
    a, b = two_tenants
    async with AsyncSessionLocal() as db:
        with tenant_scope(a.id):
            db.add(
                Task(
                    tenant_id=b.id,
                    thread_id=None,
                    title="Planted in the wrong clinic",
                    status="open",
                )
            )
            with pytest.raises(Exception) as exc:
                await db.flush()
            assert "row-level security" in str(exc.value).lower()
            await db.rollback()


async def test_a_row_cannot_be_moved_to_another_tenant(two_tenants):
    a, b = two_tenants
    async with AsyncSessionLocal() as db:
        with tenant_scope(a.id):
            task = await db.scalar(select(Task))
            assert task is not None
            task.tenant_id = b.id
            with pytest.raises(Exception) as exc:
                await db.flush()
            assert "row-level security" in str(exc.value).lower()
            await db.rollback()


async def test_deleting_another_tenants_row_affects_nothing(two_tenants):
    """A DELETE that matches no visible row is a no-op, not an error — so the
    check is that B's data is still there afterwards."""
    from sqlalchemy import delete

    a, b = two_tenants
    async with AsyncSessionLocal() as db:
        with tenant_scope(a.id):
            result = await db.execute(delete(Draft).where(Draft.tenant_id == b.id))
            assert result.rowcount == 0
            await db.commit()

    async with AsyncSessionLocal() as db:
        with tenant_scope(b.id):
            assert await _count(db, Draft) == 1


async def test_tenants_table_is_readable_without_a_context(two_tenants):
    """`tenants` is deliberately outside RLS.

    POST /api/auth/login looks a tenant up by email before any context can
    exist. If this ever starts returning nothing, every customer is locked
    out — so it is asserted rather than assumed.
    """
    a, _ = two_tenants
    async with AsyncSessionLocal() as db:
        found = await db.scalar(select(Tenant).where(Tenant.email == a.email))
        assert found is not None
        assert found.id == a.id
        assert await db.scalar(select(func.count()).select_from(Tenant)) == 2


async def test_bind_tenant_applies_to_an_already_open_transaction(two_tenants):
    """The API binds the tenant after the session has already issued a query.

    after_begin has fired by then, so bind_tenant must write the variable in
    itself — otherwise every JWT request would read zero rows.
    """
    a, _ = two_tenants
    async with AsyncSessionLocal() as db:
        # Force a transaction open before anything is bound.
        assert await _count(db, Draft) == 0
        await bind_tenant(db, a.id)
        assert await _count(db, Draft) == 1
        assert await _count(db, EmailThread) == 1


async def test_apply_tenant_context_switches_tenant_mid_transaction(two_tenants):
    """What the cross-tenant sweeps do: one session, one open transaction,
    the context changed per tenant inside the loop."""
    a, b = two_tenants
    seen = {}
    async with AsyncSessionLocal() as db:
        for tenant in (a, b):
            with tenant_scope(tenant.id):
                await apply_tenant_context(db)
                rows = (await db.execute(select(EmailThread))).scalars().all()
                seen[tenant.name] = [r.subject for r in rows]

    assert seen["Clinic A"] == ["Clinic A subject"]
    assert seen["Clinic B"] == ["Clinic B subject"]


async def test_leaving_the_scope_stops_the_next_query_seeing_anything(two_tenants):
    a, _ = two_tenants
    async with AsyncSessionLocal() as db:
        with tenant_scope(a.id):
            await apply_tenant_context(db)
            assert await _count(db, Draft) == 1
        await apply_tenant_context(db)
        assert await _count(db, Draft) == 0


async def test_a_nonexistent_tenant_context_sees_nothing(two_tenants):
    async with AsyncSessionLocal() as db:
        with tenant_scope(uuid.uuid4()):
            for model in PROTECTED:
                assert await _count(db, model) == 0, model.__tablename__


async def test_every_table_holding_a_tenant_id_is_protected(clean_db):
    """The list of protected tables must not drift behind the schema.

    Migration 010 grants the app role DML on every FUTURE table via ALTER
    DEFAULT PRIVILEGES, which is what keeps the app working as the schema
    grows — but it grants no policy. So a migration that adds a customer-data
    table and forgets `ENABLE ROW LEVEL SECURITY` produces a table that is
    readable across every tenant, and nothing else in this suite would notice:
    every other test here iterates a hardcoded four-table list.

    This test derives the list from the database instead. Any table with a
    tenant_id column must have RLS enabled, FORCEd, and at least one policy.
    """
    from sqlalchemy import text

    async with clean_db.connect() as conn:
        tables = (
            await conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c"
                    " JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " JOIN pg_attribute a ON a.attrelid = c.oid"
                    " WHERE n.nspname = 'public' AND c.relkind = 'r'"
                    "   AND a.attname = 'tenant_id' AND a.attnum > 0"
                    " ORDER BY c.relname"
                )
            )
        ).scalars().all()

        assert set(tables) == set(RLS_TABLES), (
            "a table gained or lost a tenant_id column; if it is new, give it an "
            "RLS policy in its own migration and add it to conftest.RLS_TABLES"
        )

        for table in tables:
            flags = (
                await conn.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity"
                        " FROM pg_class WHERE relname = :t"
                    ),
                    {"t": table},
                )
            ).first()
            policies = await conn.scalar(
                text("SELECT count(*) FROM pg_policies WHERE tablename = :t"), {"t": table}
            )
            assert flags.relrowsecurity, f"{table} does not have row-level security enabled"
            assert flags.relforcerowsecurity, f"{table} is not FORCEd, so its owner bypasses it"
            assert policies >= 1, f"{table} has RLS on but no policy, which denies everything"


async def test_a_new_tenant_starts_empty_and_can_write_its_own_rows(clean_db):
    """End to end through the ORM: create under a context, read back under it,
    and stay invisible to everyone else."""
    owner_engine = clean_db
    c = await seed_tenant(owner_engine, name="Clinic C", mailbox="c@clinic-c.example")
    d = await seed_tenant(owner_engine, name="Clinic D", mailbox="d@clinic-d.example")

    async with AsyncSessionLocal() as db:
        with tenant_scope(c.id):
            db.add(Task(tenant_id=c.id, thread_id=None, title="Order more gloves", status="open"))
            await db.commit()

    async with AsyncSessionLocal() as db:
        with tenant_scope(c.id):
            tasks = (await db.execute(select(Task))).scalars().all()
            assert [t.title for t in tasks] == ["Order more gloves"]
        with tenant_scope(d.id):
            assert await _count(db, Task) == 0
