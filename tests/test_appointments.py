import asyncio
from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, text, update

from servicepilot.appointments.service import AppointmentConflict, AppointmentService
from servicepilot.config import Settings
from servicepilot.db.models import Appointment, Customer, Tenant, Vehicle
from servicepilot.db.session import build_session_factory
from servicepilot.integrations.mock_dms import MockDMSAdapter
from servicepilot.main import create_app
from servicepilot.seed import seed_database

LOCAL_HEADERS = {"X-API-Key": "test-cedar-ridge-key"}
OTHER_HEADERS = {"X-API-Key": "test-harbor-point-key"}


@pytest.fixture
async def dealership_data(migrated_database):
    sessions = build_session_factory(migrated_database)
    async with sessions.begin() as session:
        await seed_database(session)
        dealers = (await session.scalars(select(Tenant).order_by(Tenant.slug))).all()
        local, other = dealers
        local.timezone = "Asia/Tokyo"
        local.opens_at = time(8)
        local.closes_at = time(10, 15)
        local.appointment_minutes = 30
        await session.flush()

        visits = {}
        for dealer in dealers:
            visits[dealer.slug] = (await session.scalars(
                select(Appointment).where(Appointment.tenant_id == dealer.id)
                .order_by(Appointment.external_dms_id)
            )).all()

    try:
        yield sessions, local, other, visits[local.slug], visits[other.slug]
    finally:
        async with sessions.begin() as session:
            tenant_ids = [dealer.id for dealer in dealers]
            for model in (Appointment, Vehicle, Customer):
                await session.execute(delete(model).where(model.tenant_id.in_(tenant_ids)))
            await session.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))


@pytest.fixture
async def api(database_settings, dealership_data):
    sessions, local, other, _, _ = dealership_data
    settings = Settings(
        database_url=database_settings.database_url,
        dealership_api_keys={
            local.slug: LOCAL_HEADERS["X-API-Key"],
            other.slug: OTHER_HEADERS["X-API-Key"],
        },
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        app.state.session_factory = sessions
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=LOCAL_HEADERS,
        ) as client:
            yield client


@pytest.fixture
def monday():
    today = datetime.now(UTC).date()
    day = today + timedelta(days=14 - today.weekday())
    return day + timedelta(days=7) if day == date(2030, 1, 7) else day


def booking_payload(visit, start):
    return {
        "customer_id": str(visit.customer_id),
        "vehicle_id": str(visit.vehicle_id),
        "scheduled_start": start,
        "service_type": "Oil change",
    }


async def slots(api, day, **params):
    response = await api.get("/availability", params={"date": day.isoformat(), **params})
    assert response.status_code == 200, response.text
    return response.json()


