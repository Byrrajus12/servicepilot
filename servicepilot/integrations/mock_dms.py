from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from servicepilot.appointments.models import Appointment
from servicepilot.customers.models import Customer
from servicepilot.tenancy.models import Tenant
from servicepilot.vehicles.models import Vehicle


class MockDMSAdapter:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def dealership(self, *, lock_schedule: bool = False) -> Tenant | None:
        statement = select(Tenant).where(Tenant.id == self.tenant_id)
        if lock_schedule:
            # The row lock stays held through the capacity check, write, and commit.
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return await self.session.scalar(statement)

    async def find_customers(self, phone_number: str) -> Sequence[Customer]:
        return (await self.session.scalars(
            select(Customer).where(
                Customer.tenant_id == self.tenant_id, Customer.phone_number == phone_number,
            ).order_by(Customer.name, Customer.id)
        )).all()

    async def customer(self, customer_id: UUID) -> Customer | None:
        return await self.session.scalar(
            select(Customer).where(Customer.tenant_id == self.tenant_id, Customer.id == customer_id)
        )

    async def vehicles(self, customer_id: UUID) -> Sequence[Vehicle]:
        return (await self.session.scalars(
            select(Vehicle).where(
                Vehicle.tenant_id == self.tenant_id, Vehicle.customer_id == customer_id,
            ).order_by(Vehicle.vin)
        )).all()

    async def vehicle(self, vehicle_id: UUID) -> Vehicle | None:
        return await self.session.scalar(
            select(Vehicle).where(Vehicle.tenant_id == self.tenant_id, Vehicle.id == vehicle_id)
        )

    def _appointments(self):
        return select(Appointment).where(Appointment.tenant_id == self.tenant_id)

    async def appointment(self, appointment_id: UUID) -> Appointment | None:
        return await self.session.scalar(
            self._appointments().where(Appointment.id == appointment_id)
            .execution_options(populate_existing=True)
        )

    async def upcoming(self, customer_id: UUID, since: datetime) -> Sequence[Appointment]:
        return (await self.session.scalars(
            self._appointments().where(
                Appointment.customer_id == customer_id,
                Appointment.status == "scheduled",
                Appointment.scheduled_start >= since,
            ).order_by(Appointment.scheduled_start, Appointment.id)
        )).all()

    async def overlapping(self, start: datetime, end: datetime) -> Sequence[Appointment]:
        return (await self.session.scalars(
            self._appointments().where(
                Appointment.status == "scheduled",
                Appointment.scheduled_start < end,
                Appointment.scheduled_end > start,
            ).execution_options(populate_existing=True)
        )).all()

    async def book(
        self, customer_id: UUID, vehicle_id: UUID, start: datetime, end: datetime, service_type: str,
    ) -> Appointment:
        appointment = Appointment(
            tenant_id=self.tenant_id,
            customer_id=customer_id,
            vehicle_id=vehicle_id,
            scheduled_start=start,
            scheduled_end=end,
            service_type=service_type,
        )
        self.session.add(appointment)
        await self.session.flush()
        return appointment

    async def reschedule(self, appointment_id: UUID, start: datetime, end: datetime) -> Appointment:
        return await self._update(appointment_id, scheduled_start=start, scheduled_end=end)

    async def cancel(self, appointment_id: UUID) -> Appointment:
        return await self._update(appointment_id, status="cancelled")

    async def _update(self, appointment_id: UUID, **changes: datetime | str) -> Appointment:
        statement = (
            update(Appointment)
            .where(Appointment.tenant_id == self.tenant_id, Appointment.id == appointment_id)
            .values(**changes)
            .returning(Appointment)
            .execution_options(populate_existing=True)
        )
        return (await self.session.scalars(statement)).one()
