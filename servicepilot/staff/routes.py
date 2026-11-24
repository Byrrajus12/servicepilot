from typing import Annotated
from uuid import UUID

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from servicepilot.staff.auth import (
    EmployeeTransaction, hash_password, new_session, password_jobs, password_matches, unauthorized,
)
from servicepilot.staff.models import Employee, EmployeeRole, EmployeeSession
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

class Page:
    def __init__(
        self, limit: Annotated[int, Query(ge=1, le=50)] = 20,
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
    ):
        self.limit, self.offset = limit, offset


Pagination = Annotated[Page, Depends()]

def employee_view(employee: Employee) -> dict:
    return {key: getattr(employee, key) for key in ("id", "username", "display_name", "role", "disabled")}

def require_manager(employee: Employee) -> None:
    if employee.role != "manager":
        raise HTTPException(403, "Manager permission required")

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
