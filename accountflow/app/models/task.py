import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_threads.id", ondelete="CASCADE"), nullable=True
    )

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)

    # "open" | "in_progress" | "completed" | "cancelled"
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="open", index=True
    )

    due_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    assigned_to: Mapped[str] = mapped_column(String(255), nullable=True)

    # If this task triggered a calendar event
    google_event_id: Mapped[str] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship(back_populates="tasks")  # noqa: F821
    thread: Mapped["EmailThread"] = relationship(back_populates="tasks")  # noqa: F821

    def __repr__(self) -> str:
        return f"<Task '{self.title[:40]}' status={self.status}>"
