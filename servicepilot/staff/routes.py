from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator
from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from servicepilot.agent.actions import lock_conversation
from servicepilot.agent.models import AgentExecution
from servicepilot.conversations.context import build_context, current_identity
from servicepilot.conversations.models import Conversation, Message
from servicepilot.conversations.pending import retire_pending
from servicepilot.sms.delivery import enqueue_reply
from servicepilot.sms.fence import lock_ownership_change
from servicepilot.sms.models import SMSDelivery
from servicepilot.sms.policy import consent_for, is_suppressed, twilio_route_matches
from servicepilot.staff.auth import (
    EmployeeTransaction, hash_password, new_session, password_jobs, password_matches, unauthorized,
)
from servicepilot.staff.consultations import cancel_consultations, enqueue_answer, latest_inbound, thread_consultations
from servicepilot.staff.models import Consultation, Employee, EmployeeRole, EmployeeSession, OwnershipEvent, StaffNote
from servicepilot.tenancy.models import Tenant

class StaffRoute(APIRoute):
    def get_route_handler(self):
        handle = super().get_route_handler()

        async def redact_invalid_input(request: Request) -> Response:
            try:
                return await handle(request)
            except RequestValidationError as exc:
                # Invalid types can expose raw passwords and private staff text in the default 422 response.
                errors = [{key: value for key, value in error.items() if key != "input"} for error in exc.errors()]
                raise RequestValidationError(errors) from None

        return redact_invalid_input


router = APIRouter(prefix="/staff", tags=["staff"], route_class=StaffRoute)

Username = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[a-z0-9._@+-]{2,100}$")]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500, pattern=r"^[^\x00]+$")]
MessageText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1600, pattern=r"^[^\x00]+$")]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    dealership: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    username: Username
    password: SecretStr = Field(min_length=1, max_length=128)


class CreateEmployee(Input):
    username: Username
    display_name: Annotated[ShortText, Field(max_length=120)]
    password: SecretStr = Field(min_length=12, max_length=128)
    role: EmployeeRole = "advisor"


class EmployeeState(Input):
    disabled: bool


class Assign(Input):
    employee_id: UUID | None


class Ownership(Input):
    expected_version: int = Field(ge=0)


class Note(Input):
    body: MessageText


class InternalNote(Note):
    reply_to_id: UUID | None = None


class Reply(Note):
    request_id: UUID


class Answer(Input):
    public_fact: ShortText


class ServiceInformation(Input):
    name: Annotated[ShortText, Field(max_length=120)]
    description: ShortText
    minutes: int = Field(ge=1, le=720)


class ServiceSettings(Input):
    services: list[ServiceInformation] = Field(max_length=40)
    policies: dict[ShortText, ShortText] = Field(max_length=30)
    closed_dates: list[date] = Field(max_length=366)

    @field_validator("services")
    @classmethod
    def unique_services(cls, services: list[ServiceInformation]) -> list[ServiceInformation]:
        if len({service.name.casefold() for service in services}) != len(services):
            raise ValueError("Service names must be unique")
        return services


class Page:
    def __init__(
        self, limit: Annotated[int, Query(ge=1, le=50)] = 20,
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
    ):
        self.limit, self.offset = limit, offset


Pagination = Annotated[Page, Depends()]


def employee_view(employee: Employee) -> dict:
    return {key: getattr(employee, key) for key in ("id", "username", "display_name", "role", "disabled")}


def conversation_view(conversation: Conversation, waiting: bool) -> dict:
    return {
        "id": conversation.id, "status": conversation.status, "owner": conversation.owner,
        "assigned_employee_id": conversation.assigned_employee_id, "ownership_version": conversation.ownership_version,
        "waiting_for_staff": waiting, "customer_id": conversation.customer_id, "updated_at": conversation.updated_at,
    }


def record_view(item, fields: str) -> dict:
    return {key: getattr(item, key) for key in ("id", "created_at", *fields.split())}


def consultation_view(consultation: Consultation) -> dict:
    return record_view(
        consultation, "conversation_id execution_id inbound_id question status assigned_id public_fact "
        "answered_by answered_at resolved_at updated_at",
    )


def require_manager(employee: Employee) -> None:
    if employee.role != "manager":
        raise HTTPException(403, "Manager permission required")


def require_assignment(employee: Employee, assigned: UUID | None) -> None:
    if assigned not in (None, employee.id) and employee.role != "manager":
        raise HTTPException(403, "Assigned to another employee")


