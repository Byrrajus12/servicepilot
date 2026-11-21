from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from servicepilot.db.base import Base, TenantOwned


class Appointment(TenantOwned, Base):
    __tablename__ = "appointments"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id", "vehicle_id"],
            ["vehicles.tenant_id", "vehicles.customer_id", "vehicles.id"],
        ),
        UniqueConstraint("tenant_id", "external_dms_id"),
        CheckConstraint("scheduled_end > scheduled_start", name="schedule_order"),
        CheckConstraint("status IN ('scheduled', 'completed', 'cancelled')", name="status"),
        Index(None, "tenant_id", "scheduled_start"),
        Index(None, "tenant_id", "customer_id", "vehicle_id"),
    )

    customer_id: Mapped[UUID]
    vehicle_id: Mapped[UUID]
    external_dms_id: Mapped[str | None] = mapped_column(String(255))
    scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scheduled_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    service_type: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(32), server_default="scheduled")
