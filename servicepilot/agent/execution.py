from asyncio import timeout
from dataclasses import replace
from datetime import datetime, timedelta
from logging import getLogger
from time import monotonic
from traceback import format_tb
from uuid import UUID

from agents import MaxTurnsExceeded
from agents.exceptions import AgentsException
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, ConfigDict
from sqlalchemy import case, exists, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from servicepilot.agent.actions import (
    STALE_REPLY, appointment_customer, confirmation_prompt, handle_confirmation, lock_conversation, owns_lease,
    record_proposal, recorded_action,
)
from servicepilot.agent.advisor import (
    ADVISOR_PROMPT_VERSION, HISTORY_LIMIT, MissingAPIKey, ModelReply, build_model_input, run_advisor, usage_reply,
)
from servicepilot.agent.confirmation import ConfirmationAttempt
from servicepilot.agent.models import AgentExecution, ExecutionStatus
from servicepilot.agent.tools import AdvisorContext
from servicepilot.config import Settings
from servicepilot.conversations.context import HistoryMessage, build_context, current_identity
from servicepilot.conversations.models import Conversation, Message
from servicepilot.conversations.pending import (
    HANDOFF, AppointmentActionResult, ConditionalRequest, PublishedProposal, pending_proposal, retire_pending,
)
from servicepilot.conversations.schemas import InboundResult
from servicepilot.sms.delivery import enqueue_reply
from servicepilot.sms.policy import consent_for, is_suppressed, twilio_route_matches
from servicepilot.sms.retry import retry_delay
from servicepilot.staff.consultations import (
    active_consultation, consultation_still_relevant, latest_inbound, thread_consultations,
    unfinished_customer_turn,
)
from servicepilot.staff.models import Consultation

logger = getLogger(__name__)
UNPREPARED_ACTION_REPLY = "I couldn’t prepare that appointment change. What would you like me to check?"
STAFF_WAIT_REPLY = "I'll check with the service team and follow up when I have an answer."
UNVERIFIED_OPERATION_REPLY = "I couldn't verify that just now."
DIALOGUE_OUTCOMES = ("revised", "conditional", "question", "unrelated")


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


def _work_time(row, message):
    return case((row.resume_source_id.is_not(None), row.created_at), else_=message.created_at)


def _work_order(row, message):
    # Finish a leased turn first. A customer's queued message gets to decide relevance before resumption.
    return (
        case((row.status == "running", 0), (row.resume_source_id.is_(None), 1), else_=2),
        _work_time(row, message), row.id,
    )


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


def _save_advisor_attempt(
    row: AgentExecution, context: AdvisorContext, reply: ModelReply | None, duration_ms: int,
    reply_source: str | None, consultation_ids: list[UUID], *, abort: str | None = None,
) -> None:
    if any(item["attempt"] == context.attempt for item in row.advisor_attempts):
        return
    row.advisor_attempts = [*row.advisor_attempts, {
        "attempt": context.attempt, "prompt_version": ADVISOR_PROMPT_VERSION, "model": row.model,
        "tools": context.tool_calls, "tool_details": context.tool_details,
        "reply_source": reply_source if row.reply_id and not abort else None,
        "reply_id": str(row.reply_id) if row.reply_id and not abort else None, "error_code": abort or row.error_code,
        "execution_id": str(row.id), "duration_ms": duration_ms, "status": "aborted" if abort else row.status,
        "input_tokens": reply.input_tokens if reply else None, "output_tokens": reply.output_tokens if reply else None,
        "proposal_id": str(context.proposal.id) if context.proposal else None,
        "consultation_ids": [str(item) for item in consultation_ids],
        "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
    }]


async def inbound_is_suppressed(session: AsyncSession, inbound: Message) -> bool:
    consent = await consent_for(session, inbound.tenant_id, inbound.provider, inbound.from_number, inbound.to_number)
    return is_suppressed(consent, inbound.created_at)


