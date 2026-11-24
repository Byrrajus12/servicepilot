from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import ValidationError
from sqlalchemy import func, select

from servicepilot.agent.execution import (
    ExecutionView, PreviousMessagePending, get_execution, process_execution,
)
from servicepilot.agent.models import AgentExecution
from servicepilot.appointments.schemas import PhoneNumber
from servicepilot.config import SIMULATED_RECIPIENT
from servicepilot.conversations.context import HistoryMessage, read_history
from servicepilot.conversations.models import Conversation, Message
from servicepilot.conversations.pending import AppointmentActionResult, pending_proposal
from servicepilot.conversations.schemas import InboundResult, InboundSMS, SMSContent
from servicepilot.sms.delivery import DeliveryView, deliver_next
from servicepilot.sms.ingest import ingest_sms
from servicepilot.sms.models import SMSDelivery
from servicepilot.sms.request import read_sms_body
from servicepilot.sms.transport import SimulationTransport
from servicepilot.staff.consultations import thread_consultations
from servicepilot.staff.models import Consultation
from servicepilot.tenancy.auth import AuthenticatedDealership, DealershipTransaction

router = APIRouter()


def require_simulation(request: Request) -> None:
    if not request.app.state.settings.enable_sms_simulation:
        raise HTTPException(404, "SMS simulation is disabled")


@router.get("/simulations/sms")
async def simulated_inbox(
    sender_phone: PhoneNumber, dealership: DealershipTransaction, request: Request,
    include_all: bool = False,
) -> dict:
    require_simulation(request)
    session, tenant_id = dealership
    conversation = await session.scalar(select(Conversation).where(
        Conversation.tenant_id == tenant_id, Conversation.sender_number == sender_phone,
        Conversation.recipient_number == SIMULATED_RECIPIENT, Conversation.status == "open",
    ))
    if conversation is None:
        return {"conversation_id": None, "owner": None, "waiting_for_staff": False, "outbound": []}
    waiting = await session.scalar(select(select(Consultation.id).where(
        *thread_consultations(conversation), Consultation.status.in_(("open", "answered")),
    ).exists()))
    followup = select(AgentExecution.id).where(
        AgentExecution.tenant_id == tenant_id, AgentExecution.conversation_id == conversation.id,
        AgentExecution.reply_id == Message.id, AgentExecution.resume_source_id.is_not(None),
    ).exists()
    query = select(Message, SMSDelivery, followup).join(
        SMSDelivery, (SMSDelivery.id == Message.id) & (SMSDelivery.tenant_id == Message.tenant_id)
        & (SMSDelivery.conversation_id == Message.conversation_id),
    ).where(
        Message.tenant_id == tenant_id, Message.conversation_id == conversation.id,
        Message.direction == "outbound", SMSDelivery.provider == "simulation",
    ).order_by(Message.created_at.desc(), Message.id.desc())
    # The interactive simulator needs a complete snapshot to observe older rows that
    # finish delivery later and messages delivered between refreshes.
    rows = (await session.execute(query if include_all else query.limit(50))).all()
    return {
        "conversation_id": conversation.id, "owner": conversation.owner, "waiting_for_staff": waiting,
        "outbound": [{
            "id": message.id, "body": message.body if delivery.status == "delivered" else None,
            "source": "staff" if message.employee_id else "advisor_followup" if resumed else "advisor",
            "status": delivery.status, "error_code": delivery.error_code, "delivered_at": delivery.delivered_at,
        } for message, delivery, resumed in reversed(rows)],
    }


