from datetime import date
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
from servicepilot.conversations.context import build_context
from servicepilot.conversations.models import Conversation, Message
from servicepilot.sms.models import SMSDelivery
from servicepilot.staff.auth import (
    EmployeeTransaction, hash_password, new_session, password_jobs, password_matches, unauthorized,
)
from servicepilot.staff.consultations import thread_consultations
from servicepilot.staff.models import Consultation, Employee, EmployeeRole, EmployeeSession
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

def require_manager(employee: Employee) -> None:
    if employee.role != "manager":
        raise HTTPException(403, "Manager permission required")

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
