from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from agents import FunctionTool, ModelBehaviorError, RunContextWrapper, function_tool
from pydantic import AwareDatetime, StringConstraints, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from servicepilot.agent.actions import appointment_customer, lock_conversation, owns_lease, prepare_proposal
from servicepilot.agent.confirmation import ConfirmationAttempt, InterpretationInput
from servicepilot.agent.models import AgentExecution
from servicepilot.appointments.schemas import AppointmentResponse, SlotResponse, VehicleResponse
from servicepilot.appointments.service import (
    AppointmentConflict, AppointmentService, InvalidSlot, RecordNotFound,
)
from servicepilot.conversations.context import current_identity
from servicepilot.conversations.models import Conversation
from servicepilot.conversations.pending import ActionType, ConditionalRequest, Proposal, PublishedProposal
from servicepilot.integrations.mock_dms import MockDMSAdapter
from servicepilot.tenancy.models import Tenant

RESULT_LIMIT = 10
INTENT_RECORDED = "intent_recorded"
AvailabilityPeriod = Literal["morning", "lunch", "afternoon", "evening"]


@dataclass
class AdvisorContext:
    tenant_id: UUID
    conversation_id: UUID
    customer_id: UUID | None
    sessions: async_sessionmaker[AsyncSession]
    inbound_id: UUID
    attempt: int
    tool_calls: list[dict[str, str]] = field(default_factory=list)
    tool_details: list[dict] = field(default_factory=list)
    proposal: Proposal | None = None
    pending: PublishedProposal | None = None
    confirmation: ConfirmationAttempt | None = None
    ownership_version: int = 0
    consultation_questions: list[str] = field(default_factory=list)
    condition_questions: set[str] = field(default_factory=set)
    staff_fact: str | None = None
    resume_latest_sms: str | None = None
    awaiting_staff: bool = False
    requires_fresh_consent: bool = False
    condition: ConditionalRequest | None = None
    interpreted_condition: str | None = None
    clarification_sms: str | None = None
    consultations: list[dict] = field(default_factory=list)
    consultation_history: dict[str, dict] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)
    service_durations: dict[str, tuple[str, int]] = field(default_factory=dict)


async def _interpret_appointment_reply(ctx: RunContextWrapper[AdvisorContext], arguments: str) -> str:
    confirmation = ctx.context.confirmation
    if ctx.context.pending is None or confirmation is None:
        return "no_pending_action"
    confirmation.tool_invocations += 1
    if confirmation.tool_invocations != 1:
        confirmation.interpretation = "duplicate"
        confirmation.decision = None
        return "duplicate"
    try:
        interpretation = InterpretationInput.model_validate_json(arguments)
        confirmation.decision = interpretation.decision
        confirmation.explicit_proposal_reference = interpretation.explicit_proposal_reference
        if interpretation.decision == "conditional" and interpretation.condition and "\x00" not in interpretation.condition:
            ctx.context.interpreted_condition = interpretation.condition.strip() or None
        if interpretation.decision == "clarify" and interpretation.clarification_sms and "\x00" not in interpretation.clarification_sms:
            ctx.context.clarification_sms = interpretation.clarification_sms.strip() or None
    except ValidationError:
        confirmation.interpretation = "invalid"
        return "invalid"
    except Exception:
        confirmation.interpretation = "tool_error"
        raise
    confirmation.interpretation = "decided"
    return INTENT_RECORDED


confirmation_schema = InterpretationInput.model_json_schema()
confirmation_schema["required"] = list(confirmation_schema["properties"])
confirmation_schema["properties"]["condition"].pop("default", None)
confirmation_schema["properties"]["clarification_sms"].pop("default", None)
interpret_appointment_reply = FunctionTool(
    name="interpret_appointment_reply",
    description="Record the semantic intent of the current SMS for the active proposal.",
    params_json_schema=confirmation_schema,
    on_invoke_tool=_interpret_appointment_reply,
)


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


