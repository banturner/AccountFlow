import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class EmailThread(Base):
    __tablename__ = "email_threads"
    __table_args__ = (
        # Message-level dedup: DB-enforced so concurrent pollers can't
        # double-process the same Gmail message.
        UniqueConstraint("tenant_id", "gmail_message_id", name="uq_tenant_gmail_message"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    # The mailbox this message arrived through (migration 009, ADR-002). A
    # reply is sent from THIS integration, never from "any active one".
    integration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integrations.id"), nullable=False, index=True
    )
    # Gmail thread ID — unique per tenant
    gmail_thread_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    gmail_message_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # RFC 5322 Message-ID / References headers — required so our replies
    # thread correctly in the CUSTOMER's mail client (In-Reply-To).
    rfc_message_id: Mapped[str] = mapped_column(String(998), nullable=True)
    rfc_references: Mapped[str] = mapped_column(Text, nullable=True)

    sender_email: Mapped[str] = mapped_column(String(255), nullable=False)
    sender_name: Mapped[str] = mapped_column(String(255), nullable=True)
    subject: Mapped[str] = mapped_column(String(1000), nullable=True)
    body_text: Mapped[str] = mapped_column(Text, nullable=True)

    # AI classification result
    # "draft_reply" | "create_appointment" | "create_task" | "flag_human" | "ignore"
    ai_action: Mapped[str] = mapped_column(String(50), nullable=True)
    ai_confidence: Mapped[float] = mapped_column(nullable=True)
    # "haiku" | "sonnet" — which model processed this (for cost tracking)
    ai_model_used: Mapped[str] = mapped_column(String(50), nullable=True)
    # Token usage per email — feeds unit-economics / gross-margin reporting
    ai_input_tokens: Mapped[int] = mapped_column(nullable=True)
    ai_output_tokens: Mapped[int] = mapped_column(nullable=True)

    # Processing state
    # "pending" | "processing" | "actioned" | "failed" | "ignored"
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending", index=True)
    error_message: Mapped[str] = mapped_column(Text, nullable=True)

    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship(back_populates="email_threads")  # noqa: F821
    drafts: Mapped[list["Draft"]] = relationship(back_populates="thread")  # noqa: F821
    tasks: Mapped[list["Task"]] = relationship(back_populates="thread")  # noqa: F821

    def __repr__(self) -> str:
        return f"<EmailThread {self.gmail_thread_id} status={self.status}>"
