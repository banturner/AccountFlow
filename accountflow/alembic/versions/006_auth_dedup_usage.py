"""Tenant auth, auto-send prefs, message-level dedup, RFC threading headers,
token-usage tracking.

Revision ID: 006
Revises: 005
"""
import sqlalchemy as sa
from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Tenant auth + automation preferences
    op.add_column("tenants", sa.Column("password_hash", sa.String(255), nullable=True))
    op.add_column(
        "tenants",
        sa.Column("auto_send_enabled", sa.Boolean, nullable=False, server_default="false"),
    )
    op.add_column(
        "tenants",
        sa.Column("auto_send_threshold", sa.Float, nullable=False, server_default="0.95"),
    )
    op.add_column(
        "tenants", sa.Column("last_error_alert_at", sa.DateTime(timezone=True), nullable=True)
    )

    # RFC threading headers + token usage on email_threads
    op.add_column("email_threads", sa.Column("rfc_message_id", sa.String(998), nullable=True))
    op.add_column("email_threads", sa.Column("rfc_references", sa.Text, nullable=True))
    op.add_column("email_threads", sa.Column("ai_input_tokens", sa.Integer, nullable=True))
    op.add_column("email_threads", sa.Column("ai_output_tokens", sa.Integer, nullable=True))

    # Message-level dedup (replaces thread-level skip logic)
    op.create_unique_constraint(
        "uq_tenant_gmail_message", "email_threads", ["tenant_id", "gmail_message_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_tenant_gmail_message", "email_threads")
    op.drop_column("email_threads", "ai_output_tokens")
    op.drop_column("email_threads", "ai_input_tokens")
    op.drop_column("email_threads", "rfc_references")
    op.drop_column("email_threads", "rfc_message_id")
    op.drop_column("tenants", "last_error_alert_at")
    op.drop_column("tenants", "auto_send_threshold")
    op.drop_column("tenants", "auto_send_enabled")
    op.drop_column("tenants", "password_hash")
