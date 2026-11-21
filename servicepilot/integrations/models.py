from typing import Any

from sqlalchemy import CheckConstraint, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from servicepilot.db.base import Base, TenantOwned


class IntegrationEvent(TenantOwned, Base):
    __tablename__ = "integration_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", "external_event_id"),
        CheckConstraint(
            "processing_status IN ('received', 'processed', 'failed')", name="processing_status"
        ),
        Index(None, "tenant_id", "processing_status", "created_at"),
    )

    provider: Mapped[str] = mapped_column(String(100))
    external_event_id: Mapped[str] = mapped_column(String(255))
    processing_status: Mapped[str] = mapped_column(String(32), server_default="received")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
