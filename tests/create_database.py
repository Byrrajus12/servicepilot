import asyncio
import os

import asyncpg
from sqlalchemy.engine import make_url


async def create_test_database() -> None:
    url = make_url(os.environ["TEST_DATABASE_URL"])
    if url.database != "servicepilot_test":
        raise ValueError("The Compose test runner requires the servicepilot_test database")
    connection = await asyncpg.connect(
        host=url.host, port=url.port, user=url.username, password=url.password, database="postgres",
    )
    try:
        exists = await connection.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", url.database)
        if not exists:
            try:
                await connection.execute("CREATE DATABASE servicepilot_test")
            except (asyncpg.DuplicateDatabaseError, asyncpg.UniqueViolationError):
                # A second test runner can create the database after the existence check.
                exists = await connection.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", url.database)
                if not exists:
                    raise
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(create_test_database())
