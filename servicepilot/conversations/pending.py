from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ValidationError
from sqlalchemy import (
    CheckConstraint, DateTime, ForeignKeyConstraint, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from servicepilot.conversations.models import Conversation
from servicepilot.db.base import Base, TenantOwned

ActionType = Literal["book", "reschedule", "cancel"]
ProposalDecision = Literal["confirm", "decline", "revise", "clarify", "conditional", "question", "unrelated"]
ActionOutcome = Literal[
    "succeeded", "unavailable", "appointment_changed", "declined", "expired",
    "superseded", "identity_required",
]
PROPOSAL_TTL = timedelta(minutes=15)
HANDOFF = "I couldn't match this number to a single customer account. Please contact the dealership for help with your appointment."


class Proposal(BaseModel):
    id: UUID
    kind: ActionType
    customer_id: UUID
    vehicle_id: UUID
    vehicle_label: str
    service_type: str
    dealership: str
    timezone: str
    appointment_id: UUID | None = None
    original_start: AwareDatetime | None = None
    appointment_version: AwareDatetime | None = None
    scheduled_start: AwareDatetime | None = None


class PublishedProposal(Proposal):
    request_id: UUID
    prompt_id: UUID
    published_at: AwareDatetime
    reference_required: bool = False


class ConditionalRequest(BaseModel):
    request_id: UUID
    customer_id: UUID | None
    request: str
    condition: str
    proposal: Proposal | None = None
    evidence: str | None = None


class AppointmentActionResult(TenantOwned, Base):
    __tablename__ = "appointment_action_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"], ["conversations.tenant_id", "conversations.id"],
        ),
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id", "appointment_id"],
            ["appointments.tenant_id", "appointments.customer_id", "appointments.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "decision_message_id"],
            ["messages.tenant_id", "messages.conversation_id", "messages.id"],
        ),
        UniqueConstraint("tenant_id", "conversation_id", "decision_message_id"),
        CheckConstraint(
            "outcome IN ('succeeded', 'unavailable', 'appointment_changed', 'declined', 'expired', "
            "'superseded', 'identity_required')", name="outcome",
        ),
        CheckConstraint(
            "(outcome = 'succeeded') = (appointment_id IS NOT NULL)", name="appointment",
        ),
    )

    conversation_id: Mapped[UUID]
    customer_id: Mapped[UUID]
    decision_message_id: Mapped[UUID | None]
    appointment_id: Mapped[UUID | None]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    proposal: Mapped[dict] = mapped_column(JSONB)
    outcome: Mapped[ActionOutcome] = mapped_column(String(24))
    reply_body: Mapped[str | None] = mapped_column(Text)


def pending_proposal(conversation: Conversation) -> PublishedProposal | None:
    if conversation.pending_confirmation != "awaiting_confirmation":
        return None
    try:
        proposal = PublishedProposal.model_validate(conversation.pending_action_payload)
    except ValidationError:
        # Phase 3A placeholders have no published prompt or operation identifier.
        return None
    return proposal if proposal.kind == conversation.pending_action_type else None


def retire_pending(
    session: AsyncSession, conversation: Conversation, outcome: ActionOutcome,
    *, decision_message_id: UUID | None = None, reply: str | None = None, appointment_id: UUID | None = None,
) -> None:
    proposal = pending_proposal(conversation)
    if proposal is not None:
        session.add(AppointmentActionResult(
            id=proposal.id, tenant_id=conversation.tenant_id, conversation_id=conversation.id,
            customer_id=proposal.customer_id, decision_message_id=decision_message_id,
            proposal=proposal.model_dump(mode="json"), outcome=outcome,
            reply_body=reply, appointment_id=appointment_id, expires_at=conversation.pending_action_expires_at,
        ))
    conversation.pending_action_type = None
    conversation.pending_action_payload = None
    conversation.pending_action_expires_at = None
    conversation.pending_confirmation = None
