"""Integration poll health, so a silently broken inbox raises an alert.

Revision ID: 007
Revises: 006
"""
import sqlalchemy as sa
from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "integrations",
        sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "integrations",
        sa.Column("last_poll_error", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("integrations", "last_poll_error")
    op.drop_column("integrations", "last_poll_at")