async def get_thread(
    session: AsyncSession, employee: Employee, conversation_id: UUID, *, write: bool = False,
) -> Conversation:
    if write:
        conversation = await lock_conversation(session, employee.tenant_id, conversation_id)
    else:
        conversation = await session.scalar(select(Conversation).where(
            Conversation.tenant_id == employee.tenant_id, Conversation.id == conversation_id,
        ))
    if conversation is None:
        raise HTTPException(404, "Conversation not found")
    if write and conversation.status != "open":
        raise HTTPException(409, "Conversation closed")
    return conversation


def waiting_filter():
    return exists().where(
        Consultation.tenant_id == Conversation.tenant_id, Consultation.conversation_id == Conversation.id,
        Consultation.status.in_(("open", "answered")),
    )


async def view_thread(session: AsyncSession, conversation: Conversation) -> dict:
    waiting = await session.scalar(select(exists().where(
        *thread_consultations(conversation), Consultation.status.in_(("open", "answered")),
    )))
    return conversation_view(conversation, waiting)


@router.post("/sessions")
async def login(body: Login, request: Request, response: Response) -> dict:
    sessions = request.app.state.session_factory
    async with sessions() as session:
        employee = await session.scalar(select(Employee).join(Tenant, Tenant.id == Employee.tenant_id).where(
            Tenant.slug == body.dealership, Employee.username == body.username,
        ))
    if not await password_matches(body.password.get_secret_value(), employee) or employee.disabled:
        raise unauthorized()
    async with sessions.begin() as session:
        current = await session.get(Employee, employee.id, with_for_update=True)
        if current is None or current.disabled or current.password_hash != employee.password_hash:
            raise unauthorized()
        token, expires_at = await new_session(session, current)
    response.headers["Cache-Control"] = "no-store"
    return {"access_token": token, "token_type": "bearer", "expires_at": expires_at}


@router.delete("/sessions/current", status_code=204)
async def logout(tx: EmployeeTransaction) -> Response:
    session, _, employee_session = tx
    employee_session.revoked_at = await session.scalar(select(func.clock_timestamp()))
    return Response(status_code=204)


@router.get("/me")
async def me(tx: EmployeeTransaction) -> dict:
    _, employee, _ = tx
    return employee_view(employee) | {"tenant_id": employee.tenant_id}


@router.get("/employees")
async def employees(tx: EmployeeTransaction, page: Pagination) -> list[dict]:
    session, employee, _ = tx
    rows = await session.scalars(select(Employee).where(
        Employee.tenant_id == employee.tenant_id,
    ).order_by(Employee.username, Employee.id).offset(page.offset).limit(page.limit))
    return [employee_view(row) for row in rows]


@router.post("/employees", status_code=201)
async def create_employee(body: CreateEmployee, tx: EmployeeTransaction) -> dict:
    session, employee, _ = tx
    require_manager(employee)
    password_hash = await to_thread.run_sync(hash_password, body.password.get_secret_value(), limiter=password_jobs)
    created = await session.scalar(insert(Employee).values(
        tenant_id=employee.tenant_id, username=body.username, display_name=body.display_name,
        password_hash=password_hash, role=body.role,
    ).on_conflict_do_nothing(index_elements=["tenant_id", "username"]).returning(Employee))
    if created is None:
        raise HTTPException(409, "Employee already exists")
    return employee_view(created)


@router.patch("/employees/{employee_id}")
async def change_employee(employee_id: UUID, body: EmployeeState, tx: EmployeeTransaction) -> dict:
    session, employee, _ = tx
    require_manager(employee)
    if employee_id == employee.id:
        raise HTTPException(409, "You cannot disable your own account")
    target = await session.scalar(select(Employee).where(
        Employee.tenant_id == employee.tenant_id, Employee.id == employee_id,
    ).with_for_update())
    if target is None:
        raise HTTPException(404, "Employee not found")
    target.disabled = body.disabled
    if body.disabled:
        await session.execute(update(EmployeeSession).where(
            EmployeeSession.tenant_id == employee.tenant_id, EmployeeSession.employee_id == target.id,
            EmployeeSession.revoked_at.is_(None),
        ).values(revoked_at=func.clock_timestamp()))
    return employee_view(target)


@router.put("/service-settings")
async def set_service_information(body: ServiceSettings, tx: EmployeeTransaction) -> ServiceSettings:
    session, employee, _ = tx
    require_manager(employee)
    dealer = await session.get_one(Tenant, employee.tenant_id, with_for_update=True)
    dealer.service_catalog = {entry.name.casefold(): entry.model_dump() for entry in body.services}
    dealer.service_settings_managed = True
    dealer.service_policies = body.policies
    dealer.closed_dates = sorted(set(body.closed_dates))
    return body


