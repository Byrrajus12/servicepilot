from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from servicepilot.conversations.models import Conversation, CustomerResolution, Message
from servicepilot.conversations.pending import retire_pending
from servicepilot.conversations.schemas import InboundResult, InboundSMS
from servicepilot.customers.models import Customer


async def resolve_customer(
    session: AsyncSession, tenant_id: UUID, phone_number: str,
) -> tuple[CustomerResolution, Customer | None]:
    matches = (await session.scalars(
        select(Customer).where(
            Customer.tenant_id == tenant_id, Customer.phone_number == phone_number,
        ).limit(2).execution_options(populate_existing=True)
    )).all()
    if len(matches) == 1:
        return "resolved", matches[0]
    if matches:
        return "ambiguous", None
    return "unmatched", None


async def persist_inbound(
    session: AsyncSession, tenant_id: UUID, sms: InboundSMS,
) -> InboundResult:
    # Serialize deduplication and thread changes in the same transaction as the message insert.
    await session.execute(select(func.pg_advisory_xact_lock(
        func.hashtextextended(f"servicepilot:inbound:{tenant_id}", 0)
    )))
    if sms.external_message_id is not None:
        existing = await session.execute(
            select(Message.id, Message.conversation_id).where(
                Message.tenant_id == tenant_id, Message.direction == "inbound",
                Message.provider == sms.provider, Message.external_message_id == sms.external_message_id,
            )
        )
        if duplicate := existing.one_or_none():
            return InboundResult(
                conversation_id=duplicate.conversation_id, message_id=duplicate.id, status="duplicate",
            )

    conversation = await session.scalar(
        select(Conversation).where(
            Conversation.tenant_id == tenant_id, Conversation.status == "open",
            Conversation.sender_number == sms.sender_phone,
            Conversation.recipient_number == sms.recipient_phone,
        ).with_for_update().execution_options(populate_existing=True)
    )
    resolution, customer = await resolve_customer(session, tenant_id, sms.sender_phone)
    customer_id = customer.id if customer else None
    if conversation is not None and (conversation.customer_resolution, conversation.customer_id) != (
        resolution, customer_id,
    ):
        retire_pending(session, conversation, "identity_required")
        if conversation.customer_id is not None:
            # A new customer association must not inherit another customer's thread or pending action.
            conversation.status = "closed"
            await session.flush()
            conversation = None
        else:
            conversation.customer_resolution = resolution
            conversation.customer_id = customer_id

    if conversation is None:
        conversation = Conversation(
            tenant_id=tenant_id, sender_number=sms.sender_phone, recipient_number=sms.recipient_phone,
            customer_resolution=resolution, customer_id=customer_id,
        )
        session.add(conversation)
        await session.flush()
    received_at = await session.scalar(select(func.clock_timestamp()))
    message = Message(
        tenant_id=tenant_id, conversation_id=conversation.id, direction="inbound",
        from_number=sms.sender_phone, to_number=sms.recipient_phone, body=sms.body,
        provider=sms.provider, external_message_id=sms.external_message_id,
        occurred_at=sms.occurred_at or received_at, created_at=received_at, updated_at=received_at,
    )
    session.add(message)
    await session.flush()
    return InboundResult(conversation_id=conversation.id, message_id=message.id, status="persisted")
