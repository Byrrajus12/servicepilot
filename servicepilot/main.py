from asyncio import timeout
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from asyncpg import InterfaceError, PostgresError
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from servicepilot.appointments.routes import appointment_error, router
from servicepilot.appointments.service import AppointmentError
from servicepilot.config import Settings
from servicepilot.conversations.routes import router as sms_simulation_router
from servicepilot.db.session import build_engine, build_session_factory
from servicepilot.sms.routes import router as twilio_router
from servicepilot.staff.routes import router as staff_router


class HealthStatus(BaseModel):
    status: Literal["ok", "unavailable"]


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings or Settings()
        engine = build_engine(app.state.settings)
        app.state.engine = engine
        app.state.session_factory = build_session_factory(engine)
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="ServicePilot", lifespan=lifespan)
    app.include_router(router)
    app.include_router(sms_simulation_router)
    app.include_router(twilio_router)
    app.include_router(staff_router)
    app.add_exception_handler(AppointmentError, appointment_error)

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