@router.get("/simulations/sms/{conversation_id}/appointment-actions")
async def simulated_appointment_actions(
    conversation_id: UUID, dealership: DealershipTransaction, request: Request,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict:
    require_simulation(request)
    session, tenant_id = dealership
    conversation = await session.scalar(select(Conversation).where(
        Conversation.tenant_id == tenant_id, Conversation.id == conversation_id,
        Conversation.recipient_number == SIMULATED_RECIPIENT,
    ))
    if conversation is None:
        raise HTTPException(404, "Conversation not found")
    proposal = pending_proposal(conversation)
    delivery = await session.get(SMSDelivery, proposal.prompt_id) if proposal else None
    now = await session.scalar(select(func.clock_timestamp()))
    results = await session.scalars(select(AppointmentActionResult).where(
        AppointmentActionResult.tenant_id == tenant_id, AppointmentActionResult.conversation_id == conversation_id,
    ).order_by(AppointmentActionResult.created_at.desc(), AppointmentActionResult.id).limit(limit))
    return {
        "pending": {
            "proposal": proposal,
            "expires_at": conversation.pending_action_expires_at,
            "status": "expired" if conversation.pending_action_expires_at <= now else "awaiting_confirmation",
            "prompt_delivery_status": delivery.status if delivery is not None else None,
        } if proposal else None,
        "results": [{
            "proposal": row.proposal, "outcome": row.outcome, "decision_message_id": row.decision_message_id,
            "appointment_id": row.appointment_id, "reply": row.reply_body, "recorded_at": row.created_at,
            "expires_at": row.expires_at,
        } for row in results],
    }


@router.post("/simulations/sms", openapi_extra={
    "requestBody": {"required": True, "content": {"application/json": {"schema": SMSContent.model_json_schema()}}},
})
async def simulate_sms(
    dealership: DealershipTransaction, request: Request,
) -> InboundResult:
    require_simulation(request)
    try:
        sms = SMSContent.model_validate_json(await read_sms_body(request))
    except ValidationError:
        raise HTTPException(422, "Invalid SMS content") from None
    if sms.sender_phone == SIMULATED_RECIPIENT:
        raise HTTPException(422, "Sender cannot be the simulation recipient")
    session, tenant_id = dealership
    return await ingest_sms(
        session, request.app.state.settings, tenant_id,
        InboundSMS(**sms.model_dump(), provider="simulation", recipient_phone=SIMULATED_RECIPIENT),
    )


@router.get("/simulations/sms/{conversation_id}/messages")
async def simulated_history(
    conversation_id: UUID, dealership: DealershipTransaction, request: Request,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[HistoryMessage]:
    require_simulation(request)
    session, tenant_id = dealership
    if not await session.scalar(select(Conversation.id).where(
        Conversation.tenant_id == tenant_id, Conversation.id == conversation_id,
        Conversation.recipient_number == SIMULATED_RECIPIENT,
    )):
        raise HTTPException(404, "Conversation not found")
    return await read_history(session, tenant_id, conversation_id, limit)


@router.get("/simulations/sms/{conversation_id}/messages/{message_id}/agent")
async def simulated_execution(
    conversation_id: UUID, message_id: UUID, dealership: DealershipTransaction, request: Request,
) -> ExecutionView:
    require_simulation(request)
    session, tenant_id = dealership
    if not await session.scalar(select(Conversation.id).where(
        Conversation.tenant_id == tenant_id, Conversation.id == conversation_id,
        Conversation.recipient_number == SIMULATED_RECIPIENT,
    )):
        raise HTTPException(404, "Agent execution not found")
    result = await get_execution(session, tenant_id, conversation_id, message_id)
    if result is None:
        raise HTTPException(404, "Agent execution not found")
    return result


@router.get("/simulations/sms/{conversation_id}/messages/{message_id}/delivery")
async def simulated_delivery(
    conversation_id: UUID, message_id: UUID, dealership: DealershipTransaction, request: Request,
) -> DeliveryView:
    require_simulation(request)
    session, tenant_id = dealership
    row = await session.scalar(select(SMSDelivery).where(
        SMSDelivery.tenant_id == tenant_id, SMSDelivery.conversation_id == conversation_id,
        SMSDelivery.provider == "simulation", SMSDelivery.id == message_id,
    ))
    if row is None:
        raise HTTPException(404, "Delivery not found")
    return DeliveryView.model_validate(row)


@router.post("/simulations/sms/{conversation_id}/messages/{message_id}/agent")
async def process_simulated_execution(
    conversation_id: UUID, message_id: UUID, tenant_id: AuthenticatedDealership, request: Request,
) -> ExecutionView:
    require_simulation(request)
    try:
        result = await process_execution(
            request.app.state.session_factory, request.app.state.settings, tenant_id, conversation_id, message_id,
        )
    except PreviousMessagePending as exc:
        raise HTTPException(409, {
            "code": "previous_message_pending", "message_id": str(exc.inbound_id),
        }) from None
    if result is None:
        raise HTTPException(404, "Agent execution not found")
    if result.reply is not None:
        await deliver_next(
            request.app.state.session_factory, request.app.state.settings,
            {"simulation": SimulationTransport()}, delivery_id=result.reply.id,
        )
    return result
