import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from tempfile import gettempdir
from time import time

import httpx

from servicepilot.agent.execution import process_next
from servicepilot.config import Settings
from servicepilot.db.session import build_engine, build_session_factory
from servicepilot.sms.delivery import deliver_next
from servicepilot.sms.transport import SimulationTransport, TwilioTransport

logger = logging.getLogger(__name__)
HEARTBEAT = Path(gettempdir()) / "servicepilot-worker.heartbeat"


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    engine = build_engine(settings)
    sessions = build_session_factory(engine)
    HEARTBEAT.unlink(missing_ok=True)

    async def consume(work: Callable[[], Awaitable[bool]]) -> None:
        while not stop.is_set():
            worked = False
            try:
                worked = await work()
                HEARTBEAT.touch()
            except Exception as exc:
                logger.error("Worker operation failed in %s (%s)", work.__name__, type(exc).__name__)
            if not worked and not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), settings.worker_poll_seconds)
                except TimeoutError:
                    pass

    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            transports = {"simulation": SimulationTransport()}
            if settings.sms_transport == "twilio":
                transports["twilio"] = TwilioTransport(settings, client)

            async def run_agent() -> bool:
                return await process_next(sessions, settings)

            async def run_delivery() -> bool:
                return await deliver_next(sessions, settings, transports)

            delivery_slots = min(2, settings.worker_concurrency)
            logger.info("SMS worker started with %d agent slots and %d delivery slots",
                        settings.worker_concurrency, delivery_slots)
            async with asyncio.TaskGroup() as tasks:
                for _ in range(settings.worker_concurrency):
                    tasks.create_task(consume(run_agent))
                for _ in range(delivery_slots):
                    tasks.create_task(consume(run_delivery))
    finally:
        HEARTBEAT.unlink(missing_ok=True)
        await engine.dispose()
        logger.info("SMS worker stopped")


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
    await run_worker(Settings(), stop)


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--health"]:
        sys.exit(0 if HEARTBEAT.is_file() and time() - HEARTBEAT.stat().st_mtime < 180 else 1)
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
