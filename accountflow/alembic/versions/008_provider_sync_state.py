"""Generic per-provider sync cursor, so Outlook can store a Graph delta link.

gmail_history_id is a short, Gmail-specific id. A Graph delta link is a full
opaque URL, so it needs its own column rather than overloading that one.

Revision ID: 008
Revises: 007
"""
import sqlalchemy as sa
from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("integrations", sa.Column("sync_state", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("integrations", "sync_state")
