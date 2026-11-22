from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from servicepilot.appointments.schemas import AppointmentResponse, CustomerResponse, VehicleResponse
from servicepilot.conversations.models import (
    ConfirmationState, Conversation, ConversationStatus, CustomerResolution, Message,
)
from servicepilot.conversations.service import resolve_customer
from servicepilot.integrations.mock_dms import MockDMSAdapter


class DealershipContext(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    name: str
    timezone: str


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

    resolution: CustomerResolution | Literal["stale"] = conversation.customer_resolution
    customer = None
    if conversation.sender_number is not None:
        resolution, customer = await resolve_customer(session, tenant_id, conversation.sender_number)
        if customer is not None and customer.id != conversation.customer_id:
            resolution, customer = "stale", None
    elif conversation.customer_id is not None:
        customer = await dms.customer(conversation.customer_id)

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
    newest = (await session.scalars(
        select(Message).where(
            Message.tenant_id == tenant_id, Message.conversation_id == conversation_id,
        ).order_by(Message.occurred_at.desc(), Message.created_at.desc(), Message.id.desc())
        .limit(history_limit)
    )).all()
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
        history=[HistoryMessage.model_validate(message) for message in reversed(newest)],
    )
