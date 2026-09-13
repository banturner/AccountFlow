"""
Tenant = one paying customer (a clinic, firm, etc.)
Phase 1: single-table, no FK isolation.
Phase 4: all other tables get tenant_id FK added via migration.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PlanTier(str):
    STARTER = "starter"
    GROWTH = "growth"
    PRO = "pro"
    SCALE = "scale"


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    plan: Mapped[str] = mapped_column(
        String(50), nullable=False, default="starter"
    )
    # bcrypt hash for dashboard login (JWT). Null until onboarding sets it.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=True)
    # Auto-send is OPT-IN. Pilot-safe default: every draft is human-reviewed.
    auto_send_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    auto_send_threshold: Mapped[float] = mapped_column(default=0.95, nullable=False)
    # Throttle for per-tenant error alert emails
    last_error_alert_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # How many email threads processed this calendar month
    monthly_email_count: Mapped[int] = mapped_column(default=0)
    # When the counter was LAST reset. The poller compares this to now on the
    # Singapore calendar month (policy.monthly_period_needs_reset); setting it
    # to a future date would zero the counter on every poll.
    monthly_reset_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    integrations: Mapped[list["Integration"]] = relationship(back_populates="tenant")  # noqa: F821
    email_threads: Mapped[list["EmailThread"]] = relationship(back_populates="tenant")  # noqa: F821
    drafts: Mapped[list["Draft"]] = relationship(back_populates="tenant")  # noqa: F821
    tasks: Mapped[list["Task"]] = relationship(back_populates="tenant")  # noqa: F821

    def __repr__(self) -> str:
        return f"<Tenant {self.email} plan={self.plan}>"
