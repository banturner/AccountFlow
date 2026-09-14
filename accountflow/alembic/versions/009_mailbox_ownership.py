"""Mailbox ownership: a thread and a draft belong to exactly one integration.

Until now the mailbox was reconstructed at send time — approve_and_send picked
ANY active integration for the tenant — so a Graph-sourced draft could be
handed to the Gmail sender with a Graph conversationId as its threadId.
Recording integration_id on the row makes the mailbox part of the data, and
lets row-level security (010) turn a stale (thread, integration) pairing into
zero rows instead of a reply from the wrong mailbox. See ADR-002.

Two renames while the tables are open, because the old names lied:
integrations.gmail_address has held the Microsoft mailbox since the Graph
provider landed (-> mailbox_address), and drafts.sendgrid_message_id has only
ever stored a Gmail / Graph id (-> sent_message_id).

Revision ID: 009
Revises: 008
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("integrations", "gmail_address", new_column_name="mailbox_address")
    op.alter_column("drafts", "sendgrid_message_id", new_column_name="sent_message_id")

    # ON DELETE is the default NO ACTION on purpose: an integration that has
    # history must not be deletable out from under it, and disconnecting a
    # mailbox is is_active=false, not a DELETE. Consequence to remember when
    # the 12-month purge job is built: rows must be removed children-first
    # (drafts and tasks, then email_threads, then integrations), because a
    # DELETE that removes an integration while its threads still exist is
    # rejected rather than cascaded.
    op.add_column(
        "email_threads",
        sa.Column(
            "integration_id",
            UUID(as_uuid=True),
            sa.ForeignKey("integrations.id", name="fk_email_threads_integration_id"),
            nullable=True,
        ),
    )
    op.add_column(
        "drafts",
        sa.Column(
            "integration_id",
            UUID(as_uuid=True),
            sa.ForeignKey("integrations.id", name="fk_drafts_integration_id"),
            nullable=True,
        ),
    )

    # Backfill. Rows created before this migration never recorded their
    # mailbox; the only honest guess is the one approve_and_send has been
    # making all along (the tenant's most recently updated active integration),
    # so this is no worse than the status quo. In practice no live tenant has
    # been connected, so there is nothing to backfill.
    op.execute(
        """
        UPDATE email_threads t
        SET integration_id = (
            SELECT i.id FROM integrations i
            WHERE i.tenant_id = t.tenant_id
            ORDER BY i.is_active DESC, i.updated_at DESC
            LIMIT 1
        )
        WHERE t.integration_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE drafts d
        SET integration_id = t.integration_id
        FROM email_threads t
        WHERE d.thread_id = t.id AND d.integration_id IS NULL
        """
    )
    # A thread whose tenant has no integration at all cannot be backfilled and
    # SET NOT NULL refuses — deliberately. It must be resolved by hand rather
    # than silently pointed at a mailbox it never came from.
    op.alter_column("email_threads", "integration_id", nullable=False)
    op.alter_column("drafts", "integration_id", nullable=False)
    op.create_index("ix_email_threads_integration_id", "email_threads", ["integration_id"])
    op.create_index("ix_drafts_integration_id", "drafts", ["integration_id"])


def downgrade() -> None:
    op.drop_index("ix_drafts_integration_id", "drafts")
    op.drop_index("ix_email_threads_integration_id", "email_threads")
    op.drop_column("drafts", "integration_id")
    op.drop_column("email_threads", "integration_id")
    op.alter_column("drafts", "sent_message_id", new_column_name="sendgrid_message_id")
    op.alter_column("integrations", "mailbox_address", new_column_name="gmail_address")
