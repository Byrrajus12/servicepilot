from asyncio import timeout
from datetime import datetime, timedelta
from logging import getLogger
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import exists, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from servicepilot.config import Settings
from servicepilot.conversations.context import current_identity
from servicepilot.conversations.models import Conversation, Message
from servicepilot.conversations.pending import pending_proposal
from servicepilot.sms.models import DeliveryStatus, SMSDelivery
from servicepilot.sms.policy import consent_for, is_suppressed, twilio_channel, twilio_route_matches
from servicepilot.sms.retry import retry_delay
from servicepilot.sms.transport import (
    SEND_TIMEOUT_SECONDS, OutboundSMS, SendError, SendReceipt, SMSTransport,
)

logger = getLogger(__name__)
TERMINAL = {"delivered", "failed", "undelivered"}
PROVIDER_PROGRESS = {"sending": 0, "uncertain": 0, "accepted": 1, "sent": 2}


class DeliveryView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    provider: str
    status: DeliveryStatus
    attempts: int
    next_attempt_at: datetime | None
    lease_expires_at: datetime | None
    provider_message_id: str | None
    delivered_at: datetime | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime


def enqueue_reply(
    session: AsyncSession, message: Message, *,
    confirmation_for_id: UUID | None = None, expires_at: datetime | None = None,
) -> None:
    if message.provider == "simulation":
        provider, account = "simulation", None
    elif message.provider.startswith("twilio-"):
        provider, account = "twilio", "AC" + message.provider.removeprefix("twilio-ac")
    else:
        return
    session.add(SMSDelivery(
        id=message.id, tenant_id=message.tenant_id, conversation_id=message.conversation_id,
        provider=provider, account_sid=account, sender=message.from_number, recipient=message.to_number,
        next_attempt_at=message.created_at, created_at=message.created_at, status="pending",
        confirmation_for_id=confirmation_for_id, expires_at=expires_at,
    ))


def apply_receipt(row: SMSDelivery, receipt: SendReceipt, now: datetime) -> bool:
    if row.status not in PROVIDER_PROGRESS or row.provider_message_id not in (None, receipt.provider_id):
        return False
    status = receipt.status
    if status not in TERMINAL and PROVIDER_PROGRESS.get(status, -1) <= PROVIDER_PROGRESS[row.status]:
        return False
    row.provider_message_id = receipt.provider_id
    row.status = status
    row.lease_expires_at = None
    row.error_code = receipt.error_code
    if status == "delivered":
        row.delivered_at = now
    return True


def fail_delivery(row: SMSDelivery, code: str, *, status="failed") -> None:
    row.status = status
    row.error_code = code
    row.next_attempt_at = None
    row.lease_expires_at = None


async def _claim_delivery(
    session: AsyncSession, settings: Settings, delivery_id: UUID | None,
) -> tuple[OutboundSMS, str, int] | bool:
    previous = aliased(SMSDelivery)
    no_earlier_work = ~exists(select(previous.id).where(
        previous.tenant_id == SMSDelivery.tenant_id, previous.conversation_id == SMSDelivery.conversation_id,
        previous.status.in_(("pending", "sending")),
        tuple_(previous.created_at, previous.id) < tuple_(SMSDelivery.created_at, SMSDelivery.id),
    ))
    query = select(SMSDelivery).where(
        or_(
            (SMSDelivery.status == "pending") & (SMSDelivery.next_attempt_at <= func.now()),
            (SMSDelivery.status == "sending") & (SMSDelivery.lease_expires_at <= func.now()),
        ),
        no_earlier_work,
    )
    if delivery_id is not None:
        query = query.where(SMSDelivery.id == delivery_id, SMSDelivery.provider == "simulation")
    row = await session.scalar(query.order_by(
        func.coalesce(SMSDelivery.next_attempt_at, SMSDelivery.lease_expires_at), SMSDelivery.id,
    ).limit(1).with_for_update(skip_locked=True, of=SMSDelivery))
    if row is None:
        return False
    if row.status == "sending" and row.provider == "twilio":
        fail_delivery(row, "twilio_acceptance_unknown", status="uncertain")
        logger.warning("SMS delivery %s has unknown provider acceptance", row.id)
        return True
    if row.attempts >= settings.sms_delivery_max_attempts:
        fail_delivery(row, "attempts_exhausted")
        return True

    # Serialize the claim against an opt-out or proposal revision on the same thread.
    conversation = await session.get_one(Conversation, row.conversation_id, with_for_update=True)
    if conversation.status != "open":
        fail_delivery(row, "conversation_closed")
        return True
    resolution, customer = await current_identity(session, conversation)
    if (resolution, customer.id if customer else None) != (conversation.customer_resolution, conversation.customer_id):
        fail_delivery(row, "identity_changed")
        return True
    now = await session.scalar(select(func.clock_timestamp()))
    remaining = int((row.expires_at - now).total_seconds()) if row.expires_at is not None else 300
    if remaining < 1:
        fail_delivery(row, "prompt_expired")
        return True
    channel = "simulation" if row.account_sid is None else twilio_channel(row.account_sid)
    consent = await consent_for(session, row.tenant_id, channel, row.recipient, row.sender)
    if is_suppressed(consent, row.created_at):
        fail_delivery(row, "sms_suppressed")
        return True
    if row.provider == "twilio" and not await twilio_route_matches(
        session, settings, row.tenant_id, channel, row.sender, row.recipient,
    ):
        fail_delivery(row, "sms_route_unavailable")
        return True
    if row.confirmation_for_id is not None:
        proposal = pending_proposal(conversation)
        if proposal is None or proposal.prompt_id != row.confirmation_for_id:
            fail_delivery(row, "prompt_superseded")
            return True
    message = await session.get_one(Message, row.id)
    row.status = "sending"
    row.attempts += 1
    row.next_attempt_at = None
    row.lease_expires_at = now + timedelta(seconds=SEND_TIMEOUT_SECONDS + 15)
    return OutboundSMS(row.id, row.sender, row.recipient, message.body, min(300, remaining)), row.provider, row.attempts


async def deliver_next(
    sessions: async_sessionmaker[AsyncSession], settings: Settings, transports: dict[str, SMSTransport],
    *, delivery_id: UUID | None = None,
) -> bool:
    async with sessions.begin() as session:
        claimed = await _claim_delivery(session, settings, delivery_id)
    if isinstance(claimed, bool):
        return claimed

    sms, provider, attempt = claimed
    receipt = failure = None
    try:
        async with timeout(SEND_TIMEOUT_SECONDS):
            receipt = await transports[provider].send(sms)
    except SendError as exc:
        failure = exc
    except TimeoutError:
        failure = SendError("sms_send_timeout", "retryable" if provider == "simulation" else "uncertain")

    async with sessions.begin() as session:
        row = await session.get_one(SMSDelivery, sms.id, with_for_update=True)
        now = await session.scalar(select(func.clock_timestamp()))
        if row.status != "sending" or row.attempts != attempt or row.lease_expires_at <= now:
            return True
        if receipt is not None:
            apply_receipt(row, receipt, now)
        elif failure.kind == "retryable" and attempt < settings.sms_delivery_max_attempts:
            row.status = "pending"
            row.next_attempt_at = now + retry_delay(settings.worker_retry_base_seconds, attempt)
            row.lease_expires_at = None
            row.error_code = failure.code
        else:
            fail_delivery(row, failure.code, status="uncertain" if failure.kind == "uncertain" else "failed")
        logger.info("SMS delivery %s attempt %d status %s code %s", sms.id, attempt, row.status, row.error_code)
    return True