@router.get("/conversations")
async def conversations(
    tx: EmployeeTransaction, page: Pagination, owner: Literal["ai", "human"] | None = None,
    assigned_employee_id: UUID | None = None, waiting: bool | None = None,
) -> list[dict]:
    session, employee, _ = tx
    has_waiting = waiting_filter()
    query = select(Conversation, has_waiting).where(Conversation.tenant_id == employee.tenant_id)
    if owner is not None:
        query = query.where(Conversation.owner == owner)
    if assigned_employee_id is not None:
        query = query.where(Conversation.assigned_employee_id == assigned_employee_id)
    if waiting is not None:
        query = query.where(has_waiting if waiting else ~has_waiting)
    rows = await session.execute(query.order_by(
        Conversation.updated_at.desc(), Conversation.id.desc(),
    ).offset(page.offset).limit(page.limit))
    return [conversation_view(conversation, is_waiting) for conversation, is_waiting in rows]


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: UUID, tx: EmployeeTransaction) -> dict:
    session, employee, _ = tx
    conversation = await get_thread(session, employee, conversation_id)
    context = await build_context(session, employee.tenant_id, conversation_id, history_limit=1)
    return await view_thread(session, conversation) | {"context": context.operations}


@router.get("/conversations/{conversation_id}/messages")
async def messages(conversation_id: UUID, tx: EmployeeTransaction, page: Pagination) -> list[dict]:
    session, employee, _ = tx
    await get_thread(session, employee, conversation_id)
    rows = await session.execute(select(Message, SMSDelivery.status, SMSDelivery.error_code).outerjoin(
        SMSDelivery, (SMSDelivery.id == Message.id) & (SMSDelivery.tenant_id == Message.tenant_id),
    ).where(
        Message.tenant_id == employee.tenant_id, Message.conversation_id == conversation_id,
    ).order_by(Message.created_at.desc(), Message.id.desc()).offset(page.offset).limit(page.limit))
    return [
        record_view(message, "direction body occurred_at employee_id")
        | {"delivery_status": status, "delivery_error_code": error_code}
        for message, status, error_code in rows
    ]


@router.get("/conversations/{conversation_id}/notes")
async def notes(conversation_id: UUID, tx: EmployeeTransaction, page: Pagination) -> list[dict]:
    session, employee, _ = tx
    await get_thread(session, employee, conversation_id)
    rows = await session.scalars(select(StaffNote).where(
        StaffNote.tenant_id == employee.tenant_id, StaffNote.conversation_id == conversation_id,
    ).order_by(StaffNote.created_at.desc(), StaffNote.id.desc()).offset(page.offset).limit(page.limit))
    return [record_view(row, "employee_id body reply_to_id") for row in rows]


@router.post("/conversations/{conversation_id}/notes", status_code=201)
async def add_note(conversation_id: UUID, body: InternalNote, tx: EmployeeTransaction) -> dict:
    session, employee, _ = tx
    conversation = await get_thread(session, employee, conversation_id, write=True)
    require_assignment(employee, conversation.assigned_employee_id)
    if body.reply_to_id is not None and not await session.scalar(select(exists().where(
        StaffNote.tenant_id == employee.tenant_id, StaffNote.conversation_id == conversation_id,
        StaffNote.id == body.reply_to_id,
    ))):
        raise HTTPException(404, "Internal note not found")
    note = StaffNote(
        tenant_id=employee.tenant_id, conversation_id=conversation_id, employee_id=employee.id, body=body.body,
        reply_to_id=body.reply_to_id,
    )
    session.add(note)
    await session.flush()
    return record_view(note, "employee_id body reply_to_id")


@router.get("/consultations")
async def consultations(
    tx: EmployeeTransaction, page: Pagination, conversation_id: UUID | None = None,
    status: Literal["open", "answered", "resolved", "canceled"] | None = None,
) -> list[dict]:
    session, employee, _ = tx
    query = select(Consultation).where(Consultation.tenant_id == employee.tenant_id)
    if conversation_id is not None:
        await get_thread(session, employee, conversation_id)
        query = query.where(Consultation.conversation_id == conversation_id)
    if status is not None:
        query = query.where(Consultation.status == status)
    rows = await session.scalars(query.order_by(
        Consultation.created_at.desc(), Consultation.id.desc(),
    ).offset(page.offset).limit(page.limit))
    return [consultation_view(row) for row in rows]


async def consultation_for_staff(
    session: AsyncSession, employee: Employee, conversation_id: UUID, consultation_id: UUID,
) -> tuple[Conversation, Consultation]:
    conversation = await get_thread(session, employee, conversation_id, write=True)
    consultation = await session.scalar(select(Consultation).where(
        *thread_consultations(conversation), Consultation.id == consultation_id,
    ))
    if consultation is None:
        raise HTTPException(404, "Consultation not found")
    require_assignment(employee, consultation.assigned_id)
    return conversation, consultation


