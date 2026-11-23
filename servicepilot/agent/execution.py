from asyncio import timeout
from datetime import datetime, timedelta
from logging import getLogger
from time import monotonic
from traceback import format_tb
from uuid import UUID

from agents import MaxTurnsExceeded
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from servicepilot.agent.advisor import HISTORY_LIMIT, MissingAPIKey, ModelReply, build_model_input, run_advisor
from servicepilot.agent.actions import (
    appointment_customer, confirmation_prompt, handle_confirmation, lock_conversation, owns_lease, record_proposal,
)
from servicepilot.agent.models import AgentExecution, ExecutionStatus
from servicepilot.agent.tools import AdvisorContext
from servicepilot.config import Settings
from servicepilot.conversations.context import HistoryMessage, build_context, current_identity
from servicepilot.conversations.models import Conversation, Message
from servicepilot.conversations.pending import HANDOFF, pending_proposal
from servicepilot.conversations.schemas import InboundResult

logger = getLogger(__name__)
UNPREPARED_ACTION_REPLY = "I couldn’t prepare that appointment change. What would you like me to check?"


class ExecutionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    status: ExecutionStatus
    attempts: int
    lease_expires_at: datetime | None
    model: str | None
    duration_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    tool_calls: list[dict[str, str]]
    error_code: str | None
    reply: HistoryMessage | None = None


class ConversationClosed(Exception):
    pass


class PreviousMessagePending(Exception):
    def __init__(self, inbound_id: UUID) -> None:
        self.inbound_id = inbound_id


def enqueue(session: AsyncSession, tenant_id: UUID, inbound: InboundResult) -> None:
    if inbound.status == "persisted":
        session.add(AgentExecution(
            id=inbound.message_id, tenant_id=tenant_id, conversation_id=inbound.conversation_id,
        ))


def _executions(tenant_id: UUID, conversation_id: UUID):
    return select(AgentExecution).where(
        AgentExecution.tenant_id == tenant_id, AgentExecution.conversation_id == conversation_id,
    )


async def get_execution(
    session: AsyncSession, tenant_id: UUID, conversation_id: UUID, inbound_id: UUID,
) -> ExecutionView | None:
    row = await session.scalar(_executions(tenant_id, conversation_id).where(AgentExecution.id == inbound_id))
    if row is None:
        return None
    view = ExecutionView.model_validate(row)
    if row.reply_id is not None:
        reply = await session.get_one(Message, row.reply_id)  # The foreign key scopes the reply to this thread.
        view.reply = HistoryMessage.model_validate(reply)
    return view


async def _claim(
    session: AsyncSession, settings: Settings, tenant_id: UUID, conversation_id: UUID, inbound_id: UUID,
) -> int | None:
    conversation = await lock_conversation(session, tenant_id, conversation_id)
    target = await session.scalar(_executions(tenant_id, conversation_id).where(AgentExecution.id == inbound_id))
    if target is None or target.status == "completed":
        return None
    if conversation is None or conversation.status != "open":
        target.status = "failed"
        target.lease_expires_at = None
        target.error_code = "conversation_closed"
        return None

    first_id = await session.scalar(
        _executions(tenant_id, conversation_id).with_only_columns(AgentExecution.id)
        .join(Message, Message.id == AgentExecution.id)
        .where(AgentExecution.status != "completed")
        .order_by(Message.created_at, Message.id).limit(1)
    )
    if first_id != inbound_id:
        raise PreviousMessagePending(first_id)
    now = await session.scalar(select(func.clock_timestamp()))
    if target.status == "running" and target.lease_expires_at > now:
        return None

    target.status = "running"
    target.attempts += 1
    target.lease_expires_at = now + timedelta(seconds=settings.agent_timeout_seconds + 15)
    target.model = settings.agent_model
    target.error_code = None
    target.duration_ms = None
    target.input_tokens = None
    target.output_tokens = None
    target.tool_calls = []
    return target.attempts


async def _finish(
    session: AsyncSession, context: AdvisorContext, resolution: str | None,
    reply: ModelReply | None, error_code: str | None, duration_ms: int,
) -> None:
    conversation = await lock_conversation(session, context.tenant_id, context.conversation_id)
    row = await session.scalar(_executions(context.tenant_id, context.conversation_id).where(
        AgentExecution.id == context.inbound_id,
    ))
    now = await session.scalar(select(func.clock_timestamp()))
    # A retry gets a new attempt number. The expired worker can no longer publish a reply.
    if row is None or not owns_lease(row, context.attempt, now):
        return

    if reply is not None:
        if conversation is None or conversation.status != "open":
            error_code = "conversation_closed"
        else:
            current_resolution, customer = await current_identity(session, conversation)
            if (current_resolution, customer.id if customer else None) != (resolution, context.customer_id):
                error_code = "identity_changed"

    if reply is not None and error_code is None:
        inbound = await session.get_one(Message, context.inbound_id)
        proposal = context.proposal
        body = reply.body if reply.body is not None else UNPREPARED_ACTION_REPLY
        if proposal is not None:
            if await appointment_customer(session, conversation) != proposal.customer_id:
                body = HANDOFF
                proposal = None
            else:
                body = confirmation_prompt(proposal)
        elif any(
            call["outcome"] == "identity_required" and call["name"].startswith("propose_")
            for call in context.tool_calls
        ):
            body = HANDOFF
        if not isinstance(body, str) or not 1 <= len(body.strip()) <= 1600 or "\x00" in body:
            row.status = "failed"
            row.error_code = "invalid_reply"
        else:
            now = await session.scalar(select(func.clock_timestamp()))
            if not owns_lease(row, context.attempt, now):
                return
            outbound = Message(
                tenant_id=context.tenant_id, conversation_id=context.conversation_id, direction="outbound",
                from_number=inbound.to_number, to_number=inbound.from_number, body=body.strip(),
                provider=inbound.provider, occurred_at=now, created_at=now, updated_at=now,
            )
            session.add(outbound)
            await session.flush()
            if proposal is not None:
                # The outbound question and the pending proposal become visible in the same commit.
                record_proposal(session, conversation, proposal, inbound, outbound, now)
            row.reply_id = outbound.id
            row.status = "completed"
    else:
        row.status = "failed"
        row.error_code = error_code or "execution_error"
    row.duration_ms = duration_ms
    row.tool_calls = context.tool_calls
    row.lease_expires_at = None
    if reply is not None:
        row.input_tokens = reply.input_tokens
        row.output_tokens = reply.output_tokens


