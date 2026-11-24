from asyncio import timeout
from datetime import datetime, timedelta
from logging import getLogger
from time import monotonic
from traceback import format_tb
from uuid import UUID

from agents import MaxTurnsExceeded
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, ConfigDict
from sqlalchemy import exists, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from servicepilot.agent.actions import (
    STALE_REPLY, appointment_customer, confirmation_prompt, handle_confirmation, lock_conversation, owns_lease,
    record_proposal, recorded_action,
)
from servicepilot.agent.advisor import HISTORY_LIMIT, MissingAPIKey, ModelReply, build_model_input, run_advisor
from servicepilot.agent.confirmation import ConfirmationAttempt
from servicepilot.agent.models import AgentExecution, ExecutionStatus
from servicepilot.agent.tools import AdvisorContext
from servicepilot.config import Settings
from servicepilot.conversations.context import HistoryMessage, build_context, current_identity
from servicepilot.conversations.models import Conversation, Message
from servicepilot.conversations.pending import HANDOFF, AppointmentActionResult, PublishedProposal, pending_proposal
from servicepilot.conversations.schemas import InboundResult
from servicepilot.sms.delivery import enqueue_reply
from servicepilot.sms.policy import consent_for, is_suppressed, twilio_route_matches
from servicepilot.sms.retry import retry_delay

logger = getLogger(__name__)
UNPREPARED_ACTION_REPLY = "I couldn’t prepare that appointment change. What would you like me to check?"


class ExecutionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    status: ExecutionStatus
    attempts: int
    lease_expires_at: datetime | None
    next_attempt_at: datetime | None
    model: str | None
    duration_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    tool_calls: list[dict[str, str]]
    confirmation_attempts: list[ConfirmationAttempt]
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


def _unfinished(row):
    return or_(row.status.in_(("pending", "running")), row.next_attempt_at.is_not(None))


def _retryable(code: str) -> bool:
    return (
        code in {
            "timeout", "openai_connection", "execution_error", "openai_http_408", "openai_http_429",
            "confirmation_missing", "confirmation_invalid", "confirmation_duplicate", "confirmation_tool_error",
        }
        or code.startswith("openai_http_5")
    )


def _save_confirmation(row: AgentExecution, diagnostic: ConfirmationAttempt) -> None:
    row.confirmation_attempts = [
        entry for entry in row.confirmation_attempts if entry["attempt"] != diagnostic.attempt
    ] + [diagnostic.model_dump(mode="json")]


async def inbound_is_suppressed(session: AsyncSession, inbound: Message) -> bool:
    consent = await consent_for(session, inbound.tenant_id, inbound.provider, inbound.from_number, inbound.to_number)
    return is_suppressed(consent, inbound.created_at)


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
    *, manual: bool = False,
) -> int | None:
    conversation = await lock_conversation(session, tenant_id, conversation_id)
    target = await session.scalar(_executions(tenant_id, conversation_id).where(AgentExecution.id == inbound_id))
    if target is None or target.status == "completed":
        return None
    if conversation is None or conversation.status != "open":
        target.status = "failed"
        target.lease_expires_at = None
        target.next_attempt_at = None
        target.error_code = "conversation_closed"
        return None
    if target.status == "failed" and target.next_attempt_at is None:
        return None

    first_id = await session.scalar(
        _executions(tenant_id, conversation_id).with_only_columns(AgentExecution.id)
        .join(Message, Message.id == AgentExecution.id)
        .where(_unfinished(AgentExecution))
        .order_by(Message.created_at, Message.id).limit(1)
    )
    if first_id != inbound_id:
        raise PreviousMessagePending(first_id)
    now = await session.scalar(select(func.clock_timestamp()))
    if target.status == "running" and target.lease_expires_at > now:
        return None
    if not manual and target.next_attempt_at is not None and target.next_attempt_at > now:
        return None
    if target.attempts >= settings.agent_max_attempts:
        # Reserve one model-free recovery attempt for a business result committed before the worker crashed.
        recovery = target.status == "running" and target.attempts == settings.agent_max_attempts and (
            await session.scalar(select(AppointmentActionResult.id).where(
                AppointmentActionResult.tenant_id == tenant_id,
                AppointmentActionResult.conversation_id == conversation_id,
                AppointmentActionResult.decision_message_id == inbound_id,
                AppointmentActionResult.reply_body.is_not(None),
            )) is not None
        )
        if not recovery:
            target.status = "failed"
            target.lease_expires_at = None
            target.next_attempt_at = None
            target.error_code = "attempts_exhausted"
            return None

    target.status = "running"
    target.attempts += 1
    target.lease_expires_at = now + timedelta(seconds=settings.agent_timeout_seconds + 15)
    target.next_attempt_at = None
    target.model = settings.agent_model
    target.error_code = None
    target.duration_ms = None
    target.input_tokens = None
    target.output_tokens = None
    target.tool_calls = []
    return target.attempts


