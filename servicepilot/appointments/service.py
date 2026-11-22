from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from servicepilot.appointments.models import Appointment
from servicepilot.customers.models import Customer
from servicepilot.integrations.dms import DMSAdapter
from servicepilot.tenancy.models import Tenant
from servicepilot.vehicles.models import Vehicle


class AppointmentError(Exception):
    pass


class RecordNotFound(AppointmentError):
    pass


class InvalidSlot(AppointmentError):
    pass


class AppointmentConflict(AppointmentError):
    pass


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime


def _local_instants(local: datetime, zone: ZoneInfo) -> set[datetime]:
    candidates = {local.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)}
    return {instant for instant in candidates if instant.astimezone(zone).replace(tzinfo=None) == local}


def _daily_slots(dealer: Tenant, day: date, zone: ZoneInfo) -> list[Slot]:
    if day.weekday() not in dealer.service_days:
        return []

    try:
        start = datetime.combine(day, dealer.opens_at)
        close = datetime.combine(day, dealer.closes_at)
        duration = timedelta(minutes=dealer.appointment_minutes)
        slot_count = (close - start) // duration
        slots = []
        for index in range(slot_count):
            local_start = start + index * duration
            local_end = local_start + duration
            for instant in _local_instants(local_start, zone):
                end = instant + duration
                # Keep slots that fit in both wall time and elapsed time.
                if end.astimezone(zone).replace(tzinfo=None) == local_end:
                    slots.append(Slot(instant, end))
        return sorted(slots, key=lambda slot: slot.start)
    except OverflowError:
        raise InvalidSlot("Appointment date is out of range") from None


def _has_capacity(
    slot: Slot, appointments: Sequence[Appointment], capacity: int, vehicle_id: UUID | None,
) -> bool:
    changes: Counter[datetime] = Counter()
    for appointment in appointments:
        start = max(slot.start, appointment.scheduled_start)
        end = min(slot.end, appointment.scheduled_end)
        if start < end:
            if vehicle_id == appointment.vehicle_id:
                return False
            changes[start] += 1
            changes[end] -= 1

    # Older appointments can overlap separate portions of the slot without overlapping each other.
    occupied = 0
    for instant in sorted(changes):
        occupied += changes[instant]
        if occupied >= capacity:
            return False
    return True


class AppointmentService:
    """Use a dealership-scoped adapter within the caller's transaction."""

    def __init__(self, dms: DMSAdapter) -> None:
        self.dms = dms

    async def find_customers(self, phone_number: str) -> Sequence[Customer]:
        return await self.dms.find_customers(phone_number)

    async def vehicles(self, customer_id: UUID) -> Sequence[Vehicle]:
        await self._require_customer(customer_id)
        return await self.dms.vehicles(customer_id)

    async def appointment(self, appointment_id: UUID) -> Appointment:
        appointment = await self.dms.appointment(appointment_id)
        if appointment is None:
            raise RecordNotFound("Appointment not found")
        return appointment

    async def upcoming(self, customer_id: UUID) -> Sequence[Appointment]:
        await self._require_customer(customer_id)
        return await self.dms.upcoming(customer_id, datetime.now(UTC))

    async def availability(self, day: date, vehicle_id: UUID | None = None) -> list[Slot]:
        dealer = await self._dealership()
        if vehicle_id is not None and await self.dms.vehicle(vehicle_id) is None:
            raise RecordNotFound("Vehicle not found")
        zone = ZoneInfo(dealer.timezone)
        now = datetime.now(UTC)
        if day < now.astimezone(zone).date():
            return []
        slots = [slot for slot in _daily_slots(dealer, day, zone) if slot.start > now]
        if not slots:
            return []
        appointments = await self.dms.overlapping(slots[0].start, slots[-1].end)
        return [
            Slot(slot.start.astimezone(zone), slot.end.astimezone(zone))
            for slot in slots
            if _has_capacity(slot, appointments, dealer.service_capacity, vehicle_id)
        ]

    async def book(
        self, customer_id: UUID, vehicle_id: UUID, start: datetime, service_type: str,
    ) -> Appointment:
        dealer = await self._dealership(lock_schedule=True)
        await self._require_customer(customer_id)
        vehicle = await self.dms.vehicle(vehicle_id)
        if vehicle is None or vehicle.customer_id != customer_id:
            raise RecordNotFound("Vehicle not found for this customer")

        slot = await self._available_slot(dealer, start, vehicle_id)
        return await self.dms.book(customer_id, vehicle_id, slot.start, slot.end, service_type)

    async def reschedule(self, appointment_id: UUID, start: datetime) -> Appointment:
        dealer = await self._dealership(lock_schedule=True)
        appointment = await self.appointment(appointment_id)
        self._require_upcoming(appointment)
        slot = await self._available_slot(dealer, start, appointment.vehicle_id, appointment.id)
        return await self.dms.reschedule(appointment.id, slot.start, slot.end)

    async def cancel(self, appointment_id: UUID) -> Appointment:
        await self._dealership(lock_schedule=True)
        appointment = await self.appointment(appointment_id)
        self._require_upcoming(appointment)
        return await self.dms.cancel(appointment.id)

    async def _dealership(self, *, lock_schedule: bool = False) -> Tenant:
        dealer = await self.dms.dealership(lock_schedule=lock_schedule)
        if dealer is None:
            raise RecordNotFound("Dealership not found")
        return dealer

    async def _require_customer(self, customer_id: UUID) -> None:
        if await self.dms.customer(customer_id) is None:
            raise RecordNotFound("Customer not found")

    @staticmethod
    def _require_upcoming(appointment: Appointment) -> None:
        if appointment.status != "scheduled" or appointment.scheduled_start <= datetime.now(UTC):
            raise AppointmentConflict("Only upcoming scheduled appointments can be changed")

    async def _available_slot(
        self, dealer: Tenant, start: datetime, vehicle_id: UUID, excluding: UUID | None = None,
    ) -> Slot:
        if start.utcoffset() is None:
            raise InvalidSlot("Appointment time must include a UTC offset")
        zone = ZoneInfo(dealer.timezone)
        try:
            start = start.astimezone(UTC)
            day = start.astimezone(zone).date()
        except OverflowError:
            raise InvalidSlot("Appointment date is out of range") from None
        if start <= datetime.now(UTC):
            raise InvalidSlot("Appointment time must be in the future")
        slot = next((slot for slot in _daily_slots(dealer, day, zone) if slot.start == start), None)
        if slot is None:
            raise InvalidSlot("Appointment time must match a slot during dealership hours")

        appointments = await self.dms.overlapping(slot.start, slot.end)
        others = [appointment for appointment in appointments if appointment.id != excluding]
        if not _has_capacity(slot, others, dealer.service_capacity, vehicle_id):
            raise AppointmentConflict("Appointment slot is no longer available")
        return slot
