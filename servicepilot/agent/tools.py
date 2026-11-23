from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from agents import RunContextWrapper, function_tool
from pydantic import AwareDatetime, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from servicepilot.agent.actions import appointment_customer, lock_conversation, owns_lease, prepare_proposal
from servicepilot.agent.models import AgentExecution
from servicepilot.appointments.schemas import AppointmentResponse, SlotResponse, VehicleResponse
from servicepilot.appointments.service import AppointmentConflict, AppointmentService, InvalidSlot, RecordNotFound
from servicepilot.conversations.context import current_identity
from servicepilot.conversations.models import Conversation
from servicepilot.conversations.pending import ActionType, Proposal, ProposalDecision, PublishedProposal
from servicepilot.integrations.mock_dms import MockDMSAdapter

RESULT_LIMIT = 10
AvailabilityPeriod = Literal["morning", "afternoon", "evening"]


@dataclass
class AdvisorContext:
    tenant_id: UUID
    conversation_id: UUID
    customer_id: UUID | None
    sessions: async_sessionmaker[AsyncSession]
    inbound_id: UUID
    attempt: int
    tool_calls: list[dict[str, str]] = field(default_factory=list)
    proposal: Proposal | None = None
    pending: PublishedProposal | None = None
    decision: ProposalDecision | None = None
    availability: dict[tuple[date, UUID | None, AvailabilityPeriod | None], list[SlotResponse]] = field(
        default_factory=dict,
    )
    appointment_details: dict[UUID, AppointmentResponse] = field(default_factory=dict)


@function_tool
async def interpret_appointment_reply(
    ctx: RunContextWrapper[AdvisorContext], decision: ProposalDecision,
) -> dict:
    """Classify this SMS for the active proposal. Confirm only clear, unconditional agreement to the exact proposal.
    Decline a clear refusal, revise a changed request, or clarify any uncertain or conflicting reply.
    This records intent only. It cannot change an appointment.
    """
    context = ctx.context
    if context.pending is None:
        return {"status": "no_pending_action"}
    context.decision = decision if context.decision is None else "clarify"
    return {"status": "intent_recorded"}


def vehicle_data(vehicle: VehicleResponse) -> dict:
    return vehicle.model_dump(mode="json", exclude={"customer_id", "vin"})


def appointment_data(appointment: AppointmentResponse, zone: ZoneInfo) -> dict:
    data = appointment.model_dump(mode="json", exclude={"customer_id"})
    data["scheduled_start"] = appointment.scheduled_start.astimezone(zone).isoformat()
    data["scheduled_end"] = appointment.scheduled_end.astimezone(zone).isoformat()
    return data


def limited(key: str, items: list) -> dict:
    return {key: items[:RESULT_LIMIT], "truncated": len(items) > RESULT_LIMIT}


class IdentityRequired(Exception):
    pass


def tool_failure(ctx: RunContextWrapper[AdvisorContext], _error: Exception) -> str:
    ctx.context.tool_calls.append({"name": getattr(ctx, "tool_name", "unknown"), "outcome": "tool_error"})
    return '{"status":"tool_error"}'


async def _read(
    context: AdvisorContext,
    tool: str,
    operation: Callable[[MockDMSAdapter, AppointmentService], Awaitable[dict]],
    *,
    private: bool = True,
) -> dict:
    try:
        async with context.sessions() as session:
            if private:
                conversation = await session.scalar(select(Conversation).where(
                    Conversation.tenant_id == context.tenant_id,
                    Conversation.id == context.conversation_id,
                    Conversation.status == "open",
                ))
                if context.customer_id is None or conversation is None:
                    raise IdentityRequired
                resolution, customer = await current_identity(session, conversation)
                if resolution != "resolved" or customer is None or customer.id != context.customer_id:
                    raise IdentityRequired
            dms = MockDMSAdapter(session, context.tenant_id)
            result = {"status": "ok", **await operation(dms, AppointmentService(dms))}
    except IdentityRequired:
        result = {"status": "identity_required"}
    except RecordNotFound:
        result = {"status": "not_found"}
    except InvalidSlot:
        result = {"status": "invalid_date"}
    except SQLAlchemyError:
        result = {"status": "unavailable"}
    context.tool_calls.append({"name": tool, "outcome": result["status"]})
    return result


@function_tool(failure_error_function=tool_failure)
async def get_customer_vehicles(ctx: RunContextWrapper[AdvisorContext]) -> dict:
    """List vehicles owned by the resolved customer."""
    async def read(dms: MockDMSAdapter, service: AppointmentService) -> dict:
        vehicles = await service.vehicles(ctx.context.customer_id)
        return limited("vehicles", [
            vehicle_data(VehicleResponse.model_validate(v)) for v in vehicles[:RESULT_LIMIT + 1]
        ])

    return await _read(ctx.context, "get_customer_vehicles", read)


