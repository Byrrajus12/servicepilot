from datetime import datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from servicepilot.agent.models import AgentExecution
from servicepilot.appointments.service import AppointmentConflict, AppointmentService, InvalidSlot, RecordNotFound
from servicepilot.conversations.context import current_identity
from servicepilot.conversations.models import Conversation, Message
from servicepilot.conversations.pending import (
    HANDOFF, PROPOSAL_TTL, ActionOutcome, ActionType, AppointmentActionResult, Proposal, ProposalDecision,
    PublishedProposal, pending_proposal, retire_pending,
)
from servicepilot.integrations.mock_dms import MockDMSAdapter
from servicepilot.sms.models import SMSDelivery


async def lock_conversation(session: AsyncSession, tenant_id: UUID, conversation_id: UUID) -> Conversation | None:
    return await session.scalar(select(Conversation).where(
        Conversation.tenant_id == tenant_id, Conversation.id == conversation_id,
    ).with_for_update())


def owns_lease(row: AgentExecution, attempt: int, now: datetime) -> bool:
    return row.status == "running" and row.attempts == attempt and row.lease_expires_at > now


async def appointment_customer(session: AsyncSession, conversation: Conversation) -> UUID | None:
    # Development trust boundary for appointment writes. A stronger sender policy can replace this check.
    if (
        conversation.status != "open" or conversation.customer_resolution != "resolved"
        or conversation.sender_number is None or conversation.recipient_number is None
    ):
        return None
    resolution, customer = await current_identity(session, conversation)
    if resolution == "resolved" and customer is not None and customer.id == conversation.customer_id:
        return customer.id
    return None


async def prepare_proposal(
    session: AsyncSession, conversation: Conversation, customer_id: UUID, kind: ActionType,
    *, vehicle_id: UUID | None = None, appointment_id: UUID | None = None,
    scheduled_start: datetime | None = None, service_type: str | None = None,
) -> Proposal:
    dms = MockDMSAdapter(session, conversation.tenant_id)
    service = AppointmentService(dms)
    dealer = await dms.dealership()
    appointment = None
    if kind != "book":
        appointment = await service.appointment(appointment_id)
        if appointment.customer_id != customer_id:
            raise RecordNotFound
        service.require_upcoming(appointment)
        vehicle_id = appointment.vehicle_id
        service_type = appointment.service_type
        if kind == "reschedule" and scheduled_start == appointment.scheduled_start:
            raise InvalidSlot
    vehicle = await dms.vehicle(vehicle_id)
    if vehicle is None or vehicle.customer_id != customer_id:
        raise RecordNotFound
    if scheduled_start is not None:
        await service.check_slot(scheduled_start, vehicle_id, appointment_id, service_type=service_type)
    return Proposal(
        id=uuid4(), kind=kind, customer_id=customer_id, vehicle_id=vehicle_id,
        vehicle_label=f"{vehicle.model_year} {vehicle.make} {vehicle.model}", dealership=dealer.name,
        timezone=dealer.timezone, service_type=service_type, appointment_id=appointment_id,
        original_start=appointment.scheduled_start if appointment else None,
        appointment_version=appointment.updated_at if appointment else None, scheduled_start=scheduled_start,
    )


def local_time(start: datetime, timezone: str) -> str:
    local = start.astimezone(ZoneInfo(timezone))
    time = local.strftime("%I:%M %p %Z").lstrip("0")
    return f"{local:%a, %b} {local.day}, {local.year} at {time}"


def confirmation_prompt(proposal: Proposal) -> str:
    target = f"the {proposal.service_type} for your {proposal.vehicle_label} at {proposal.dealership}"
    if proposal.kind == "book":
        return f"Should I book {target} for {local_time(proposal.scheduled_start, proposal.timezone)}?"
    original = local_time(proposal.original_start, proposal.timezone)
    if proposal.kind == "cancel":
        return f"Should I cancel {target} scheduled for {original}?"
    new = local_time(proposal.scheduled_start, proposal.timezone)
    return f"Should I move {target} from {original} to {new}?"


