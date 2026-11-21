from asyncio import timeout
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from asyncpg import InterfaceError, PostgresError
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from servicepilot.config import Settings
from servicepilot.db.session import build_engine


class HealthStatus(BaseModel):
    status: Literal["ok", "unavailable"]


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = build_engine(settings or Settings())
        app.state.engine = engine
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="ServicePilot", lifespan=lifespan)

    @app.get("/health")
    async def health() -> HealthStatus:
        return HealthStatus(status="ok")

    @app.get("/ready", responses={503: {"model": HealthStatus}})
    async def ready(request: Request, response: Response) -> HealthStatus:
        try:
            async with timeout(5), request.app.state.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except (SQLAlchemyError, PostgresError, InterfaceError, OSError):
            response.status_code = 503
            return HealthStatus(status="unavailable")

        return HealthStatus(status="ok")

    return app


app = create_app()