@router.put("/conversations/{conversation_id}/consultations/{consultation_id}/answer")
async def answer(conversation_id: UUID, consultation_id: UUID, body: Answer, tx: EmployeeTransaction) -> dict:
    session, employee, _ = tx
    conversation, consultation = await consultation_for_staff(session, employee, conversation_id, consultation_id)
    if consultation.answered_by == employee.id and consultation.public_fact == body.public_fact:
        return consultation_view(consultation)
    if (
        consultation.status != "open" or conversation.owner != "ai"
        or conversation.ownership_version != consultation.ownership_version
    ):
        raise HTTPException(409, "Consultation is no longer open")
    now = await session.scalar(select(func.clock_timestamp()))
    await enqueue_answer(session, consultation, employee.id, body.public_fact, now)
    await session.flush()
    return consultation_view(consultation)


@router.put("/conversations/{conversation_id}/consultations/{consultation_id}/cancel")
async def cancel(conversation_id: UUID, consultation_id: UUID, tx: EmployeeTransaction) -> dict:
    session, employee, _ = tx
    _, consultation = await consultation_for_staff(session, employee, conversation_id, consultation_id)
    if consultation.status == "resolved":
        raise HTTPException(409, "Consultation already resolved")
    if consultation.status != "canceled":
        now = await session.scalar(select(func.clock_timestamp()))
        consultation.status = "canceled"
        consultation.resolved_at = consultation.updated_at = now
    return consultation_view(consultation)


@router.get("/conversations/{conversation_id}/ownership-events")
async def ownership_events(conversation_id: UUID, tx: EmployeeTransaction, page: Pagination) -> list[dict]:
    session, employee, _ = tx
    await get_thread(session, employee, conversation_id)
    events = await session.scalars(select(OwnershipEvent).where(
        OwnershipEvent.tenant_id == employee.tenant_id, OwnershipEvent.conversation_id == conversation_id,
    ).order_by(OwnershipEvent.created_at.desc(), OwnershipEvent.id.desc()).offset(page.offset).limit(page.limit))
    return [record_view(event, "employee_id assigned_id previous_owner owner ownership_version") for event in events]


def audit_owner(session: AsyncSession, conversation: Conversation, employee: Employee, previous_owner: str) -> None:
    session.add(OwnershipEvent(
        tenant_id=employee.tenant_id, conversation_id=conversation.id, employee_id=employee.id,
        previous_owner=previous_owner, owner=conversation.owner, assigned_id=conversation.assigned_employee_id,
        ownership_version=conversation.ownership_version,
    ))


@router.put("/conversations/{conversation_id}/assignment")
async def assign(conversation_id: UUID, body: Assign, tx: EmployeeTransaction) -> dict:
    session, employee, _ = tx
    conversation = await get_thread(session, employee, conversation_id, write=True)
    require_assignment(employee, conversation.assigned_employee_id)
    require_assignment(employee, body.employee_id)
    if conversation.assigned_employee_id == body.employee_id:
        return await view_thread(session, conversation)
    if conversation.owner == "human" and body.employee_id is None:
        raise HTTPException(409, "Return to AI before clearing the assignment")
    if body.employee_id is not None:
        target = await session.scalar(select(Employee.id).where(
            Employee.tenant_id == employee.tenant_id, Employee.id == body.employee_id, Employee.disabled.is_(False),
        ))
        if target is None:
            raise HTTPException(404, "Active employee not found")
    conversation.assigned_employee_id = body.employee_id
    conversation.updated_at = await session.scalar(select(func.clock_timestamp()))
    await session.execute(update(Consultation).where(
        *thread_consultations(conversation), Consultation.status == "open",
    ).values(assigned_id=body.employee_id))
    audit_owner(session, conversation, employee, conversation.owner)
    return await view_thread(session, conversation)


def unfinished_work(conversation: Conversation):
    return update(AgentExecution).where(
        AgentExecution.tenant_id == conversation.tenant_id, AgentExecution.conversation_id == conversation.id,
        (AgentExecution.status.in_(("pending", "running")) | AgentExecution.next_attempt_at.is_not(None)),
    )