async def _finish(
    session: AsyncSession, settings: Settings, context: AdvisorContext, resolution: str | None,
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

    inbound = await session.get_one(Message, context.inbound_id)
    if await inbound_is_suppressed(session, inbound):
        error_code = "sms_suppressed"
    if reply is not None and error_code is None:
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
            prompt = None
            if proposal is not None or any(
                call["name"] == "appointment_confirmation" and call["outcome"] == "confirmation_required"
                for call in context.tool_calls
            ):
                prompt = pending_proposal(conversation)
            enqueue_reply(
                session, outbound, confirmation_for_id=prompt.prompt_id if prompt else None,
                expires_at=conversation.pending_action_expires_at if prompt else None,
            )
            row.reply_id = outbound.id
            row.status = "completed"
    else:
        row.status = "failed"
        row.error_code = error_code or "execution_error"
        if row.attempts < settings.agent_max_attempts and _retryable(row.error_code):
            row.next_attempt_at = now + retry_delay(settings.worker_retry_base_seconds, row.attempts)
    row.duration_ms = duration_ms
    row.tool_calls = context.tool_calls
    if context.confirmation is not None:
        context.confirmation.error_code = row.error_code
        _save_confirmation(row, context.confirmation)
    row.lease_expires_at = None
    if reply is not None:
        row.input_tokens = reply.input_tokens
        row.output_tokens = reply.output_tokens
    logger.info("Agent execution %s attempt %d status %s code %s",
                row.id, row.attempts, row.status, row.error_code)


async def process_execution(
    sessions: async_sessionmaker[AsyncSession], settings: Settings,
    tenant_id: UUID, conversation_id: UUID, inbound_id: UUID,
) -> ExecutionView | None:
    async with sessions.begin() as session:
        inbound = await session.get(Message, inbound_id)
        if (
            inbound is None or inbound.provider != "simulation"
            or (inbound.tenant_id, inbound.conversation_id) != (tenant_id, conversation_id)
        ):
            return None
        attempt = await _claim(session, settings, tenant_id, conversation_id, inbound_id, manual=True)
    if attempt is not None:
        await _run_attempt(sessions, settings, tenant_id, conversation_id, inbound_id, attempt)
    async with sessions() as session:
        return await get_execution(session, tenant_id, conversation_id, inbound_id)


async def process_next(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> bool:
    older_execution = aliased(AgentExecution)
    older_inbound = aliased(Message)
    no_earlier_work = ~exists(
        select(older_execution.id).join(older_inbound, older_inbound.id == older_execution.id).where(
            older_execution.tenant_id == AgentExecution.tenant_id,
            older_execution.conversation_id == AgentExecution.conversation_id,
            _unfinished(older_execution),
            tuple_(older_inbound.created_at, older_inbound.id) < tuple_(Message.created_at, Message.id),
        )
    )
    async with sessions.begin() as session:
        candidate = (await session.execute(
            select(AgentExecution.tenant_id, AgentExecution.conversation_id, AgentExecution.id)
            .join(Message, Message.id == AgentExecution.id)
            .join(Conversation, Conversation.id == AgentExecution.conversation_id)
            .where(
                or_(
                    AgentExecution.status == "pending",
                    AgentExecution.next_attempt_at <= func.now(),
                    AgentExecution.lease_expires_at <= func.now(),
                ),
                no_earlier_work,
            )
            .order_by(
                func.coalesce(AgentExecution.next_attempt_at, AgentExecution.lease_expires_at, Message.created_at),
                Message.id,
            ).limit(1).with_for_update(skip_locked=True, of=Conversation)
        )).one_or_none()
        if candidate is None:
            return False
        attempt = await _claim(session, settings, *candidate)
    if attempt is not None:
        await _run_attempt(sessions, settings, *candidate, attempt)
    return True


class InboundSuppressed(Exception):
    pass


class InboundRouteUnavailable(Exception):
    pass


async def _start_confirmation(
    session: AsyncSession, conversation: Conversation, row: AgentExecution, inbound: Message, context: AdvisorContext,
) -> tuple[str | None, str | None, PublishedProposal | None]:
    now = await session.scalar(select(func.clock_timestamp()))
    if not owns_lease(row, context.attempt, now):
        return "stale_attempt", None, None
    proposal = pending_proposal(conversation)
    previous = row.confirmation_attempts[-1] if row.confirmation_attempts else None
    recorded = await recorded_action(session, conversation, inbound) if previous else None
    if previous and recorded is None and (
        proposal is None or str(proposal.id) != previous["proposal_id"]
        or str(proposal.prompt_id) != previous["prompt_id"]
    ):
        outcome, reply = "stale_confirmation", STALE_REPLY
        proposal = None
    elif previous and recorded is None and previous["action_outcome"] == "confirmation_required":
        outcome, reply = await handle_confirmation(
            session, conversation, row, context.attempt, inbound, expected_proposal=proposal, decision="clarify",
        )
    else:
        outcome, reply = await handle_confirmation(session, conversation, row, context.attempt, inbound)
    if outcome == "stale_attempt":
        return outcome, reply, None
    if recorded is not None:
        proposal = PublishedProposal.model_validate(recorded.proposal)
    if proposal is not None or previous is not None:
        diagnostic = ConfirmationAttempt(
            attempt=context.attempt,
            proposal_id=proposal.id if proposal else UUID(previous["proposal_id"]),
            prompt_id=proposal.prompt_id if proposal else UUID(previous["prompt_id"]),
        )
        if outcome is None:
            diagnostic.interpretation = "pending"
            context.pending = proposal
        else:
            if recorded is not None or outcome == "confirmation_required":
                diagnostic.interpretation = "recovered"
            diagnostic.record_outcome(outcome)
        context.confirmation = diagnostic
        _save_confirmation(row, diagnostic)
    return outcome, reply, proposal if outcome == "revised" else None


async def _apply_confirmation(
    settings: Settings, context: AdvisorContext, diagnostic: ConfirmationAttempt,
) -> tuple[str, str | None]:
    async with context.sessions.begin() as session:
        conversation = await lock_conversation(session, context.tenant_id, context.conversation_id)
        if conversation is None or conversation.status != "open":
            diagnostic.record_outcome("conversation_closed")
            raise ConversationClosed
        row = await session.get_one(AgentExecution, context.inbound_id)
        now = await session.scalar(select(func.clock_timestamp()))
        if not owns_lease(row, context.attempt, now):
            return "stale_attempt", None
        inbound = await session.get_one(Message, context.inbound_id)
        if await inbound_is_suppressed(session, inbound):
            diagnostic.record_outcome("sms_suppressed")
            raise InboundSuppressed
        if inbound.provider.startswith("twilio-") and not await twilio_route_matches(
            session, settings, context.tenant_id, inbound.provider, inbound.to_number, inbound.from_number,
        ):
            diagnostic.record_outcome("sms_route_unavailable")
            raise InboundRouteUnavailable
        outcome, reply = await handle_confirmation(
            session, conversation, row, context.attempt, inbound,
            expected_proposal=context.pending, decision=diagnostic.decision,
        )
        committed = diagnostic.model_copy()
        committed.record_outcome(outcome)
        if outcome != "stale_attempt":
            _save_confirmation(row, committed)
    context.confirmation = committed
    return outcome, reply


async def _run_attempt(
    sessions: async_sessionmaker[AsyncSession], settings: Settings,
    tenant_id: UUID, conversation_id: UUID, inbound_id: UUID, attempt: int,
) -> None:
    context = AdvisorContext(tenant_id, conversation_id, None, sessions, inbound_id, attempt)
    resolution = None
    reply = None
    error_code = None
    started = monotonic()
    revision_of = None
    try:
        async with timeout(settings.agent_timeout_seconds):
            async with sessions.begin() as session:
                conversation = await lock_conversation(session, tenant_id, conversation_id)
                if conversation is None or conversation.status != "open":
                    raise ConversationClosed
                row = await session.get_one(AgentExecution, inbound_id)
                inbound = await session.get_one(Message, inbound_id)
                if inbound.provider.startswith("twilio-") and not await twilio_route_matches(
                    session, settings, tenant_id, inbound.provider, inbound.to_number, inbound.from_number,
                ):
                    raise InboundRouteUnavailable
                if await inbound_is_suppressed(session, inbound):
                    raise InboundSuppressed
                decision_resolution, decision_customer = await current_identity(session, conversation)
                decision_identity = (decision_resolution, decision_customer.id if decision_customer else None)
                outcome, action_reply, revision_of = await _start_confirmation(
                    session, conversation, row, inbound, context,
                )
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
            elif (source := context.pending or revision_of) is not None and source.customer_id != context.customer_id:
                error_code = "identity_changed"
                context.confirmation.record_outcome("identity_changed")
            elif outcome != "stale_attempt" and attempt <= settings.agent_max_attempts:
                reply = await run_advisor(
                    settings, context, build_model_input(snapshot, inbound_body, context.pending, revision_of=revision_of),
                )
                if context.pending is not None:
                    diagnostic = context.confirmation
                    error_code = diagnostic.interpretation_error()
                    if error_code is not None:
                        reply = None
                    else:
                        outcome, action_reply = await _apply_confirmation(settings, context, diagnostic)
                        context.tool_calls.append({"name": "appointment_confirmation", "outcome": outcome})
                        context.proposal = None
                        if outcome == "revised":
                            revision_of, context.pending = context.pending, None
                            revised = await run_advisor(
                                settings, context, build_model_input(snapshot, inbound_body, None, revision_of=revision_of),
                            )
                            reply = ModelReply(
                                revised.body, reply.input_tokens + revised.input_tokens,
                                reply.output_tokens + revised.output_tokens,
                            )
                        else:
                            reply = ModelReply(action_reply, reply.input_tokens, reply.output_tokens) if action_reply else None
    except (TimeoutError, APITimeoutError):
        error_code = "timeout"
    except MaxTurnsExceeded:
        error_code = "max_turns"
    except MissingAPIKey:
        error_code = "missing_api_key"
    except InboundSuppressed:
        error_code = "sms_suppressed"
    except InboundRouteUnavailable:
        error_code = "sms_route_unavailable"
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
        error_code = (
            "confirmation_tool_error"
            if context.confirmation is not None and context.confirmation.interpretation == "tool_error"
            else "execution_error"
        )
    async with sessions.begin() as session:
        await _finish(session, settings, context, resolution, reply, error_code, int((monotonic() - started) * 1000))
