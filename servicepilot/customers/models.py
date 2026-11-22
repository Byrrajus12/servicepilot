from sqlalchemy import Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from servicepilot.db.base import Base, TenantOwned


class Customer(TenantOwned, Base):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        Index(None, "tenant_id", "phone_number"),
    )

    name: Mapped[str] = mapped_column(String(200))
    phone_number: Mapped[str] = mapped_column(String(16))
