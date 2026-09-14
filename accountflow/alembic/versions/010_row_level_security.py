"""Row-level security as the tenant-isolation backstop (ADR-001).

FORCE RLS on the four tables that hold customer data — integrations,
email_threads, drafts, tasks — each with one policy for every command:

    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid

current_setting(..., true) returns NULL when the variable was never set in the
session and '' once a transaction-local value has expired, so the NULLIF is
what keeps the unset case at "zero rows" instead of "invalid input syntax for
type uuid" on a reused pooled connection. The application sets the variable
per transaction with set_config('app.tenant_id', <id>, true) — see
app/database.py.

`tenants` is deliberately NOT covered: login looks a tenant up by email before
any context can exist (api/routes/auth.py), the admin routes list it, and the
cross-tenant sweeps read it first to learn which contexts to set. It holds no
patient data.

Roles. The Postgres image's `accountflow` user is a superuser and owns every
table; superusers ignore RLS and owners ignore plain ENABLE, so policies alone
would be a no-op twice over. This migration therefore creates the runtime role
the API and worker connect as — non-superuser, non-owner, DML grants only —
taking its name and password from DATABASE_URL, while Alembic itself keeps
running as the owner through MIGRATION_DATABASE_URL (alembic/env.py). It
refuses to run when both DSNs name the same role, because that is exactly the
silent no-op configuration. Postgres only logs the CREATE ROLE statement (and
so the password) if log_statement is 'ddl' or 'all'; the image default is
'none'.

downgrade() removes the policies and disables RLS but leaves the role and its
grants in place: they are harmless without RLS, the app may still be connected
as that role, and a role with open sessions cannot be dropped anyway.

Revision ID: 010
Revises: 009
"""
import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.engine import make_url

from app.config import get_settings

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None

RLS_TABLES = ("integrations", "email_threads", "drafts", "tasks")
# Everything the runtime role touches. alembic_version is deliberately absent.
GRANT_TABLES = ("tenants",) + RLS_TABLES
POLICY = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _runtime_role() -> tuple[str, str]:
    url = make_url(get_settings().database_url)
    if not url.username:
        raise RuntimeError("DATABASE_URL carries no username, so the runtime role is unknown")
    return url.username, url.password or ""


def _refuse_offline() -> None:
    """Offline mode would emit a script with no grants and no policies.

    This migration inspects the live catalogue (who am I, does the role exist,
    is it a superuser) and issues its DDL through the driver, so `alembic
    upgrade --sql` cannot render it. Failing loudly matters more than usual
    here: a silently RLS-free script is one a reviewer would approve.
    """
    if context.is_offline_mode():
        raise RuntimeError(
            "Migration 010 cannot run in --sql (offline) mode: it must inspect the "
            "server to create and check the runtime role. Run `alembic upgrade head` "
            "against the database instead."
        )


def upgrade() -> None:
    _refuse_offline()
    conn = op.get_bind()
    role, password = _runtime_role()

    current_user = conn.execute(sa.text("SELECT current_user")).scalar()
    if role == current_user:
        raise RuntimeError(
            f"DATABASE_URL and the migration connection both use role {role!r}. "
            "The table owner bypasses row-level security, so the runtime role must "
            "be a different, non-owner role: point DATABASE_URL at the accountflow_app "
            "DSN and MIGRATION_DATABASE_URL at the owner DSN (see .env.example), "
            "then re-run `alembic upgrade head`."
        )

    existing = conn.execute(
        sa.text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :role"),
        {"role": role},
    ).first()
    if existing is None:
        if not password:
            raise RuntimeError(
                f"Role {role!r} does not exist and DATABASE_URL has no password to create it with"
            )
        # Identifiers and role passwords cannot be bound parameters, hence the
        # hand quoting; exec_driver_sql so neither ':' nor '%' in the password
        # is mistaken for a placeholder.
        conn.exec_driver_sql(
            f"CREATE ROLE {_quote_ident(role)} LOGIN PASSWORD {_quote_literal(password)}"
        )
    elif existing.rolsuper or existing.rolbypassrls:
        raise RuntimeError(
            f"Role {role!r} is a superuser or has BYPASSRLS; row-level security would not apply to it"
        )

    quoted = _quote_ident(role)
    conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {quoted}")
    for table in GRANT_TABLES:
        conn.exec_driver_sql(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {quoted}")
    # Tables created by later migrations (run as the owner) inherit the grants.
    conn.exec_driver_sql(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {quoted}"
    )

    for table in RLS_TABLES:
        conn.exec_driver_sql(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        conn.exec_driver_sql(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        conn.exec_driver_sql(
            f"CREATE POLICY tenant_isolation ON {table} FOR ALL "
            f"USING ({POLICY}) WITH CHECK ({POLICY})"
        )


def downgrade() -> None:
    _refuse_offline()
    conn = op.get_bind()
    for table in reversed(RLS_TABLES):
        conn.exec_driver_sql(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        conn.exec_driver_sql(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        conn.exec_driver_sql(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
