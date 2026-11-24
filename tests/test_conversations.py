import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from servicepilot.config import SIMULATED_RECIPIENT, Settings
from servicepilot.conversations.context import build_context
from servicepilot.conversations.schemas import InboundSMS
from servicepilot.conversations.service import persist_inbound
from servicepilot.db.models import Appointment, Conversation, Customer, Message, Tenant, Vehicle
from servicepilot.main import create_app

SHARED_PHONE = "+12065550191"
UNKNOWN_PHONE = "+12065550192"
AMBIGUOUS_PHONE = "+12065550193"
SIMULATION_PATH = "/simulations/sms"
LOCAL_HEADERS = {"X-API-Key": "test-local-key"}
FOREIGN_HEADERS = {"X-API-Key": "test-foreign-key"}


@pytest.fixture
async def sms_data(committed_tenants):
    sessions, tenant_ids = committed_tenants
    async with sessions.begin() as session:
        local = Tenant(slug=f"sms-local-{uuid4().hex}", name="SMS Local")
        foreign = Tenant(slug=f"sms-foreign-{uuid4().hex}", name="SMS Foreign")
        session.add_all([local, foreign])
        await session.flush()
        tenant_ids.extend((local.id, foreign.id))
        local_customer = Customer(tenant_id=local.id, phone_number=SHARED_PHONE, name="Local Customer")
        foreign_customer = Customer(tenant_id=foreign.id, phone_number=SHARED_PHONE, name="Foreign Customer")
        session.add_all([
            local_customer,
            foreign_customer,
            Customer(tenant_id=local.id, phone_number=AMBIGUOUS_PHONE, name="Shared A"),
            Customer(tenant_id=local.id, phone_number=AMBIGUOUS_PHONE, name="Shared B"),
        ])
        await session.flush()
        start = datetime.now(UTC) + timedelta(days=2)
        for customer, vin in ((local_customer, "1" * 17), (foreign_customer, "2" * 17)):
            vehicle = Vehicle(
                tenant_id=customer.tenant_id, customer_id=customer.id, vin=vin,
                make="Ford", model="Escape", model_year=2024,
            )
            session.add(vehicle)
            await session.flush()
            session.add(Appointment(
                tenant_id=customer.tenant_id, customer_id=customer.id, vehicle_id=vehicle.id,
                scheduled_start=start, scheduled_end=start + timedelta(hours=1), service_type="Oil change",
            ))
    return sessions, local, foreign, local_customer, foreign_customer


def simulation_app(database_settings, sms_data, *, enabled):
    sessions, local, foreign, _, _ = sms_data
    settings = Settings(
        database_url=database_settings.database_url, enable_sms_simulation=enabled,
        dealership_api_keys={
            local.slug: LOCAL_HEADERS["X-API-Key"],
            foreign.slug: FOREIGN_HEADERS["X-API-Key"],
        },
    )
    app = create_app(settings)
    app.state.settings = settings
    app.state.session_factory = sessions
    return app