@function_tool(failure_error_function=tool_failure)
async def get_customer_appointments(ctx: RunContextWrapper[AdvisorContext]) -> dict:
    """List upcoming service appointments for the resolved customer."""
    async def read(dms: MockDMSAdapter, service: AppointmentService) -> dict:
        dealer = await dms.dealership()
        if dealer is None:
            raise RecordNotFound
        zone = ZoneInfo(dealer.timezone)
        appointments = await service.upcoming(ctx.context.customer_id)
        return limited("appointments", [
            appointment_data(AppointmentResponse.model_validate(item), zone)
            for item in appointments[:RESULT_LIMIT + 1]
        ])

    return await _read(ctx.context, "get_customer_appointments", read)


@function_tool(failure_error_function=tool_failure)
async def get_appointment_details(ctx: RunContextWrapper[AdvisorContext], appointment_id: UUID) -> dict:
    """Read an appointment by its reference if it belongs to the resolved customer."""
    async def read(dms: MockDMSAdapter, service: AppointmentService) -> dict:
        appointment = await service.appointment(appointment_id)
        dealer = await dms.dealership()
        if dealer is None or appointment.customer_id != ctx.context.customer_id:
            raise RecordNotFound
        details = AppointmentResponse.model_validate(appointment)
        ctx.context.appointment_details[appointment_id] = details
        return {"appointment": appointment_data(details, ZoneInfo(dealer.timezone))}

    return await _read(ctx.context, "get_appointment_details", read)


@function_tool(failure_error_function=tool_failure)
async def get_available_slots(
    ctx: RunContextWrapper[AdvisorContext], day: date, vehicle_id: UUID | None = None,
    period: AvailabilityPeriod | None = None,
) -> dict:
    """Find open slots on a dealership-local date. Filter by a local time of day if requested."""
    async def read(dms: MockDMSAdapter, service: AppointmentService) -> dict:
        if vehicle_id is not None:
            vehicle = await dms.vehicle(vehicle_id)
            if vehicle is None or vehicle.customer_id != ctx.context.customer_id:
                raise RecordNotFound
        slots = await service.availability(day, vehicle_id)
        hours = {"morning": range(0, 12), "afternoon": range(12, 17), "evening": range(17, 24)}
        matches = [
            SlotResponse.model_validate(slot) for slot in slots
            if period is None or slot.start.hour in hours[period]
        ][:RESULT_LIMIT + 1]
        ctx.context.availability[day, vehicle_id, period] = matches[:RESULT_LIMIT]
        return limited("slots", [slot.model_dump(mode="json") for slot in matches])

    return await _read(ctx.context, "get_available_slots", read, private=vehicle_id is not None)


async def _propose(context: AdvisorContext, name: str, kind: ActionType, **parameters) -> dict:
    try:
        async with context.sessions.begin() as session:
            conversation = await lock_conversation(session, context.tenant_id, context.conversation_id)
            row = await session.get_one(AgentExecution, context.inbound_id)
            now = await session.scalar(select(func.clock_timestamp()))
            if (
                conversation is None or row.tenant_id != context.tenant_id
                or row.conversation_id != context.conversation_id or not owns_lease(row, context.attempt, now)
            ):
                status = "stale_attempt"
            elif context.customer_id is None or await appointment_customer(session, conversation) != context.customer_id:
                status = "identity_required"
            elif context.pending is not None and context.decision != "revise":
                status = "pending_decision_required"
            elif context.proposal is not None:
                status = "proposal_already_prepared"
            else:
                context.proposal = await prepare_proposal(
                    session, conversation, context.customer_id, kind, **parameters,
                )
                status = "proposal_prepared"
    except RecordNotFound:
        status = "not_found"
    except InvalidSlot:
        status = "invalid_slot"
    except AppointmentConflict:
        status = "unavailable" if kind == "book" else "appointment_or_slot_unavailable"
    context.tool_calls.append({"name": name, "outcome": status})
    return {"status": status}


@function_tool(failure_error_function=tool_failure)
async def propose_booking(
    ctx: RunContextWrapper[AdvisorContext], vehicle_id: UUID, scheduled_start: AwareDatetime,
    service_type: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)],
) -> dict:
    """Prepare a booking at an exact available time. On success the backend sends the confirmation question."""
    if "\x00" in service_type:
        return {"status": "invalid_service"}
    return await _propose(
        ctx.context, "propose_booking", "book",
        vehicle_id=vehicle_id, scheduled_start=scheduled_start, service_type=service_type,
    )


@function_tool(failure_error_function=tool_failure)
async def propose_reschedule(
    ctx: RunContextWrapper[AdvisorContext], appointment_id: UUID, scheduled_start: AwareDatetime,
) -> dict:
    """Prepare a move to an exact available time. On success the backend sends the confirmation question."""
    return await _propose(
        ctx.context, "propose_reschedule", "reschedule",
        appointment_id=appointment_id, scheduled_start=scheduled_start,
    )


@function_tool(failure_error_function=tool_failure)
async def propose_cancellation(ctx: RunContextWrapper[AdvisorContext], appointment_id: UUID) -> dict:
    """Prepare cancellation of the selected appointment. On success the backend sends the confirmation question."""
    return await _propose(ctx.context, "propose_cancellation", "cancel", appointment_id=appointment_id)


ADVISOR_TOOLS = [
    get_customer_vehicles, get_customer_appointments, get_appointment_details, get_available_slots,
    propose_booking, propose_reschedule, propose_cancellation,
]
