"""
Stores encrypted OAuth tokens for Gmail + Google Calendar.
Replaces the unsafe token.json file (P0 fix from Phase 1).
Row-level lock on token refresh prevents concurrent refresh races.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Integration(Base):
    __tablename__ = "integrations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", name="uq_tenant_provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    # e.g. "google"
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    # Address of the connected mailbox — Gmail or Microsoft 365. Written from
    # the provider's identity claims at connect time; read for the self-send
    # filter and as the From header on Gmail sends.
    mailbox_address: Mapped[str] = mapped_column(String(255), nullable=True)

    # Fernet-encrypted token JSON
    access_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_enc: Mapped[str] = mapped_column(Text, nullable=True)
    token_expiry: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    # The last Gmail historyId we successfully processed — used for incremental polling
    gmail_history_id: Mapped[str] = mapped_column(String(100), nullable=True)

    # Generic sync cursor for non-Gmail providers. Microsoft Graph hands back a
    # full delta-link URL rather than a short id, so it cannot share the column
    # above.
    sync_state: Mapped[str] = mapped_column(Text, nullable=True)

    # Custom AI system prompt for this inbox (Phase 3)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=True)

    # Poll health. Without these a broken inbox is invisible: a revoked OAuth
    # grant makes fetch_new_messages return zero messages, which is
    # indistinguishable from a quiet inbox, so no thread rows are created and
    # the failure-rate alert (which divides failures by threads) never fires.
    last_poll_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_poll_error: Mapped[str] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship(back_populates="integrations")  # noqa: F821

    def __repr__(self) -> str:
        return f"<Integration tenant={self.tenant_id} provider={self.provider} mailbox={self.mailbox_address}>"
