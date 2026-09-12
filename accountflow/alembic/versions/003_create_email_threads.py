"""create email_threads table

Revision ID: 003
Revises: 002
Create Date: 2026-07-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_threads",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("gmail_thread_id", sa.String(255), nullable=False),
        sa.Column("gmail_message_id", sa.String(255), nullable=False),
        sa.Column("sender_email", sa.String(255), nullable=False),
        sa.Column("sender_name", sa.String(255), nullable=True),
        sa.Column("subject", sa.String(1000), nullable=True),
        sa.Column("body_text", sa.Text, nullable=True),
        sa.Column("ai_action", sa.String(50), nullable=True),
        sa.Column("ai_confidence", sa.Float, nullable=True),
        sa.Column("ai_model_used", sa.String(50), nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_email_threads_tenant_id", "email_threads", ["tenant_id"])
    op.create_index("ix_email_threads_gmail_thread_id", "email_threads", ["gmail_thread_id"])
    op.create_index("ix_email_threads_status", "email_threads", ["status"])


def downgrade() -> None:
    op.drop_index("ix_email_threads_status", "email_threads")
    op.drop_index("ix_email_threads_gmail_thread_id", "email_threads")
    op.drop_index("ix_email_threads_tenant_id", "email_threads")
    op.drop_table("email_threads")
