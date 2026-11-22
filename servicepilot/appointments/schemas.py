from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints

PhoneNumber = Annotated[str, StringConstraints(pattern=r"^\+[1-9][0-9]{1,14}$")]


class BookAppointment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: UUID
    vehicle_id: UUID
    scheduled_start: AwareDatetime
    service_type: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class RescheduleAppointment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_start: AwareDatetime


class CustomerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    phone_number: str


class VehicleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    customer_id: UUID
    vin: str
    make: str
    model: str
    model_year: int


class AppointmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    customer_id: UUID
    vehicle_id: UUID
    scheduled_start: AwareDatetime
    scheduled_end: AwareDatetime
    service_type: str
    status: Literal["scheduled", "completed", "cancelled"]


class SlotResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    start: AwareDatetime
    end: AwareDatetime