def tool_failure(ctx: RunContextWrapper[AdvisorContext], error: Exception) -> str:
    status = "tool_error"
    validation = error if isinstance(error, ValidationError) else error.__cause__
    if isinstance(validation, ValidationError):
        status = ("missing_parameters" if all(item["type"] == "missing" for item in validation.errors())
                  else "invalid_arguments")
    elif isinstance(error, ModelBehaviorError):
        status = "invalid_arguments"
    tool = getattr(ctx, "tool_name", "unknown")
    ctx.context.tool_calls.append({"name": tool, "outcome": status})
    return '{"status":"' + status + '"}'


async def _read(
    context: AdvisorContext,
    tool: str,
    operation: Callable[[MockDMSAdapter, AppointmentService], Awaitable[dict]],
    *,
    private: bool = True,
    diagnostic: dict | None = None,
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
    except AppointmentConflict:
        result = {"status": "appointment_unavailable"}
    except SQLAlchemyError:
        result = {"status": "tool_error"}
    if diagnostic is not None:
        context.tool_details.append({"name": tool, "outcome": result["status"], **diagnostic})
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
        return {"appointment": appointment_data(details, ZoneInfo(dealer.timezone))}

    return await _read(ctx.context, "get_appointment_details", read)


@function_tool(failure_error_function=tool_failure)
async def get_available_slots(
    ctx: RunContextWrapper[AdvisorContext], day: date, vehicle_id: UUID | None = None,
    period: AvailabilityPeriod | None = None,
    requested_start: AwareDatetime | None = None, service_type: str | None = None,
    appointment_id: UUID | None = None,
) -> dict:
    """Check openings for one service in dealership time.

    Use a single catalog service ID for service_type and the known vehicle_id for a
    booking. For a move, supply appointment_id alone for service and vehicle scope.
    Use requested_start with its offset for an exact time. Its local date takes priority
    over day. One exact lookup returns availability, the reason for rejection and dated
    alternatives. An unavailable exact time can still return nearby openings.
    """
    customer_id = ctx.context.customer_id if vehicle_id or appointment_id or requested_start else None
    diagnostic = {
        "day": day.isoformat(), "period": period, "exact_time": requested_start is not None,
        "has_vehicle": vehicle_id is not None, "has_appointment": appointment_id is not None,
        "has_service": service_type is not None,
    }

    async def read(dms: MockDMSAdapter, service: AppointmentService) -> dict:
        dealer = await dms.dealership()
        if dealer is None:
            raise RecordNotFound
        actual_vehicle_id, actual_service = vehicle_id, service_type
        if appointment_id is not None:
            appointment = await service.appointment(appointment_id)
            if appointment.customer_id != ctx.context.customer_id:
                raise RecordNotFound
            service.require_upcoming(appointment)
            actual_vehicle_id, actual_service = appointment.vehicle_id, appointment.service_type
        if vehicle_id is not None:
            vehicle = await dms.vehicle(vehicle_id)
            if vehicle is None or vehicle.customer_id != ctx.context.customer_id:
                raise RecordNotFound
        zone = ZoneInfo(dealer.timezone)
        local_request = requested_start.astimezone(zone) if requested_start is not None else None
        lookup_day = local_request.date() if local_request is not None else day
        wanted = local_request.astimezone(UTC) if local_request is not None else None
        now = datetime.now(UTC)
        closed = lookup_day.weekday() not in dealer.service_days or lookup_day in dealer.closed_dates
        entry = dealer.service_catalog.get(actual_service.casefold()) if actual_service else None
        result = {
            "day": lookup_day.isoformat(), "timezone": zone.key, "period": period,
            "scope": "customer" if customer_id is not None else "public",
            "vehicle_checked": actual_vehicle_id is not None,
            "service_id": actual_service.casefold() if actual_service else None,
            "requested_start": local_request.isoformat() if local_request is not None else None,
            "operating_hours": {"opens_at": dealer.opens_at.isoformat(), "closes_at": dealer.closes_at.isoformat(),
                                "closed_on_requested_day": closed},
            "service_duration_minutes": entry["minutes"] if entry else None,
            "service_status": "configured" if entry else "unconfigured" if not dealer.service_catalog else "unspecified",
        }
        diagnostic.update(requested_start=result["requested_start"], scope=result["scope"])
        if (service_type is not None and appointment_id is None and dealer.service_catalog
                and service_type.casefold() not in dealer.service_catalog):
            return {**result, "status": "unknown_service", "slots": [],
                    "valid_service_ids": list(dealer.service_catalog)}
        if entry:
            ctx.context.service_durations[result["service_id"]] = (entry["name"], entry["minutes"])

        hours = {"morning": range(0, 12), "lunch": range(11, 14), "afternoon": range(12, 17),
                 "evening": range(17, 24)}
        if wanted is not None:
            slots = await service.nearby_availability(
                wanted, actual_vehicle_id, service_type=actual_service, excluding=appointment_id,
                customer_id=customer_id,
            )
            available = any(slot.start.astimezone(UTC) == wanted for slot in slots)
            result.update(
                requested_available=available,
                unavailable_reason=None if available else await service.explain_unavailable_start(
                    dealer, local_request, actual_vehicle_id, customer_id,
                    service_type=actual_service, excluding=appointment_id,
                ),
            )
        else:
            slots = await service.availability(
                lookup_day, actual_vehicle_id, service_type=actual_service, excluding=appointment_id,
                customer_id=customer_id,
            )
            slots = [slot for slot in slots if period is None or slot.start.hour in hours[period]]
            if period == "lunch":
                noon = datetime.combine(lookup_day, time(12), zone)
                slots.sort(key=lambda slot: (abs(slot.start - noon), slot.start))
            if not slots:
                result["unavailable_reason"] = (
                    "past" if lookup_day < now.astimezone(zone).date() else "closed" if closed else "no_openings"
                )
        matches = [SlotResponse.model_validate(slot).model_dump(mode="json") for slot in slots[:RESULT_LIMIT + 1]]
        diagnostic.update(
            reason=result.get("unavailable_reason"),
            alternatives_day=slots[0].start.date().isoformat() if slots else None, slot_count=len(slots),
            offered_starts=[item["start"] for item in matches],
        )
        return {**result, **limited("slots", matches)}

    return await _read(
        ctx.context, "get_available_slots", read,
        private=vehicle_id is not None or appointment_id is not None or customer_id is not None,
        diagnostic=diagnostic,
    )


async def _propose(context: AdvisorContext, name: str, kind: ActionType, **parameters) -> dict:
    try:
        async with context.sessions.begin() as session:
            conversation = await lock_conversation(session, context.tenant_id, context.conversation_id)
            row = await session.get_one(AgentExecution, context.inbound_id)
            now = await session.scalar(select(func.clock_timestamp()))
            if (
                conversation is None or row.tenant_id != context.tenant_id
                or row.conversation_id != context.conversation_id or not owns_lease(row, context.attempt, now)
                or conversation.owner != "ai" or conversation.ownership_version != context.ownership_version
            ):
                status = "stale_attempt"
            elif context.customer_id is None or await appointment_customer(session, conversation) != context.customer_id:
                status = "identity_required"
            elif context.pending is not None:
                status = "pending_decision_required"
            elif context.requires_fresh_consent and context.condition is None:
                status = "needs_customer_followup"
            elif context.proposal is not None:
                status = "proposal_already_prepared"
            else:
                dealer = await session.get_one(Tenant, context.tenant_id)
                if kind == "book" and dealer.service_catalog and (
                    parameters["service_type"].casefold() not in dealer.service_catalog
                ):
                    status = "invalid_service"
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
    except SQLAlchemyError:
        status = "tool_error"
    context.tool_calls.append({"name": name, "outcome": status})
    return {"status": status}


@function_tool(failure_error_function=tool_failure)
async def propose_booking(
    ctx: RunContextWrapper[AdvisorContext], vehicle_id: UUID, scheduled_start: AwareDatetime,
    service_type: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)],
) -> dict:
    """Check and prepare an exact requested time for one catalog service ID.

    Success means a confirmation question will be sent. The appointment is not booked yet.
    """
    if "\x00" in service_type:
        ctx.context.tool_calls.append({"name": "propose_booking", "outcome": "invalid_service"})
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