async def unverified_commitment(
    session: AsyncSession, conversation: Conversation, context: AdvisorContext, reply: ModelReply,
) -> str | None:
    output = reply.operational
    if output is None:
        return None
    if output.appointment_commitment != "none":
        # Only the canonical proposal or confirmation outcome can speak for an appointment operation.
        return "model_authored_appointment_commitment"
    if output.staff_commitment == "new_request" and not context.consultation_questions:
        return "missing_staged_consultation"
    if output.staff_commitment == "existing_request" and not context.awaiting_staff:
        return "missing_active_consultation"
    if output.availability_checked and not any(
        item["name"] == "get_available_slots" and item["outcome"] == "ok" for item in context.tool_calls
    ):
        return "missing_successful_availability_lookup"
    return None


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
    if conversation is not None and conversation.owner != "ai":
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
        .join(Message, Message.id == AgentExecution.source_message_id)
        .where(_unfinished(AgentExecution))
        .order_by(*_work_order(AgentExecution, Message)).limit(1)
    )
    if first_id != inbound_id:
        raise PreviousMessagePending(first_id)
    now = await session.scalar(select(func.clock_timestamp()))
    if target.status == "running" and target.lease_expires_at > now:
        return None
    if target.status == "running" and not any(item["attempt"] == target.attempts for item in target.advisor_attempts):
        target.advisor_attempts = [*target.advisor_attempts, {
            "execution_id": str(target.id), "attempt": target.attempts, "model": target.model,
            "status": "aborted", "error_code": "lease_expired",
        }]
    if target.resume_source_id is not None and await session.scalar(select(
        unfinished_customer_turn(tenant_id, conversation_id),
    )):
        target.status = "pending"
        target.lease_expires_at = None
        target.next_attempt_at = None
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
            if target.resume_source_id is not None:
                consultation = await active_consultation(session, conversation, target.id, target.resume_source_id)
                if consultation is not None:
                    consultation.status = "canceled"
                    consultation.resolved_at = now
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
    if row is None or row.attempts != context.attempt:
        return
    if conversation is None or conversation.owner != "ai" or conversation.ownership_version != context.ownership_version:
        _save_advisor_attempt(row, context, reply, duration_ms, None, [], abort="ownership_changed")
        return
    if not owns_lease(row, context.attempt, now):
        _save_advisor_attempt(row, context, reply, duration_ms, None, [], abort="lease_lost")
        return
    consultation = None
    if row.resume_source_id is not None:
        consultation = await active_consultation(session, conversation, row.id, row.resume_source_id)
        if consultation is None:
            error_code = "consultation_superseded"
        elif await session.scalar(select(unfinished_customer_turn(row.tenant_id, row.conversation_id))):
            error_code = "consultation_interrupted"

    if reply is not None:
        if conversation.status != "open":
            error_code = "conversation_closed"
        else:
            current_resolution, customer = await current_identity(session, conversation)
            if (current_resolution, customer.id if customer else None) != (resolution, context.customer_id):
                error_code = "identity_changed"

    inbound = await session.get_one(Message, row.source_message_id)
    if await inbound_is_suppressed(session, inbound):
        error_code = "sms_suppressed"
    operational = reply.operational if reply else None
    consultation_ids = []
    reply_source = None
    if reply is not None and error_code is None:
        proposal = context.proposal
        body = reply.body
        reply_source = "model"
        unsupported = await unverified_commitment(session, conversation, context, reply)
        if unsupported is not None:
            context.tool_details.append({"name": "operational_commitment", "outcome": unsupported})
            body = STAFF_WAIT_REPLY if context.consultation_questions else UNVERIFIED_OPERATION_REPLY
            if consultation is not None:
                body = None
            reply_source = "unverified_operation"
        if body is None and any(
            call["name"].startswith("propose_") and call["outcome"] != "proposal_prepared"
            for call in context.tool_calls
        ):
            body = UNPREPARED_ACTION_REPLY
            reply_source = "unprepared_action"
        if context.confirmation and context.confirmation.action_outcome not in (None, *DIALOGUE_OUTCOMES):
            reply_source = "confirmation_outcome"
        refreshed_prompt = None
        if context.confirmation and context.confirmation.action_outcome == "confirmation_required":
            current = pending_proposal(conversation)
            if (current is not None and current.id == context.confirmation.proposal_id
                    and current.prompt_id == context.confirmation.prompt_id
                    and (context.pending is None or current == context.pending)):
                refreshed_prompt = current
                if context.confirmation.decision == "clarify" and context.clarification_sms:
                    body = context.clarification_sms
                    reply_source = "confirmation_clarification"
            else:
                context.confirmation.record_outcome("stale_confirmation")
                body = STALE_REPLY
        if context.consultation_questions:
            partial = (body or "").strip()
            normalized = " ".join(partial.casefold().split())
            if unsupported is None:
                reply_source = "consultation_acknowledgment"
            if (partial and len(partial) <= 1600 and "\x00" not in partial
                    and not any(question.casefold() in normalized for question in context.consultation_questions)):
                body = partial
            else:
                body = STAFF_WAIT_REPLY
        elif consultation is not None and unsupported is None:
            reply_source = "staff_resumption"
            if not reply.body and proposal is None:
                body = None

        condition = context.condition
        if operational and operational.condition and "\x00" not in operational.condition and consultation is None:
            condition = ConditionalRequest(
                request_id=inbound.id, customer_id=context.customer_id, request=inbound.body,
                condition=operational.condition.strip() or inbound.body,
                proposal=proposal or (condition.proposal if condition else None),
            )
        elif operational and operational.withdraw_condition and consultation is None:
            condition = None
        if condition is not None:
            if operational and operational.condition_evidence in context.evidence:
                condition = condition.model_copy(update={"evidence": context.evidence[operational.condition_evidence]})
            elif proposal is not None and condition.proposal is not None and (
                proposal.kind, proposal.vehicle_id, proposal.appointment_id, proposal.scheduled_start, proposal.service_type
            ) != (
                condition.proposal.kind, condition.proposal.vehicle_id, condition.proposal.appointment_id,
                condition.proposal.scheduled_start, condition.proposal.service_type,
            ):
                condition = condition.model_copy(update={"evidence": None})
            if proposal is not None:
                condition = condition.model_copy(update={"proposal": proposal})
            if condition.evidence is None:
                proposal = None
        if context.consultation_questions:
            proposal = None
        elif consultation is not None and condition is not None and not reply.verified_proposal_information:
            # Continue with the staff answer and keep the verified condition for a future fresh proposal.
            proposal = None

        if proposal is not None:
            if await appointment_customer(session, conversation) != proposal.customer_id:
                body = HANDOFF
                proposal = None
                reply_source = "identity_handoff"
            else:
                body = confirmation_prompt(proposal)
                reply_source = "proposal"
                duration = context.service_durations.get(operational.proposal_duration_for) if operational else None
                if reply.verified_proposal_information:
                    body = f"{reply.verified_proposal_information} {body}"
                    reply_source = "model_information_and_proposal"
                elif duration:
                    service, minutes = duration
                    body = f"{service} typically takes about {minutes} minutes. {body}"
                    reply_source = "duration_and_proposal"
        now = await session.scalar(select(func.clock_timestamp()))
        if not owns_lease(row, context.attempt, now):
            return
        if operational and consultation is None:
            superseded = set(operational.superseded_consultations) & {item["id"] for item in context.consultations}
            if superseded:
                consultation_ids.extend((await session.scalars(update(Consultation).where(
                    *thread_consultations(conversation), Consultation.id.in_(superseded),
                    Consultation.ownership_version == context.ownership_version, consultation_still_relevant(),
                ).values(status="canceled", resolved_at=now, updated_at=now).returning(Consultation.id))).all())
        if not isinstance(body, str) or not 1 <= len(body.strip()) <= 1600 or "\x00" in body:
            row.status = "failed"
            row.error_code = "invalid_reply"
            if condition is not None and condition.evidence is None:
                conversation.conditional_request = condition.model_dump(mode="json")
            if (consultation is not None or condition is not None) and row.attempts < settings.agent_max_attempts:
                row.next_attempt_at = now + retry_delay(settings.worker_retry_base_seconds, row.attempts)
            elif consultation is not None:
                consultation.status = "canceled"
                consultation.resolved_at = now
        else:
            conversation.conditional_request = condition.model_dump(mode="json") if condition is not None else None
            if context.consultation_questions:
                if context.confirmation is None or context.confirmation.action_outcome != "unrelated":
                    retire_pending(session, conversation, "superseded")
                created = [
                    Consultation(
                        tenant_id=row.tenant_id, conversation_id=row.conversation_id, execution_id=row.id,
                        inbound_id=inbound.id, question=question, assigned_id=conversation.assigned_employee_id,
                        condition_request_id=condition.request_id if condition and question in context.condition_questions else None,
                        ownership_version=conversation.ownership_version,
                    )
                    for question in context.consultation_questions
                ]
                session.add_all(created)
                await session.flush()
                consultation_ids.extend(item.id for item in created)
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
                conversation.conditional_request = None
            elif refreshed_prompt is not None:
                # Keep the exact change and its original expiration. A later reply must follow
                # delivery of this clarification, even if the original prompt was delivered.
                conversation.pending_action_payload = refreshed_prompt.model_copy(
                    update={"prompt_id": outbound.id},
                ).model_dump(mode="json")
            prompt = pending_proposal(conversation) if proposal is not None or refreshed_prompt is not None else None
            enqueue_reply(
                session, outbound, confirmation_for_id=prompt.prompt_id if prompt else None,
                expires_at=conversation.pending_action_expires_at if prompt else None,
                ownership_version=context.ownership_version,
                inbound_guard_id=inbound.id if consultation is not None or context.consultation_questions else None,
            )
            row.reply_id = outbound.id
            row.status = "completed"
            if consultation is not None:
                consultation.status = "resolved"
                consultation.resolved_at = now
    else:
        row.status = "failed"
        row.error_code = error_code or "execution_error"
        if row.error_code == "consultation_interrupted":
            row.status = "pending"
        elif row.attempts < settings.agent_max_attempts and _retryable(row.error_code):
            row.next_attempt_at = now + retry_delay(settings.worker_retry_base_seconds, row.attempts)
        elif consultation is not None:
            consultation.status = "canceled"
            consultation.resolved_at = now
    row.duration_ms = duration_ms
    row.tool_calls = context.tool_calls
    if consultation is not None:
        consultation_ids.append(consultation.id)
    _save_advisor_attempt(row, context, reply, duration_ms, reply_source, consultation_ids)
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
        select(older_execution.id).join(older_inbound, older_inbound.id == older_execution.source_message_id).where(
            older_execution.tenant_id == AgentExecution.tenant_id,
            older_execution.conversation_id == AgentExecution.conversation_id,
            _unfinished(older_execution),
            tuple_(*_work_order(older_execution, older_inbound)) < tuple_(*_work_order(AgentExecution, Message)),
        )
    )
    async with sessions.begin() as session:
        candidate = (await session.execute(
            select(AgentExecution.tenant_id, AgentExecution.conversation_id, AgentExecution.id)
            .join(Message, Message.id == AgentExecution.source_message_id)
            .join(Conversation, Conversation.id == AgentExecution.conversation_id)
            .where(
                Conversation.owner == "ai",
                or_(
                    AgentExecution.status == "pending",
                    AgentExecution.next_attempt_at <= func.now(),
                    AgentExecution.lease_expires_at <= func.now(),
                ),
                no_earlier_work,
            )
            .order_by(
                func.coalesce(AgentExecution.next_attempt_at, AgentExecution.lease_expires_at,
                              _work_time(AgentExecution, Message)),
                AgentExecution.id,
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
    elif previous and recorded is None and previous["action_outcome"] == "unrelated":
        outcome, reply = await handle_confirmation(
            session, conversation, row, context.attempt, inbound, expected_proposal=proposal, decision="unrelated",
        )
    else:
        outcome, reply = await handle_confirmation(session, conversation, row, context.attempt, inbound)
    if outcome == "stale_attempt":
        return outcome, reply, None
    if recorded is not None:
        proposal = PublishedProposal.model_validate(recorded.proposal)
        if outcome == "revised" and previous.get("decision") in ("conditional", "question"):
            outcome = previous["decision"]
            context.requires_fresh_consent = True
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
            if recorded is not None or outcome in ("confirmation_required", "unrelated"):
                diagnostic.interpretation = "recovered"
            if outcome in ("conditional", "question", "unrelated"):
                diagnostic.decision = outcome
            diagnostic.record_outcome(outcome)
        context.confirmation = diagnostic
        _save_confirmation(row, diagnostic)
    return outcome, reply, proposal if outcome in ("revised", "conditional", "question") else None


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
        if (conversation.owner != "ai" or conversation.ownership_version != context.ownership_version
                or not owns_lease(row, context.attempt, now)):
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
            explicit_proposal_reference=diagnostic.explicit_proposal_reference,
        )
        if outcome == "conditional":
            context.condition = ConditionalRequest(
                request_id=inbound.id, customer_id=context.pending.customer_id,
                request=inbound.body, condition=context.interpreted_condition or inbound.body, proposal=context.pending,
            )
            conversation.conditional_request = context.condition.model_dump(mode="json")
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
                if conversation is None:
                    raise ConversationClosed
                row = await session.get_one(AgentExecution, inbound_id)
                now = await session.scalar(select(func.clock_timestamp()))
                if conversation.owner != "ai" or not owns_lease(row, attempt, now):
                    return
                context.ownership_version = conversation.ownership_version
                if conversation.status != "open":
                    raise ConversationClosed
                inbound = await session.get_one(Message, row.source_message_id)
                resumed = row.resume_source_id is not None
                if resumed:
                    consultation = await active_consultation(session, conversation, row.id, inbound.id)
                    if consultation is None:
                        row.status = "failed"
                        row.error_code = "consultation_superseded"
                        row.lease_expires_at = None
                        _save_advisor_attempt(row, context, None, int((monotonic() - started) * 1000), None, [row.id])
                        return
                    context.staff_fact = consultation.public_fact
                rows = (await session.execute(select(Consultation, Message.body)
                    .join(Message, Message.id == Consultation.inbound_id).where(
                        *thread_consultations(conversation), Consultation.id != row.id,
                        Consultation.ownership_version == conversation.ownership_version,
                        consultation_still_relevant(),
                    ).order_by(Consultation.created_at, Consultation.id).limit(10))).all()
                context.consultations = [
                    {"id": item.id, "customer_request": body[:500], "staff_question": item.question,
                     "status": item.status} for item, body in rows
                ]
                resolved = (await session.scalars(select(Consultation).where(
                    *thread_consultations(conversation), Consultation.ownership_version == conversation.ownership_version,
                    Consultation.status == "resolved", Consultation.public_fact.is_not(None),
                    Consultation.answered_at >= now - timedelta(hours=24),
                ).order_by(Consultation.answered_at.desc(), Consultation.id).limit(10))).all()
                active = [item for item, _ in rows if item.status in ("open", "answered")]
                for item in active + resolved:
                    context.consultation_history.setdefault(" ".join(item.question.casefold().split()), {
                        "id": item.id, "status": item.status, "public_fact": item.public_fact,
                        "answered_at": item.answered_at, "condition_request_id": item.condition_request_id,
                    })
                context.awaiting_staff = any(item.status in ("open", "answered") for item, _ in rows)
                if inbound.provider.startswith("twilio-") and not await twilio_route_matches(
                    session, settings, tenant_id, inbound.provider, inbound.to_number, inbound.from_number,
                ):
                    raise InboundRouteUnavailable
                if await inbound_is_suppressed(session, inbound):
                    raise InboundSuppressed
                decision_resolution, decision_customer = await current_identity(session, conversation)
                decision_identity = (decision_resolution, decision_customer.id if decision_customer else None)
                if conversation.customer_id is not None and decision_customer is None:
                    context.consultations.clear()
                    context.consultation_history.clear()
                    context.awaiting_staff = False
                    if resumed:
                        row.status, row.error_code, row.lease_expires_at = "failed", "identity_changed", None
                        consultation.status, consultation.resolved_at = "canceled", now
                        _save_advisor_attempt(row, context, None, int((monotonic() - started) * 1000), None, [row.id])
                        return
                if conversation.conditional_request is not None:
                    condition = ConditionalRequest.model_validate(conversation.conditional_request)
                    if condition.customer_id == decision_identity[1]:
                        context.condition = condition
                        if resumed and consultation.condition_request_id == condition.request_id and context.staff_fact:
                            context.evidence["staff_fact"] = context.staff_fact
                for item in context.consultation_history.values():
                    item["matches_condition"] = bool(
                        context.condition and item["condition_request_id"] == context.condition.request_id
                        and item["status"] in ("open", "answered")
                    )
                if row.resume_source_id is None:
                    outcome, action_reply, revision_of = await _start_confirmation(
                        session, conversation, row, inbound, context,
                    )
                else:
                    # A staff fact is information, never a customer confirmation.
                    outcome, action_reply = None, None
                    context.requires_fresh_consent = pending_proposal(conversation) is not None
                    original = await recorded_action(session, conversation, inbound)
                    if original is not None and original.outcome == "superseded":
                        revision_of = PublishedProposal.model_validate(original.proposal)
                source_message_id = inbound.id
                history_anchor_id = await session.scalar(
                    latest_inbound(conversation).with_only_columns(Message.id),
                ) if resumed else inbound.id
            if outcome is not None:
                context.tool_calls.append({"name": "appointment_confirmation", "outcome": outcome})
            async with sessions() as session:
                inbound = await session.get_one(Message, source_message_id)
                anchor = inbound if history_anchor_id == source_message_id else await session.get_one(Message, history_anchor_id)
                snapshot = await build_context(
                    session, tenant_id, conversation_id, history_limit=HISTORY_LIMIT,
                    before_inbound=anchor,
                )
                if resumed and anchor.id != inbound.id:
                    context.resume_latest_sms = anchor.body[:1600]
                inbound_body = inbound.body
                received_at = inbound.occurred_at
            if snapshot is None or snapshot.operations.status != "open":
                raise ConversationClosed
            resolution = snapshot.operations.customer_resolution
            if snapshot.operations.customer is not None:
                context.customer_id = snapshot.operations.customer.id
            if (context.staff_fact is not None or context.condition is not None or context.consultation_history
                    ) and decision_identity != (resolution, context.customer_id):
                error_code = "identity_changed"
            elif action_reply is not None:
                reply = ModelReply(action_reply, 0, 0)
                if decision_identity != (resolution, context.customer_id):
                    error_code = "identity_changed"
            elif (source := context.pending or revision_of) is not None and source.customer_id != context.customer_id:
                error_code = "identity_changed"
                if context.confirmation is not None:
                    context.confirmation.record_outcome("identity_changed")
            elif outcome != "stale_attempt" and attempt <= settings.agent_max_attempts:
                if outcome == "unrelated":
                    context.requires_fresh_consent = True
                reply = await run_advisor(
                    settings, context, build_model_input(
                        snapshot, inbound_body, context.pending, received_at=received_at,
                        revision_of=revision_of, advisor=context,
                    ),
                )
                if context.pending is not None:
                    diagnostic = context.confirmation
                    error_code = diagnostic.interpretation_error()
                    if error_code is None:
                        outcome, action_reply = await _apply_confirmation(settings, context, diagnostic)
                        context.tool_calls.append({"name": "appointment_confirmation", "outcome": outcome})
                        context.proposal = None
                        if outcome in DIALOGUE_OUTCOMES:
                            revision_of = context.pending if outcome != "unrelated" else None
                            context.pending = None
                            context.requires_fresh_consent = outcome != "revised"
                            revised = await run_advisor(
                                settings, context, build_model_input(
                                    snapshot, inbound_body, None, received_at=received_at,
                                    revision_of=revision_of, advisor=context,
                                ),
                            )
                            reply = revised.with_prior_usage(reply)
                        else:
                            reply = replace(reply, body=action_reply)
                            if action_reply is None:
                                error_code = "execution_error"
    except (TimeoutError, APITimeoutError):
        error_code = "timeout"
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
        if isinstance(exc, AgentsException) and exc.run_data is not None:
            measured = usage_reply(exc.run_data)
            reply = measured.with_prior_usage(reply) if reply else measured
        if isinstance(exc, MaxTurnsExceeded):
            error_code = "max_turns"
        else:
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