async def process_execution(
    sessions: async_sessionmaker[AsyncSession], settings: Settings,
    tenant_id: UUID, conversation_id: UUID, inbound_id: UUID,
) -> ExecutionView | None:
    async with sessions.begin() as session:
        attempt = await _claim(session, settings, tenant_id, conversation_id, inbound_id)
    if attempt is not None:
        context = AdvisorContext(tenant_id, conversation_id, None, sessions, inbound_id, attempt)
        resolution = None
        reply = None
        error_code = None
        started = monotonic()
        try:
            async with timeout(settings.agent_timeout_seconds):
                async with sessions.begin() as session:
                    conversation = await lock_conversation(session, tenant_id, conversation_id)
                    if conversation is None or conversation.status != "open":
                        raise ConversationClosed
                    row = await session.get_one(AgentExecution, inbound_id)
                    inbound = await session.get_one(Message, inbound_id)
                    decision_resolution, decision_customer = await current_identity(session, conversation)
                    decision_identity = (decision_resolution, decision_customer.id if decision_customer else None)
                    outcome, action_reply = await handle_confirmation(session, conversation, row, attempt, inbound)
                    if outcome is None:
                        context.pending = pending_proposal(conversation)
                if outcome is not None:
                    context.tool_calls.append({"name": "appointment_confirmation", "outcome": outcome})
                async with sessions() as session:
                    inbound = await session.get_one(Message, inbound_id)
                    snapshot = await build_context(
                        session, tenant_id, conversation_id, history_limit=HISTORY_LIMIT, before_inbound=inbound,
                    )
                    inbound_body = inbound.body
                if snapshot is None or snapshot.operations.status != "open":
                    raise ConversationClosed
                resolution = snapshot.operations.customer_resolution
                if snapshot.operations.customer is not None:
                    context.customer_id = snapshot.operations.customer.id
                if action_reply is not None:
                    reply = ModelReply(action_reply, 0, 0)
                    if decision_identity != (resolution, context.customer_id):
                        error_code = "identity_changed"
                elif context.pending is not None and context.pending.customer_id != context.customer_id:
                    error_code = "identity_changed"
                elif outcome != "stale_attempt":
                    reply = await run_advisor(settings, context, build_model_input(snapshot, inbound_body, context.pending))
                    if context.pending is not None:
                        async with sessions.begin() as session:
                            conversation = await lock_conversation(session, tenant_id, conversation_id)
                            if conversation is None or conversation.status != "open":
                                raise ConversationClosed
                            row = await session.get_one(AgentExecution, inbound_id)
                            inbound = await session.get_one(Message, inbound_id)
                            outcome, action_reply = await handle_confirmation(
                                session, conversation, row, attempt, inbound,
                                expected_proposal=context.pending, decision=context.decision or "clarify",
                            )
                        context.tool_calls.append({"name": "appointment_confirmation", "outcome": outcome})
                        if outcome != "superseded":
                            context.proposal = None
                            reply = ModelReply(action_reply, reply.input_tokens, reply.output_tokens) if action_reply else None
        except (TimeoutError, APITimeoutError):
            error_code = "timeout"
        except MaxTurnsExceeded:
            error_code = "max_turns"
        except MissingAPIKey:
            error_code = "missing_api_key"
        except ConversationClosed:
            error_code = "conversation_closed"
        except APIStatusError as exc:
            error_code = f"openai_http_{exc.status_code}"
        except APIConnectionError:
            error_code = "openai_connection"
        except Exception as exc:
            # Exception messages can contain provider payloads or database values.
            logger.error(
                "Unexpected agent execution failure on attempt %s (%s)\nTraceback (most recent call last):\n%s",
                attempt, type(exc).__name__, "".join(format_tb(exc.__traceback__)).rstrip(),
            )
            error_code = "execution_error"
        async with sessions.begin() as session:
            await _finish(session, context, resolution, reply, error_code, int((monotonic() - started) * 1000))
    async with sessions() as session:
        return await get_execution(session, tenant_id, conversation_id, inbound_id)