@function_tool(failure_error_function=tool_failure)
async def ask_dealership_staff(
    ctx: RunContextWrapper[AdvisorContext],
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)],
    addresses_condition: bool = False,
) -> dict:
    """Stage a distinct private staff question or look up matching work."""
    context = ctx.context
    question = " ".join(question.split())
    existing = context.consultation_history.get(question.casefold())
    result = {}
    if "\x00" in question or context.staff_fact is not None:
        outcome = "consultation_unavailable"
    elif staged := next((item for item in context.consultation_questions if item.casefold() == question.casefold()), None):
        if addresses_condition:
            context.condition_questions.add(staged)
        outcome = "consultation_staged"
    elif existing and (not addresses_condition or existing["matches_condition"]):
        outcome = f"consultation_{existing['status']}"
        result["consultation_id"] = str(existing["id"])
        if existing["status"] == "resolved":
            result["approved_fact"] = existing["public_fact"]
            result["answered_at"] = existing["answered_at"].isoformat()
    elif len(context.consultation_questions) >= 3:
        outcome = "consultation_unavailable"
    else:
        context.consultation_questions.append(question)
        if addresses_condition:
            context.condition_questions.add(question)
        outcome = "consultation_prepared"
    context.tool_calls.append({"name": "ask_dealership_staff", "outcome": outcome})
    return {"status": outcome, **result}