def record_proposal(
    session: AsyncSession, conversation: Conversation, proposal: Proposal,
    inbound: Message, outbound: Message, now: datetime,
) -> None:
    retire_pending(session, conversation, "superseded")
    published = PublishedProposal(
        **proposal.model_dump(), request_id=inbound.id, prompt_id=outbound.id, published_at=now,
    )
    conversation.pending_action_type = proposal.kind
    conversation.pending_action_payload = published.model_dump(mode="json")
    conversation.pending_action_expires_at = now + PROPOSAL_TTL
    conversation.pending_confirmation = "awaiting_confirmation"


async def _execute(
    service: AppointmentService, proposal: PublishedProposal, timezone: str,
) -> tuple[ActionOutcome, str, UUID | None]:
    try:
        if timezone != proposal.timezone:
            raise AppointmentConflict
        if proposal.kind == "book":
            appointment = await service.book(
                proposal.customer_id, proposal.vehicle_id, proposal.scheduled_start, proposal.service_type,
            )
        else:
            appointment = await service.appointment(proposal.appointment_id)
            if (
                appointment.customer_id != proposal.customer_id or appointment.vehicle_id != proposal.vehicle_id
                or appointment.updated_at != proposal.appointment_version
                or appointment.scheduled_start != proposal.original_start
            ):
                raise AppointmentConflict
            service.require_upcoming(appointment)
            if proposal.kind == "cancel":
                appointment = await service.cancel(appointment.id)
            else:
                appointment = await service.reschedule(appointment.id, proposal.scheduled_start)
    except (InvalidSlot, AppointmentConflict, RecordNotFound):
        if proposal.kind != "cancel":
            # An unchanged, scheduled source appointment means only the proposed slot could have failed.
            if timezone == proposal.timezone and (proposal.kind == "book" or await _source_unchanged(service, proposal)):
                return "unavailable", "That time is no longer available. What other day or time works for you?", None
        return "appointment_changed", "That appointment can no longer be changed as requested. Please ask for the current appointment details.", None

    if proposal.kind == "cancel":
        return "succeeded", f"Your {appointment.service_type} appointment at {proposal.dealership} was cancelled.", appointment.id
    when = local_time(appointment.scheduled_start, timezone)
    verb = "booked for" if proposal.kind == "book" else "moved to"
    return "succeeded", f"Your {appointment.service_type} at {proposal.dealership} is {verb} {when}.", appointment.id


async def _source_unchanged(service: AppointmentService, proposal: PublishedProposal) -> bool:
    try:
        appointment = await service.appointment(proposal.appointment_id)
        service.require_upcoming(appointment)
    except (RecordNotFound, AppointmentConflict):
        return False
    return (
        appointment.customer_id == proposal.customer_id and appointment.vehicle_id == proposal.vehicle_id
        and appointment.updated_at == proposal.appointment_version
        and appointment.scheduled_start == proposal.original_start
    )


STALE_REPLY = "Please reply to the latest appointment confirmation text."
EXPIRED_REPLY = "That request expired. What appointment change would you like to make?"
UNDELIVERED_REPLY = "I couldn't verify delivery of the confirmation text. Please request the appointment change again."


