from typing import Annotated

from fastapi import APIRouter, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints
from sqlalchemy import func, select

from servicepilot.staff.auth import EmployeeTransaction, new_session, password_matches, unauthorized
from servicepilot.staff.models import Employee
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


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    dealership: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    username: Username
    password: SecretStr = Field(min_length=1, max_length=128)

def employee_view(employee: Employee) -> dict:
    return {key: getattr(employee, key) for key in ("id", "username", "display_name", "role", "disabled")}

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