@pytest.fixture
async def sms_api(database_settings, sms_data):
    app = simulation_app(database_settings, sms_data, enabled=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def simulate(api, *, headers=LOCAL_HEADERS, phone=SHARED_PHONE, body="Can I book service?", **fields):
    response = await api.post(
        SIMULATION_PATH, headers=headers, json={"sender_phone": phone, "body": body, **fields},
    )
    assert response.status_code == 200, response.text
    return response.json()


def inbound(body, *, sender=SHARED_PHONE, provider="fixture", recipient=SIMULATED_RECIPIENT, **fields):
    return InboundSMS(
        sender_phone=sender, recipient_phone=recipient, body=body, provider=provider, **fields,
    )


async def test_simulation_auth_and_input_validation(sms_api, sms_data):
    _, _, foreign, _, foreign_customer = sms_data
    payload = {"sender_phone": SHARED_PHONE, "body": "Can I book service?"}
    assert (await sms_api.post(SIMULATION_PATH, json=payload)).status_code == 401
    assert (await sms_api.post(
        SIMULATION_PATH, headers={"X-API-Key": "wrong"}, json=payload,
    )).status_code == 401
    for invalid in (
        {"tenant_id": str(foreign.id)}, {"customer_id": str(foreign_customer.id)},
        {"recipient_phone": UNKNOWN_PHONE}, {"body": "   "}, {"body": "bad\x00body"},
        {"sender_phone": "2065550191"}, {"external_message_id": "bad\x00id"},
    ):
        response = await sms_api.post(SIMULATION_PATH, headers=LOCAL_HEADERS, json=payload | invalid)
        assert response.status_code == 422, response.text


async def test_simulation_requires_opt_in(database_settings, sms_data):
    app = simulation_app(database_settings, sms_data, enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            SIMULATION_PATH, headers=LOCAL_HEADERS, json={"sender_phone": SHARED_PHONE, "body": "Hello"},
        )
    assert response.status_code == 404


async def test_same_sender_and_provider_id_are_isolated_across_dealerships(sms_api, sms_data):
    sessions, local, foreign, local_customer, foreign_customer = sms_data
    first = await simulate(sms_api, body="Local history", external_message_id="same-id")
    second = await simulate(
        sms_api, headers=FOREIGN_HEADERS, body="Foreign history", external_message_id="same-id",
    )
    assert first["status"] == second["status"] == "persisted"
    assert first["conversation_id"] != second["conversation_id"]
    async with sessions() as session:
        for dealership, customer, receipt, expected_vin, body in (
            (local, local_customer, first, "1" * 17, "Local history"),
            (foreign, foreign_customer, second, "2" * 17, "Foreign history"),
        ):
            context = await build_context(session, dealership.id, UUID(receipt["conversation_id"]))
            assert context is not None
            assert context.operations.dealership.id == dealership.id
            assert context.operations.customer_resolution == "resolved"
            assert context.operations.customer.id == customer.id
            assert [vehicle.vin for vehicle in context.operations.vehicles] == [expected_vin]
            assert [item.customer_id for item in context.operations.upcoming_appointments] == [customer.id]
            assert [message.body for message in context.history] == [body]
        assert await build_context(session, local.id, UUID(second["conversation_id"])) is None
        assert await build_context(session, foreign.id, UUID(first["conversation_id"])) is None


@pytest.mark.parametrize(("phone", "resolution"), [(AMBIGUOUS_PHONE, "ambiguous"), (UNKNOWN_PHONE, "unmatched")])
async def test_unresolved_phone_keeps_one_unassigned_thread(sms_api, sms_data, phone, resolution):
    sessions, local, _, _, _ = sms_data
    first = await simulate(sms_api, phone=phone, body="Hello")
    second = await simulate(sms_api, phone=phone, body="Hello")
    assert first["status"] == second["status"] == "persisted"
    assert first["conversation_id"] == second["conversation_id"]
    assert first["message_id"] != second["message_id"]
    async with sessions() as session:
        conversation_id = UUID(first["conversation_id"])
        conversation = await session.get(Conversation, conversation_id)
        assert conversation.customer_resolution == resolution
        assert conversation.customer_id is None
        context = await build_context(session, local.id, conversation_id)
        assert context.operations.customer_resolution == resolution
        assert context.operations.customer is None
        assert context.operations.vehicles == context.operations.upcoming_appointments == []
        assert len(context.history) == 2


async def test_message_fields_and_bounded_history_in_chronological_order(sms_data):
    sessions, local, _, _, _ = sms_data
    start = datetime.now(UTC) - timedelta(minutes=5)
    async with sessions.begin() as session:
        latest = await persist_inbound(
            session, local.id, inbound("  Third  ", external_message_id="third", occurred_at=start + timedelta(minutes=2)),
        )
        earliest = await persist_inbound(session, local.id, inbound("First", occurred_at=start))
        middle = await persist_inbound(
            session, local.id, inbound("Second", occurred_at=start + timedelta(minutes=1)),
        )
        other_line = await persist_inbound(
            session, local.id, inbound("Other line", recipient="+12065550199"),
        )
    assert latest.conversation_id == earliest.conversation_id == middle.conversation_id
    assert other_line.conversation_id != latest.conversation_id
    async with sessions() as session:
        message = await session.get(Message, latest.message_id)
        assert (message.direction, message.from_number, message.to_number) == (
            "inbound", SHARED_PHONE, SIMULATED_RECIPIENT,
        )
        assert (message.body, message.provider, message.external_message_id) == ("  Third  ", "fixture", "third")
        assert message.created_at > message.occurred_at == start + timedelta(minutes=2)
        context = await build_context(session, local.id, latest.conversation_id, history_limit=2)
        assert [message.body for message in context.history] == ["Second", "  Third  "]
        with pytest.raises(ValueError, match="history_limit"):
            await build_context(session, local.id, latest.conversation_id, history_limit=51)


async def test_duplicate_id_preserves_original_receipt_and_skips_thread_creation(sms_api, sms_data):
    sessions, local, _, _, _ = sms_data
    first = await simulate(sms_api, body="Original", external_message_id="retry")
    retry = await simulate(sms_api, phone=UNKNOWN_PHONE, body="Altered", external_message_id="retry")
    assert retry == first | {"status": "duplicate"}
    async with sessions.begin() as session:
        assert await session.scalar(
            select(func.count()).select_from(Conversation).where(Conversation.tenant_id == local.id)
        ) == 1
        original = await session.get(Message, UUID(first["message_id"]))
        assert original.body == "Original"
        other_source = await persist_inbound(
            session, local.id, inbound("Separate source", external_message_id="retry"),
        )
        assert other_source.status == "persisted"
        with pytest.raises(IntegrityError) as error:
            async with session.begin_nested():
                session.add(Message(
                    tenant_id=local.id, conversation_id=original.conversation_id, direction="inbound",
                    from_number=original.from_number, to_number=original.to_number, body="Conflict",
                    provider=original.provider, external_message_id=original.external_message_id,
                ))
                await session.flush()
        assert error.value.orig.sqlstate == "23505"


@pytest.mark.parametrize("same_id", [True, False])
async def test_concurrent_inbound_delivery_uses_one_thread(sms_api, sms_data, same_id):
    sessions, local, _, _, _ = sms_data
    first, second = await asyncio.gather(
        simulate(sms_api, external_message_id="concurrent"),
        simulate(sms_api, external_message_id="concurrent" if same_id else "different"),
    )
    assert first["conversation_id"] == second["conversation_id"]
    assert sorted([first["status"], second["status"]]) == (
        ["duplicate", "persisted"] if same_id else ["persisted", "persisted"]
    )
    assert (first["message_id"] == second["message_id"]) is same_id
    async with sessions() as session:
        assert await session.scalar(
            select(func.count()).select_from(Message).where(Message.tenant_id == local.id)
        ) == (1 if same_id else 2)


async def test_pending_action_persists_expires_and_is_unchanged_by_yes(sms_api, sms_data):
    sessions, local, foreign, local_customer, _ = sms_data
    result = await simulate(sms_api)
    conversation_id = UUID(result["conversation_id"])
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    async with sessions.begin() as session:
        appointment_id = await session.scalar(select(Appointment.id).where(
            Appointment.tenant_id == local.id, Appointment.customer_id == local_customer.id,
        ))
        conversation = await session.get(Conversation, conversation_id)
        conversation.pending_action_type = "cancel_appointment"
        conversation.pending_action_payload = {"appointment_id": str(appointment_id)}
        conversation.pending_action_expires_at = expires_at
        conversation.pending_confirmation = "awaiting_confirmation"

    await simulate(sms_api, body="Yes")
    async with sessions.begin() as session:
        assert await build_context(session, foreign.id, conversation_id) is None
        context = await build_context(session, local.id, conversation_id)
        pending = context.operations.pending_action
        assert pending.type == "cancel_appointment"
        assert pending.payload == {"appointment_id": str(appointment_id)}
        assert pending.confirmation == "awaiting_confirmation"
        assert pending.expires_at == expires_at
        assert context.operations.upcoming_appointments[0].status == "scheduled"
        conversation = await session.get(Conversation, conversation_id)
        conversation.pending_action_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    async with sessions() as session:
        context = await build_context(session, local.id, conversation_id)
        assert context.operations.pending_action.confirmation == "expired"


async def test_new_ambiguity_retires_previously_resolved_thread(sms_api, sms_data):
    sessions, local, _, _, _ = sms_data
    original = await simulate(sms_api, body="Original customer's history")
    original_id = UUID(original["conversation_id"])
    async with sessions.begin() as session:
        session.add(Customer(tenant_id=local.id, phone_number=SHARED_PHONE, name="New shared customer"))

    async with sessions() as session:
        context = await build_context(session, local.id, original_id)
        assert context.operations.customer_resolution == "ambiguous"
        assert context.operations.customer is None

    new = await simulate(sms_api, body="New message")
    assert new["conversation_id"] != original["conversation_id"]
    async with sessions() as session:
        assert (await session.get(Conversation, original_id)).status == "closed"
        context = await build_context(session, local.id, UUID(new["conversation_id"]))
        assert context.operations.customer_resolution == "ambiguous"
        assert [message.body for message in context.history] == ["New message"]
