from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from servicepilot.db.base import Base, Record


class Tenant(Record, Base):
    __tablename__ = "tenants"

    slug: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200))
