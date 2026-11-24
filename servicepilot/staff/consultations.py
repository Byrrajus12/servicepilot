from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, exists, or_, select, tuple_, update
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession

from servicepilot.agent.models import AgentExecution
from servicepilot.conversations.models import Conversation, Message
from servicepilot.sms.models import SMSDelivery
from servicepilot.staff.models import Consultation


def thread_consultations(conversation: Conversation):
    return (
        Consultation.tenant_id == conversation.tenant_id,
        Consultation.conversation_id == conversation.id,
    )


async def pending_consultation(
    session: AsyncSession, conversation: Conversation, consultation_id: UUID,
) -> Consultation | None:
    return await session.scalar(select(Consultation).where(
        *thread_consultations(conversation), Consultation.id == consultation_id,
        Consultation.ownership_version == conversation.ownership_version,
        Consultation.status.in_(("open", "answered")),
    ))


def latest_inbound(conversation: Conversation):
    return select(Message).where(
        Message.tenant_id == conversation.tenant_id, Message.conversation_id == conversation.id,
        Message.direction == "inbound",
    ).order_by(Message.created_at.desc(), Message.id.desc()).limit(1)


async def cancel_consultations(session: AsyncSession, conversation: Conversation, now: datetime) -> None:
    await session.execute(update(Consultation).where(
        *thread_consultations(conversation), Consultation.status.in_(("open", "answered")),
    ).values(status="canceled", resolved_at=now))


async def active_consultation(
    session: AsyncSession, conversation: Conversation, execution_id: UUID, source_id: UUID,
) -> Consultation | None:
    return await session.scalar(select(Consultation).where(
        *thread_consultations(conversation), Consultation.id == execution_id,
        Consultation.status == "answered", Consultation.ownership_version == conversation.ownership_version,
        Consultation.inbound_id == source_id,
    ))


def unfinished_customer_turn(tenant_id, conversation_id, *, after_id=None):
    direct = aliased(AgentExecution)
    filters = [direct.tenant_id == tenant_id, direct.conversation_id == conversation_id,
               direct.resume_source_id.is_(None),
               or_(direct.status.in_(("pending", "running")), direct.next_attempt_at.is_not(None))]
    if after_id is not None:
        message, anchor = aliased(Message), aliased(Message)
        return exists(select(direct.id).join(message, message.id == direct.id).where(
            *filters, anchor.id == after_id,
            tuple_(message.created_at, message.id) > tuple_(anchor.created_at, anchor.id),
        ))
    return exists(select(direct.id).where(*filters))


def consultation_link(delivery):
    execution = aliased(AgentExecution)
    return exists(select(execution.id).where(
        execution.tenant_id == delivery.tenant_id, execution.conversation_id == delivery.conversation_id,
        execution.reply_id == delivery.id,
        or_(and_(execution.resume_source_id.is_not(None), Consultation.id == execution.id),
            and_(execution.resume_source_id.is_(None), Consultation.execution_id == execution.id)),
    ).correlate(delivery, Consultation))


def consultation_delivery_relevant(delivery):
    linked = select(Consultation.id).where(
        Consultation.tenant_id == delivery.tenant_id, Consultation.conversation_id == delivery.conversation_id,
        consultation_link(delivery),
    ).correlate(delivery)
    # An acknowledgment can mention more than one question. Suppress the old text if any was withdrawn.
    return exists(linked) & ~exists(linked.where(Consultation.status == "canceled"))


def consultation_still_relevant():
    """Include a composed staff reply until the provider submission fence passes."""
    execution = aliased(AgentExecution)
    unsettled = exists(select(execution.id).join(SMSDelivery, SMSDelivery.id == execution.reply_id).where(
        execution.id == Consultation.id, execution.tenant_id == Consultation.tenant_id,
        execution.conversation_id == Consultation.conversation_id, execution.resume_source_id.is_not(None),
        SMSDelivery.status.in_(("pending", "sending")), SMSDelivery.submission_started.is_(False),
    ))
    return or_(Consultation.status.in_(("open", "answered")), and_(Consultation.status == "resolved", unsettled))


async def enqueue_answer(
    session: AsyncSession, consultation: Consultation, employee_id: UUID, public_fact: str, now: datetime,
) -> None:
    consultation.status = "answered"
    consultation.public_fact = public_fact
    consultation.answered_by = employee_id
    consultation.answered_at = now
    consultation.updated_at = now
    session.add(AgentExecution(
        id=consultation.id, tenant_id=consultation.tenant_id, conversation_id=consultation.conversation_id,
        resume_source_id=consultation.inbound_id, created_at=now, updated_at=now,
    ))