async def handle_confirmation(
    session: AsyncSession, conversation: Conversation, row: AgentExecution, attempt: int, inbound: Message,
    *, expected_proposal: PublishedProposal | None = None, decision: ProposalDecision | None = None,
) -> tuple[str | None, str | None]:
    now = await session.scalar(select(func.clock_timestamp()))
    if (
        conversation.status != "open" or not owns_lease(row, attempt, now)
        or (row.tenant_id, row.conversation_id, row.id) != (conversation.tenant_id, conversation.id, inbound.id)
        or (inbound.tenant_id, inbound.conversation_id, inbound.direction, inbound.from_number, inbound.to_number)
        != (conversation.tenant_id, conversation.id, "inbound", conversation.sender_number, conversation.recipient_number)
    ):
        return "stale_attempt", None

    recorded = await recorded_action(session, conversation, inbound)
    if recorded is not None:
        if await appointment_customer(session, conversation) != recorded.customer_id:
            return "identity_required", HANDOFF
        if recorded.outcome == "superseded" and recorded.reply_body is None:
            return "revised", None
        return recorded.outcome, recorded.reply_body

    proposal = pending_proposal(conversation)
    if decision is not None and (proposal is None or proposal != expected_proposal):
        return "stale_confirmation", STALE_REPLY
    if proposal is None:
        return None, None

    # A reply received or sent before the exact outbound prompt cannot decide this action.
    prompt = await session.scalar(select(Message).where(
        Message.id == proposal.prompt_id,
        Message.tenant_id == conversation.tenant_id, Message.conversation_id == conversation.id,
        Message.direction == "outbound",
        Message.from_number == inbound.to_number, Message.to_number == inbound.from_number,
    ))
    if (
        prompt is None or inbound.id == proposal.request_id
        or min(inbound.created_at, inbound.occurred_at) <= max(proposal.published_at, prompt.created_at)
    ):
        return "stale_confirmation", STALE_REPLY

    def finalize(
        outcome: ActionOutcome, reply: str | None = None, appointment_id: UUID | None = None,
    ) -> tuple[ActionOutcome, str | None]:
        retire_pending(
            session, conversation, outcome, decision_message_id=inbound.id,
            reply=reply, appointment_id=appointment_id,
        )
        return outcome, reply

    if conversation.pending_action_expires_at <= max(now, inbound.occurred_at):
        return finalize("expired", EXPIRED_REPLY)
    delivery = await session.scalar(select(SMSDelivery).where(
        SMSDelivery.id == proposal.prompt_id, SMSDelivery.tenant_id == conversation.tenant_id,
        SMSDelivery.conversation_id == conversation.id,
    ))
    if (
        delivery is None or delivery.status != "delivered"
        or delivery.delivered_at >= min(inbound.created_at, inbound.occurred_at)
    ):
        if decision is None:
            finalize("superseded", UNDELIVERED_REPLY)
        return "delivery_unverified", UNDELIVERED_REPLY
    if delivery.provider == "twilio":
        # An older prompt may have appeared on the phone after this proposal, or still be in flight.
        ambiguous = await session.scalar(select(SMSDelivery.id).where(
            SMSDelivery.tenant_id == conversation.tenant_id, SMSDelivery.conversation_id == conversation.id,
            SMSDelivery.confirmation_for_id.is_not(None), SMSDelivery.provider == "twilio",
            tuple_(SMSDelivery.created_at, SMSDelivery.id) < (delivery.created_at, delivery.id),
            or_(
                SMSDelivery.status.in_(("sending", "uncertain", "accepted", "sent")),
                SMSDelivery.delivered_at >= delivery.delivered_at,
            ),
        ).limit(1))
        if ambiguous is not None:
            finalize("superseded", UNDELIVERED_REPLY)
            return "delivery_unverified", UNDELIVERED_REPLY
    if await appointment_customer(session, conversation) != proposal.customer_id:
        return finalize("identity_required", HANDOFF)
    if decision is None:
        return None, None
    if decision == "decline":
        return finalize("declined", "Okay, I'll leave things as they are.")
    if decision == "revise":
        finalize("superseded")
        return "revised", None
    if decision == "clarify":
        return "confirmation_required", confirmation_prompt(proposal)
    if decision != "confirm":
        return "invalid_decision", None

    dms = MockDMSAdapter(session, conversation.tenant_id)
    dealer = await dms.dealership(lock_schedule=True)
    # A schedule writer may have held the dealership lock past the lease or proposal expiration.
    now = await session.scalar(select(func.clock_timestamp()))
    if not owns_lease(row, attempt, now):
        return "stale_attempt", None
    if conversation.pending_action_expires_at <= now:
        return finalize("expired", EXPIRED_REPLY)
    if await appointment_customer(session, conversation) != proposal.customer_id:
        return finalize("identity_required", HANDOFF)
    outcome, reply, appointment_id = await _execute(AppointmentService(dms), proposal, dealer.timezone)
    return finalize(outcome, reply, appointment_id)


async def recorded_action(
    session: AsyncSession, conversation: Conversation, inbound: Message,
) -> AppointmentActionResult | None:
    return await session.scalar(select(AppointmentActionResult).where(
        AppointmentActionResult.tenant_id == conversation.tenant_id,
        AppointmentActionResult.conversation_id == conversation.id,
        AppointmentActionResult.decision_message_id == inbound.id,
    ))
