from collections.abc import AsyncIterator
from hmac import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from servicepilot.tenancy.models import Tenant

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def dealership_transaction(
    request: Request, api_key: Annotated[str | None, Security(api_key_header)],
) -> AsyncIterator[tuple[AsyncSession, UUID]]:
    slug = None
    if api_key:
        provided = api_key.encode()
        for dealership, key in request.app.state.settings.dealership_api_keys.items():
            if compare_digest(provided, key.get_secret_value().encode()):
                slug = dealership

    unauthorized = HTTPException(401, "Invalid API key", headers={"WWW-Authenticate": "APIKey"})
    if slug is None:
        raise unauthorized

    async with request.app.state.session_factory.begin() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == slug))
        if tenant_id is None:
            raise unauthorized
        yield session, tenant_id


DealershipTransaction = Annotated[
    tuple[AsyncSession, UUID], Depends(dealership_transaction, scope="function")
]