async def test_booking_rescheduling_and_cancellation(api, dealership_data, monday):
    _, _, _, local_visits, _ = dealership_data
    first, second, *_ = await slots(api, monday)
    response = await api.post("/appointments", json=booking_payload(local_visits[0], first["start"]))
    assert response.status_code == 201, response.text
    booked = response.json()
    assert booked["status"] == "scheduled"
    assert datetime.fromisoformat(booked["scheduled_start"]) == datetime.fromisoformat(first["start"])
    assert datetime.fromisoformat(booked["scheduled_start"]).utcoffset() == timedelta(0)
    assert datetime.fromisoformat(booked["scheduled_end"]) == datetime.fromisoformat(first["end"])
    assert first not in await slots(api, monday)

    path = f"/appointments/{booked['id']}"
    customer_path = f"/customers/{booked['customer_id']}/appointments"
    assert (await api.get(path)).json() == booked
    assert booked in (await api.get(customer_path)).json()

    occupied = await api.post("/appointments", json=booking_payload(local_visits[1], second["start"]))
    assert occupied.status_code == 201
    assert (await api.patch(path, json={"scheduled_start": second["start"]})).status_code == 409
    assert (await api.get(path)).json() == booked

    assert (await api.post(f"/appointments/{occupied.json()['id']}/cancel")).status_code == 200
    moved = await api.patch(path, json={"scheduled_start": second["start"]})
    assert moved.status_code == 200, moved.text
    assert moved.json()["id"] == booked["id"]
    assert datetime.fromisoformat(moved.json()["scheduled_start"]) == datetime.fromisoformat(second["start"])
    assert first in await slots(api, monday)
    assert second not in await slots(api, monday)

    cancelled = await api.post(f"{path}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert second in await slots(api, monday)
    assert booked["id"] not in {visit["id"] for visit in (await api.get(customer_path)).json()}
    assert (await api.post(f"{path}/cancel")).status_code == 409
    assert (await api.patch(path, json={"scheduled_start": first["start"]})).status_code == 409
    assert (await api.post("/appointments", json=booking_payload(local_visits[2], second["start"]))).status_code == 201


async def test_scheduling_boundaries_and_local_timezone(api, dealership_data, monday):
    _, local, _, visits, _ = dealership_data
    options = await slots(api, monday)
    starts = [datetime.fromisoformat(slot["start"]) for slot in options]
    assert [start.time() for start in starts] == [time(8), time(8, 30), time(9), time(9, 30)]
    assert starts[0].astimezone(UTC).date() == monday - timedelta(days=1)
    assert datetime.fromisoformat(options[-1]["end"]).time() == time(10)
    assert await slots(api, monday - timedelta(days=1)) == []
    assert await slots(api, date(2000, 1, 3)) == []

    switch = await api.get("/availability", headers=OTHER_HEADERS, params={"date": monday.isoformat()})
    assert switch.status_code == 200
    assert len(switch.json()) == 9
    assert datetime.fromisoformat(switch.json()[0]["start"]).utcoffset() != starts[0].utcoffset()

    invalid_starts = (
        starts[0] - timedelta(minutes=30),
        starts[0] + timedelta(minutes=1),
        starts[-1] + timedelta(minutes=30),
        starts[0] - timedelta(days=1),
        datetime(2000, 1, 3, 8, tzinfo=ZoneInfo(local.timezone)),
        starts[0].replace(tzinfo=None),
    )
    for start in invalid_starts:
        result = await api.post("/appointments", json=booking_payload(visits[0], start.isoformat()))
        assert result.status_code == 422, result.text

    payload = booking_payload(visits[0], starts[0].astimezone(UTC).isoformat())
    assert (await api.post("/appointments", json=payload)).status_code == 201


async def test_tenant_authorization_and_customer_vehicle_ownership(api, dealership_data, monday):
    _, _, other, local, foreign = dealership_data
    request = api.build_request("GET", "/customers", params={"phone_number": "+12065550101"})
    del request.headers["X-API-Key"]
    assert (await api.send(request)).status_code == 401
    assert (await api.get("/customers", headers={"X-API-Key": "wrong"})).status_code == 401
    phone = {"phone_number": "+13135550101", "tenant_id": str(other.id)}
    assert (await api.get("/customers", params=phone)).json() == []
    foreign_customers = (await api.get("/customers", headers=OTHER_HEADERS, params=phone)).json()
    assert [customer["id"] for customer in foreign_customers] == [str(foreign[0].customer_id)]
    local_vehicles = (await api.get(f"/customers/{local[0].customer_id}/vehicles")).json()
    assert [vehicle["id"] for vehicle in local_vehicles] == [str(local[0].vehicle_id)]
    assert (await api.get(f"/customers/{foreign[0].customer_id}/vehicles")).status_code == 404
    assert (await api.get(f"/customers/{foreign[0].customer_id}/appointments")).status_code == 404
    assert (await api.get("/availability", params={
        "date": monday.isoformat(), "vehicle_id": str(foreign[0].vehicle_id),
    })).status_code == 404

    start = (await slots(api, monday))[0]["start"]
    foreign_path = f"/appointments/{foreign[0].id}"
    for path in (foreign_path, f"/appointments/{uuid4()}"):
        assert (await api.get(path)).json() == {"detail": "Appointment not found"}
        assert (await api.patch(path, json={"scheduled_start": start})).status_code == 404
        assert (await api.post(f"{path}/cancel")).status_code == 404

    assert (await api.post("/appointments", json=booking_payload(foreign[0], start))).status_code == 404
    payload = booking_payload(local[0], start)
    for vehicle_id in (foreign[0].vehicle_id, local[1].vehicle_id, uuid4()):
        response = await api.post("/appointments", json=payload | {"vehicle_id": str(vehicle_id)})
        assert response.status_code == 404
        assert response.json() == {"detail": "Vehicle not found for this customer"}
    assert (await api.post("/appointments", json=payload | {"tenant_id": str(other.id)})).status_code == 422


async def test_sunday_hours_on_daylight_saving_transition(api, dealership_data):
    sessions, dealer, _, _, _ = dealership_data
    async with sessions.begin() as session:
        await session.execute(update(Tenant).where(Tenant.id == dealer.id).values(
            timezone="America/Detroit", service_days=[6], opens_at=time(1, 30), closes_at=time(2, 30),
        ))

    november = date(datetime.now(UTC).year + 1, 11, 1)
    fallback = november + timedelta(days=(6 - november.weekday()) % 7)
    starts = [datetime.fromisoformat(slot["start"]) for slot in await slots(api, fallback)]
    assert [start.time() for start in starts] == [time(1, 30), time(2)]
    assert all(start.utcoffset() == timedelta(hours=-5) for start in starts)


async def test_completed_and_past_appointments_cannot_be_changed(api, dealership_data, monday):
    sessions, _, _, (completed, past, *_), _ = dealership_data
    old_start = datetime(2000, 1, 3, 8, tzinfo=UTC)
    async with sessions.begin() as session:
        await session.execute(update(Appointment).where(Appointment.id == completed.id).values(status="completed"))
        await session.execute(update(Appointment).where(Appointment.id == past.id).values(
            scheduled_start=old_start, scheduled_end=old_start + timedelta(hours=1),
        ))

    new_start = (await slots(api, monday))[0]["start"]
    for visit, status in ((completed, "completed"), (past, "scheduled")):
        path = f"/appointments/{visit.id}"
        assert (await api.patch(path, json={"scheduled_start": new_start})).status_code == 409
        assert (await api.post(f"{path}/cancel")).status_code == 409
        assert (await api.get(path)).json()["status"] == status
        assert (await api.get(f"/customers/{visit.customer_id}/appointments")).json() == []


async def test_capacity_counts_actual_overlap_and_prevents_vehicle_conflicts(api, dealership_data, monday):
    sessions, dealer, _, visits, _ = dealership_data
    start = datetime.combine(monday, time(8), ZoneInfo(dealer.timezone)).astimezone(UTC)
    async with sessions.begin() as session:
        await session.execute(update(Tenant).where(Tenant.id == dealer.id).values(service_capacity=2))
        # Existing shorter appointments occupy different halves of the new slot.
        for visit, offset in zip(visits, (0, 15)):
            begin = start + timedelta(minutes=offset)
            session.add(Appointment(
                tenant_id=dealer.id,
                customer_id=visit.customer_id,
                vehicle_id=visit.vehicle_id,
                scheduled_start=begin,
                scheduled_end=begin + timedelta(minutes=15),
                service_type="Legacy visit",
            ))

    first = (await slots(api, monday))[0]
    assert datetime.fromisoformat(first["start"]) == start
    assert first not in await slots(api, monday, vehicle_id=str(visits[0].vehicle_id))
    conflict = await api.post("/appointments", json=booking_payload(visits[0], first["start"]))
    assert conflict.status_code == 409
    assert (await api.post("/appointments", json=booking_payload(visits[2], first["start"]))).status_code == 201
    assert first not in await slots(api, monday)


async def test_concurrent_requests_cannot_book_the_same_capacity(dealership_data, monday):
    sessions, dealer, _, (first, second, *_), _ = dealership_data
    start = datetime.combine(monday, time(8), ZoneInfo(dealer.timezone)).astimezone(UTC)
    second_pid = asyncio.get_running_loop().create_future()

    async def competing_booking():
        async with sessions.begin() as session:
            second_pid.set_result(await session.scalar(select(func.pg_backend_pid())))
            service = AppointmentService(MockDMSAdapter(session, dealer.id))
            return await service.book(second.customer_id, second.vehicle_id, start, "Oil change")

    async with asyncio.TaskGroup() as tasks:
        async with sessions.begin() as session:
            first_pid = await session.scalar(select(func.pg_backend_pid()))
            service = AppointmentService(MockDMSAdapter(session, dealer.id))
            await service.book(first.customer_id, first.vehicle_id, start, "Oil change")

            async def competing_conflict():
                with pytest.raises(AppointmentConflict):
                    await competing_booking()

            tasks.create_task(competing_conflict())
            async with asyncio.timeout(5):
                pid = await second_pid
                while first_pid not in await session.scalar(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": pid},
                ):
                    await asyncio.sleep(0.01)

    async with sessions() as session:
        count = await session.scalar(select(func.count()).select_from(Appointment).where(
            Appointment.tenant_id == dealer.id,
            Appointment.scheduled_start == start,
            Appointment.status == "scheduled",
        ))
        assert count == 1
