from datetime import date, time

from sqlalchemy import CheckConstraint, Date, Integer, String, Time, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from servicepilot.db.base import Base, Record


class Tenant(Record, Base):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint("opens_at < closes_at", name="business_hours"),
        CheckConstraint(
            "cardinality(service_days) > 0 AND service_days <@ ARRAY[0,1,2,3,4,5,6]",
            name="service_days",
        ),
        CheckConstraint("appointment_minutes BETWEEN 1 AND 720", name="appointment_minutes"),
        CheckConstraint("service_capacity > 0", name="service_capacity"),
    )

    slug: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(100), server_default="America/Detroit")
    service_days: Mapped[list[int]] = mapped_column(ARRAY(Integer), server_default=text("'{0,1,2,3,4}'"))
    opens_at: Mapped[time] = mapped_column(Time, server_default="08:00")
    closes_at: Mapped[time] = mapped_column(Time, server_default="17:00")
    appointment_minutes: Mapped[int] = mapped_column(server_default="60")
    service_capacity: Mapped[int] = mapped_column(server_default="1")
    closed_dates: Mapped[list[date]] = mapped_column(ARRAY(Date), server_default="{}")
    service_catalog: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    service_policies: Mapped[dict] = mapped_column(JSONB, server_default="{}")
