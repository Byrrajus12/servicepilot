from datetime import UTC, datetime, time
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, JsonValue
from sqlalchemy import exists, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from servicepilot.appointments.schemas import AppointmentResponse, CustomerResponse, VehicleResponse
from servicepilot.conversations.models import (
    ConfirmationState, Conversation, ConversationStatus, CustomerResolution, Message,
)
from servicepilot.conversations.service import resolve_customer
from servicepilot.customers.models import Customer
from servicepilot.integrations.mock_dms import MockDMSAdapter
from servicepilot.sms.models import SMSDelivery


class DealershipContext(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    name: str
    timezone: str
    service_days: list[int]
    opens_at: time
    closes_at: time
    appointment_minutes: int


class PendingActionContext(BaseModel):
    type: str
    payload: dict[str, JsonValue]
    expires_at: AwareDatetime
    confirmation: ConfirmationState | Literal["expired"]


class ConversationOperations(BaseModel):
    dealership: DealershipContext
    conversation_id: UUID
    status: ConversationStatus
    customer_resolution: CustomerResolution | Literal["stale"]
    customer: CustomerResponse | None
    vehicles: list[VehicleResponse]
    upcoming_appointments: list[AppointmentResponse]
    pending_action: PendingActionContext | None


class HistoryMessage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    direction: Literal["inbound", "outbound"]
    body: str
    occurred_at: AwareDatetime


class ConversationContext(BaseModel):
    operations: ConversationOperations
    history: list[HistoryMessage]


async def build_context(
    session: AsyncSession, tenant_id: UUID, conversation_id: UUID, *, history_limit: int = 20,
    before_inbound: Message | None = None,
) -> ConversationContext | None:
    if not 1 <= history_limit <= 50:
        raise ValueError("history_limit must be between 1 and 50")

    conversation = await session.scalar(
        select(Conversation).where(
            Conversation.tenant_id == tenant_id, Conversation.id == conversation_id,
        ).execution_options(populate_existing=True)
    )
    if conversation is None:
        return None

    dms = MockDMSAdapter(session, tenant_id)
    dealership = await dms.dealership()
    if dealership is None:
        return None

    resolution, customer = await current_identity(session, conversation)

    now = datetime.now(UTC)
    pending = None
    if (
        conversation.status == "open"
        and resolution == conversation.customer_resolution
        and conversation.pending_action_type is not None
        and conversation.pending_action_payload is not None
        and conversation.pending_action_expires_at is not None
        and conversation.pending_confirmation is not None
    ):
        pending = PendingActionContext(
            type=conversation.pending_action_type,
            payload=conversation.pending_action_payload,
            expires_at=conversation.pending_action_expires_at,
            confirmation=(
                "expired" if conversation.pending_action_expires_at <= now
                else conversation.pending_confirmation
            ),
        )

    vehicles = await dms.vehicles(customer.id) if customer is not None else []
    appointments = await dms.upcoming(customer.id, now) if customer is not None else []
    newest = await read_history(
        session, tenant_id, conversation_id, history_limit, before_inbound=before_inbound,
    )
    if before_inbound is not None and customer is None and conversation.customer_id is not None:
        newest = []
    return ConversationContext(
        operations=ConversationOperations(
            dealership=DealershipContext.model_validate(dealership),
            conversation_id=conversation.id,
            status=conversation.status,
            customer_resolution=resolution,
            customer=CustomerResponse.model_validate(customer) if customer is not None else None,
            vehicles=[VehicleResponse.model_validate(vehicle) for vehicle in vehicles],
            upcoming_appointments=[AppointmentResponse.model_validate(item) for item in appointments],
            pending_action=pending,
        ),
        history=newest,
    )


async def current_identity(
    session: AsyncSession, conversation: Conversation,
) -> tuple[CustomerResolution | Literal["stale"], Customer | None]:
    if conversation.sender_number is not None:
        resolution, customer = await resolve_customer(
            session, conversation.tenant_id, conversation.sender_number,
        )
        if customer is not None and customer.id != conversation.customer_id:
            return "stale", None
        return resolution, customer
    if conversation.customer_id is not None:
        customer = await MockDMSAdapter(session, conversation.tenant_id).customer(conversation.customer_id)
        return ("resolved", customer) if customer is not None else ("stale", None)
    return conversation.customer_resolution, None


async def read_history(
    session: AsyncSession, tenant_id: UUID, conversation_id: UUID, history_limit: int,
    *, before_inbound: Message | None = None,
) -> list[HistoryMessage]:
    query = select(Message).where(
        Message.tenant_id == tenant_id, Message.conversation_id == conversation_id,
    )
    if before_inbound is not None:
        outbound = Message.direction == "outbound"
        if before_inbound.provider.startswith("twilio-"):
            outbound = outbound & exists(select(SMSDelivery.id).where(
                SMSDelivery.id == Message.id, SMSDelivery.tenant_id == Message.tenant_id,
                SMSDelivery.conversation_id == Message.conversation_id,
                SMSDelivery.status == "delivered", SMSDelivery.delivered_at < before_inbound.created_at,
            ))
        query = query.where(or_(
            outbound,
            (Message.direction == "inbound")
            & (tuple_(Message.created_at, Message.id) < (before_inbound.created_at, before_inbound.id)),
        ))
    newest = (await session.scalars(
        query.order_by(Message.occurred_at.desc(), Message.created_at.desc(), Message.id.desc())
        .limit(history_limit)
    )).all()
    return [HistoryMessage.model_validate(message) for message in reversed(newest)]
