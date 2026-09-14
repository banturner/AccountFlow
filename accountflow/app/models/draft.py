import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_threads.id", ondelete="CASCADE"), nullable=False
    )
    # The mailbox the reply goes out from — the thread's integration, fixed at
    # draft time (migration 009, ADR-002) so approval cannot pick another one.
    integration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integrations.id"), nullable=False, index=True
    )

    to_email: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(1000), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    # Approval state: "pending_review" | "approved" | "rejected" | "sent" | "auto_sent"
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="pending_review", index=True
    )

    # If the owner edits the body before approving
    edited_body: Mapped[str] = mapped_column(Text, nullable=True)

    # Rejection reason (Phase 3 — feeds monthly AI tuning review)
    rejection_reason: Mapped[str] = mapped_column(Text, nullable=True)

    # Provider id (Gmail message id / Graph message id) of the sent reply — set
    # after a successful send. Misnamed sendgrid_message_id until migration 009.
    sent_message_id: Mapped[str] = mapped_column(String(255), nullable=True)

    ai_confidence: Mapped[float] = mapped_column(nullable=True)

    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship(back_populates="drafts")  # noqa: F821
    thread: Mapped["EmailThread"] = relationship(back_populates="drafts")  # noqa: F821

    def __repr__(self) -> str:
        return f"<Draft to={self.to_email} status={self.status}>"
