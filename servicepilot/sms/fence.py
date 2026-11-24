from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker


def _key(tenant_id: UUID, conversation_id: UUID):
    return func.hashtextextended(f"servicepilot:ownership:{tenant_id}:{conversation_id}", 0)


async def lock_ownership_change(session: AsyncSession, tenant_id: UUID, conversation_id: UUID) -> None:
    await session.execute(select(func.pg_advisory_xact_lock(_key(tenant_id, conversation_id))))


@asynccontextmanager
async def delivery_fence(
    sessions: async_sessionmaker[AsyncSession], tenant_id: UUID, conversation_id: UUID,
) -> AsyncIterator[AsyncConnection]:
    # This session lock linearizes provider submission with staff ownership changes.
    # AUTOCOMMIT keeps the network call outside a database transaction.
    async with sessions.kw["bind"].connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        key = _key(tenant_id, conversation_id)
        try:
            await connection.execute(select(func.pg_advisory_lock(key)))
            yield connection
        finally:
            try:
                await connection.execute(select(func.pg_advisory_unlock(key)))
            except BaseException:
                await connection.invalidate()
                raise
