from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256, scrypt
from hmac import compare_digest
from secrets import token_bytes, token_urlsafe
from typing import Annotated

from anyio import CapacityLimiter, to_thread
from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from servicepilot.staff.models import Employee, EmployeeSession

SESSION_TTL = timedelta(hours=12)
password_jobs = CapacityLimiter(4)
bearer = HTTPBearer(auto_error=False)


def _derive(password: str, salt: bytes) -> bytes:
    return scrypt(password.encode(), salt=salt, n=2 ** 15, r=8, p=3, maxmem=64 * 1024 ** 2)


def hash_password(password: str) -> str:
    salt = token_bytes(16)
    return f"{salt.hex()}:{_derive(password, salt).hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    salt, digest = encoded.split(":") if encoded else ("0" * 32, "0" * 128)
    matches = compare_digest(_derive(password, bytes.fromhex(salt)), bytes.fromhex(digest))
    return matches and encoded is not None


def digest_token(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def unauthorized() -> HTTPException:
    return HTTPException(401, "Invalid employee credentials", headers={"WWW-Authenticate": "Bearer"})


async def new_session(session: AsyncSession, employee: Employee) -> tuple[str, datetime]:
    token = token_urlsafe(32)
    expires_at = datetime.now(UTC) + SESSION_TTL
    session.add(EmployeeSession(
        tenant_id=employee.tenant_id, employee_id=employee.id,
        token_digest=digest_token(token), expires_at=expires_at,
    ))
    return token, expires_at


async def employee_transaction(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> AsyncIterator[tuple[AsyncSession, Employee, EmployeeSession]]:
    if credentials is None or len(credentials.credentials) > 128:
        raise unauthorized()
    async with request.app.state.session_factory.begin() as session:
        result = (await session.execute(
            select(Employee, EmployeeSession).join(EmployeeSession, and_(
                EmployeeSession.employee_id == Employee.id, EmployeeSession.tenant_id == Employee.tenant_id,
            )).where(
                EmployeeSession.token_digest == digest_token(credentials.credentials),
                EmployeeSession.revoked_at.is_(None), EmployeeSession.expires_at > datetime.now(UTC),
                Employee.disabled.is_(False),
            )
        )).one_or_none()
        if result is None:
            raise unauthorized()
        yield session, result.Employee, result.EmployeeSession


EmployeeTransaction = Annotated[
    tuple[AsyncSession, Employee, EmployeeSession], Depends(employee_transaction, scope="function"),
]


async def password_matches(password: str, employee: Employee | None) -> bool:
    return await to_thread.run_sync(
        verify_password, password, employee.password_hash if employee is not None else None, limiter=password_jobs,
    )