@router.put("/conversations/{conversation_id}/ownership/{owner}")
async def set_ownership(
    conversation_id: UUID, owner: Literal["ai", "human"], body: Ownership, tx: EmployeeTransaction,
) -> dict:
    session, employee, _ = tx
    await lock_ownership_change(session, employee.tenant_id, conversation_id)
    conversation = await get_thread(session, employee, conversation_id, write=True)
    require_assignment(employee, conversation.assigned_employee_id)
    if body.expected_version != conversation.ownership_version:
        raise HTTPException(409, "Ownership changed")
    if owner == conversation.owner:
        return await view_thread(session, conversation)
    previous = conversation.owner
    now = await session.scalar(select(func.clock_timestamp()))
    conversation.owner = owner
    conversation.ownership_version += 1
    conversation.updated_at = now
    if owner == "human":
        conversation.assigned_employee_id = employee.id
        retire_pending(session, conversation, "superseded")
        conversation.conditional_request = None
        await cancel_consultations(session, conversation, now)
        # Work with a reply that never reached a transport is still unanswered.
        # Keep its archived outbox row fenced with the previous ownership version.
        never_submitted = exists().where(
            SMSDelivery.id == AgentExecution.reply_id, SMSDelivery.tenant_id == employee.tenant_id,
            SMSDelivery.conversation_id == conversation.id, SMSDelivery.status.in_(("pending", "sending")),
            SMSDelivery.submission_started.is_(False),
        )
        await session.execute(update(AgentExecution).where(
            AgentExecution.tenant_id == employee.tenant_id, AgentExecution.conversation_id == conversation.id,
            AgentExecution.status == "completed", AgentExecution.resume_source_id.is_(None), never_submitted,
        ).values(status="pending", reply_id=None, error_code=None))
        await session.execute(unfinished_work(conversation).where(
            AgentExecution.resume_source_id.is_not(None),
        ).values(status="failed", next_attempt_at=None, lease_expires_at=None, error_code="ownership_changed"))
        await session.execute(unfinished_work(conversation).where(
            AgentExecution.resume_source_id.is_(None),
        ).values(status="pending", next_attempt_at=None, lease_expires_at=None, error_code=None))
    audit_owner(session, conversation, employee, previous)
    return await view_thread(session, conversation)


@router.post("/conversations/{conversation_id}/replies", status_code=201)
async def reply(conversation_id: UUID, body: Reply, tx: EmployeeTransaction, request: Request) -> dict:
    session, employee, _ = tx
    conversation = await get_thread(session, employee, conversation_id, write=True)
    existing = await session.scalar(select(Message).where(
        Message.tenant_id == employee.tenant_id, Message.conversation_id == conversation_id,
        Message.employee_request_id == body.request_id,
    ))
    if existing is not None:
        if existing.body != body.body or existing.employee_id != employee.id:
            raise HTTPException(409, "Reply request ID already used")
        return record_view(existing, "direction body occurred_at employee_id")
    require_assignment(employee, conversation.assigned_employee_id)
    if conversation.owner != "human":
        raise HTTPException(409, "Take ownership before sending a customer reply")
    inbound = await session.scalar(latest_inbound(conversation))
    if inbound is None or (inbound.provider != "simulation" and not inbound.provider.startswith("twilio-")):
        raise HTTPException(409, "Conversation has no usable SMS route")
    resolution, customer = await current_identity(session, conversation)
    if (resolution, customer.id if customer else None) != (conversation.customer_resolution, conversation.customer_id):
        raise HTTPException(409, "Conversation identity changed")
    consent = await consent_for(session, employee.tenant_id, inbound.provider, inbound.from_number, inbound.to_number)
    if consent is not None and consent.suppressed:
        raise HTTPException(409, "Customer has opted out of SMS")
    if inbound.provider.startswith("twilio-") and not await twilio_route_matches(
        session, request.app.state.settings, employee.tenant_id, inbound.provider, inbound.to_number, inbound.from_number,
    ):
        raise HTTPException(409, "SMS route is unavailable")
    now = await session.scalar(select(func.clock_timestamp()))
    if is_suppressed(consent, now):
        raise HTTPException(409, "Customer has opted out of SMS")
    outbound = Message(
        tenant_id=employee.tenant_id, conversation_id=conversation_id, direction="outbound",
        employee_id=employee.id, employee_request_id=body.request_id,
        from_number=inbound.to_number, to_number=inbound.from_number, provider=inbound.provider,
        body=body.body, occurred_at=now, created_at=now, updated_at=now,
    )
    session.add(outbound)
    await session.flush()
    enqueue_reply(session, outbound, ownership_version=None)
    await session.execute(unfinished_work(conversation).values(
        status="failed", next_attempt_at=None, lease_expires_at=None, error_code="addressed_by_employee",
    ))
    return record_view(outbound, "direction body occurred_at employee_id")
