from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import delete
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from servicepilot.config import Settings
from servicepilot.db.models import Tenant
from servicepilot.db.session import build_session_factory
from servicepilot.seed import seed_database


class TestDatabaseConfig(BaseSettings):
    __test__ = False
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    test_database_url: str | None = None


@pytest.fixture(scope="session")
def database_settings() -> Settings:
    if not (url := TestDatabaseConfig().test_database_url):
        pytest.fail("Set TEST_DATABASE_URL to a dedicated PostgreSQL database")
    return Settings(database_url=url)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def migrated_database(database_settings: Settings) -> AsyncIterator[AsyncEngine]:
    schema = f"test_{uuid4().hex}"
    admin_engine = create_async_engine(database_settings.database_url, poolclass=NullPool)
    engine = create_async_engine(
        database_settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        try:
            config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
            config.attributes["configure_logging"] = False

            def upgrade(sync_connection: Connection) -> None:
                config.attributes["connection"] = sync_connection
                command.upgrade(config, "head")

            async with engine.begin() as connection:
                await connection.run_sync(upgrade)
            yield engine
        finally:
            await engine.dispose()
            async with admin_engine.begin() as connection:
                await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
    finally:
        await admin_engine.dispose()


@pytest_asyncio.fixture
async def committed_tenants(
    migrated_database: AsyncEngine,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], list[UUID]]]:
    sessions = build_session_factory(migrated_database)
    tenant_ids: list[UUID] = []
    try:
        yield sessions, tenant_ids
    finally:
        if tenant_ids:
            async with sessions.begin() as session:
                # Committed test data is visible to other connections. Delete FK dependents
                # first, including deliveries that refer to a separate confirmation prompt.
                for table in reversed(Tenant.metadata.sorted_tables):
                    if "tenant_id" in table.c:
                        await session.execute(delete(table).where(table.c.tenant_id.in_(tenant_ids)))
                await session.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))


@pytest_asyncio.fixture
async def session(migrated_database: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with migrated_database.connect() as connection:
        transaction = await connection.begin()
        try:
            async with AsyncSession(
                bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
            ) as session:
                yield session
        finally:
            await transaction.rollback()


@pytest_asyncio.fixture
async def seeded_session(session: AsyncSession) -> AsyncSession:
    await seed_database(session)
    return session