@function_tool(failure_error_function=tool_failure)
async def get_service_information(ctx: RunContextWrapper[AdvisorContext], day: date | None = None) -> dict:
    """Read verified services, durations, policies and hours. Pass day for closures.

    Missing policy keys are unknown, including prices, waiting areas, loaners, shuttles,
    fees, parts and technicians. Ask staff when the missing fact matters to the customer.
    """
    diagnostic = {"day": day.isoformat() if day is not None else None}

    async def read(dms: MockDMSAdapter, service: AppointmentService) -> dict:
        dealer = await dms.dealership()
        if dealer is None:
            raise RecordNotFound
        hours = {
            "timezone": dealer.timezone,
            "opens_at": dealer.opens_at.isoformat(timespec="minutes"),
            "closes_at": dealer.closes_at.isoformat(timespec="minutes"),
            "service_days": dealer.service_days,
        }
        if day is not None:
            hours.update(
                date=day.isoformat(), closed=day in dealer.closed_dates or day.weekday() not in dealer.service_days,
            )
        diagnostic.update(service_count=len(dealer.service_catalog), policy_count=len(dealer.service_policies),
                          closed=hours.get("closed"))
        ctx.context.evidence.update({f"policy:{key}": value for key, value in dealer.service_policies.items()})
        ctx.context.service_durations.update({
            key: (entry["name"], entry["minutes"]) for key, entry in dealer.service_catalog.items()
        })
        return {
            "catalog_status": "configured" if dealer.service_catalog else "unconfigured",
            "services": [
                {"service_id": key, "name": entry["name"], "duration_minutes": entry["minutes"],
                 "description": entry["description"]}
                for key, entry in dealer.service_catalog.items()
            ],
            "policies": dealer.service_policies,
            "missing_policies_are_unknown": True,
            "hours": hours,
        }

    return await _read(ctx.context, "get_service_information", read, private=False, diagnostic=diagnostic)


ADVISOR_TOOLS = [
    get_customer_vehicles, get_customer_appointments, get_appointment_details, get_available_slots,
    propose_booking, propose_reschedule, propose_cancellation, get_service_information, ask_dealership_staff,
]
