from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from servicepilot.appointments.schemas import (
    AppointmentResponse,
    BookAppointment,
    CustomerResponse,
    PhoneNumber,
    RescheduleAppointment,
    SlotResponse,
    VehicleResponse,
)
from servicepilot.appointments.service import (
    AppointmentConflict,
    AppointmentError,
    AppointmentService,
    InvalidSlot,
    RecordNotFound,
)
from servicepilot.integrations.mock_dms import MockDMSAdapter
from servicepilot.tenancy.auth import DealershipTransaction

router = APIRouter()


async def appointment_service(dealership: DealershipTransaction) -> AppointmentService:
    session, tenant_id = dealership
    return AppointmentService(MockDMSAdapter(session, tenant_id))


Service = Annotated[AppointmentService, Depends(appointment_service, scope="function")]


async def appointment_error(_: Request, error: AppointmentError) -> JSONResponse:
    status_code = {AppointmentConflict: 409, InvalidSlot: 422, RecordNotFound: 404}[type(error)]
    return JSONResponse(status_code=status_code, content={"detail": str(error)})


@router.get("/customers", response_model=list[CustomerResponse])
async def find_customers(phone_number: Annotated[PhoneNumber, Query()], service: Service):
    return await service.find_customers(phone_number)


@router.get("/customers/{customer_id}/vehicles", response_model=list[VehicleResponse])
async def list_vehicles(customer_id: UUID, service: Service):
    return await service.vehicles(customer_id)


@router.get("/customers/{customer_id}/appointments", response_model=list[AppointmentResponse])
async def upcoming_appointments(customer_id: UUID, service: Service):
    return await service.upcoming(customer_id)


@router.get("/availability", response_model=list[SlotResponse])
async def availability(
    day: Annotated[date, Query(alias="date")], service: Service, vehicle_id: UUID | None = None,
):
    return await service.availability(day, vehicle_id)


@router.get("/appointments/{appointment_id}", response_model=AppointmentResponse)
async def get_appointment(appointment_id: UUID, service: Service):
    return await service.appointment(appointment_id)


@router.post("/appointments", response_model=AppointmentResponse, status_code=201)
async def book_appointment(booking: BookAppointment, service: Service):
    return await service.book(
        booking.customer_id, booking.vehicle_id, booking.scheduled_start, booking.service_type,
    )


@router.patch("/appointments/{appointment_id}", response_model=AppointmentResponse)
async def reschedule_appointment(appointment_id: UUID, change: RescheduleAppointment, service: Service):
    return await service.reschedule(appointment_id, change.scheduled_start)


@router.post("/appointments/{appointment_id}/cancel", response_model=AppointmentResponse)
async def cancel_appointment(appointment_id: UUID, service: Service):
    return await service.cancel(appointment_id)
