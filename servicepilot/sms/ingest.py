from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from servicepilot.agent.execution import enqueue
from servicepilot.agent.models import AgentExecution
from servicepilot.config import Settings
from servicepilot.conversations.models import Conversation, Message
from servicepilot.conversations.pending import pending_proposal, retire_pending
from servicepilot.conversations.schemas import InboundResult, InboundSMS
from servicepilot.conversations.service import persist_inbound
from servicepilot.sms.models import SMSConsent, SMSDelivery
from servicepilot.sms.policy import consent_for, is_suppressed

STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "REVOKE", "OPTOUT"}
START_WORDS = {"START", "UNSTOP", "YES"}


async def repeated_twilio_reply(session: AsyncSession, message: Message) -> bool:
    conversation = await session.get_one(Conversation, message.conversation_id)
    if pending_proposal(conversation) is not None:
        return False
    # A rapid repeat of text already answered on the same line can be an autoresponder.
    answered = await session.scalar(select(Message.id).join(AgentExecution, AgentExecution.id == Message.id)
        .join(SMSDelivery, SMSDelivery.id == AgentExecution.reply_id).where(
            Message.tenant_id == message.tenant_id, Message.conversation_id == message.conversation_id,
            Message.direction == "inbound", Message.body == message.body,
            Message.created_at > message.created_at - timedelta(seconds=15),
            SMSDelivery.status.in_(("accepted", "sent", "delivered", "uncertain")),
        ).limit(1))
    return answered is not None


async def ingest_sms(
    session: AsyncSession, settings: Settings, tenant_id: UUID, sms: InboundSMS,
    *, provider_command: str | None = None,
) -> InboundResult:
    # The existing ingestion lock also serializes consent changes, deduplication and rate decisions.
    receipt = await persist_inbound(session, tenant_id, sms)
    if receipt.status == "duplicate":
        execution = await session.get(AgentExecution, receipt.message_id)
        return receipt.model_copy(update={"processing": "queued" if execution is not None else None})

    message = await session.get_one(Message, receipt.message_id)
    consent = await consent_for(session, tenant_id, sms.provider, sms.sender_phone, sms.recipient_phone)
    word = sms.body.strip().upper()
    command = provider_command
    if command is None:
        if word in STOP_WORDS:
            command = "STOP"
        elif word in START_WORDS and (word != "YES" or (consent and consent.suppressed)):
            command = "START"
        elif word in {"HELP", "INFO"}:
            command = "HELP"

    if command in {"STOP", "START"}:
        if consent is None:
            consent = SMSConsent(
                tenant_id=tenant_id, channel=sms.provider, sender=sms.sender_phone, recipient=sms.recipient_phone,
            )
            session.add(consent)
        consent.suppressed = command == "STOP"
        if consent.suppressed:
            consent.last_opt_out_at = message.created_at
            conversation = await session.get_one(Conversation, receipt.conversation_id)
            retire_pending(session, conversation, "superseded")
    elif command is None and not is_suppressed(consent, message.created_at):
        count = await session.scalar(select(func.count()).select_from(Message).where(
            Message.tenant_id == tenant_id, Message.conversation_id == receipt.conversation_id,
            Message.direction == "inbound", Message.created_at > message.created_at - timedelta(minutes=1),
        ))
        if count > settings.sms_messages_per_minute:
            return receipt.model_copy(update={"processing": "rate_limited"})
        if sms.provider.startswith("twilio-") and await repeated_twilio_reply(session, message):
            return receipt.model_copy(update={"processing": "loop_guard"})
        enqueue(session, tenant_id, receipt)
        return receipt

    return receipt.model_copy(update={"processing": "suppressed" if command is None else "control"})
