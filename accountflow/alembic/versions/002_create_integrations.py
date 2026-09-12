"""create integrations table

Revision ID: 002
Revises: 001
Create Date: 2026-07-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integrations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("gmail_address", sa.String(255), nullable=True),
        sa.Column("access_token_enc", sa.Text, nullable=False),
        sa.Column("refresh_token_enc", sa.Text, nullable=True),
        sa.Column("token_expiry", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gmail_history_id", sa.String(100), nullable=True),
        sa.Column("system_prompt", sa.Text, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_unique_constraint("uq_tenant_provider", "integrations", ["tenant_id", "provider"])
    op.create_index("ix_integrations_tenant_id", "integrations", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_integrations_tenant_id", "integrations")
    op.drop_constraint("uq_tenant_provider", "integrations")
    op.drop_table("integrations")
