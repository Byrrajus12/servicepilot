from uuid import UUID

from sqlalchemy import ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from servicepilot.db.base import Base, TenantOwned


class Vehicle(TenantOwned, Base):
    __tablename__ = "vehicles"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
        UniqueConstraint("tenant_id", "vin"),
        UniqueConstraint("tenant_id", "customer_id", "id"),
    )

    customer_id: Mapped[UUID]
    vin: Mapped[str] = mapped_column(String(17))
    make: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(80))
    model_year: Mapped[int]
