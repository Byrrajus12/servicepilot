from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from servicepilot.db.models import (
    Appointment,
    Conversation,
    Customer,
    IntegrationEvent,
    Message,
    Vehicle,
    Workflow,
)


async def two_appointments(session):
    return (await session.scalars(
        select(Appointment)
        .where(Appointment.external_dms_id == "DEMO-001")
        .order_by(Appointment.tenant_id)
    )).all()


async def test_updated_at_advances_within_one_transaction(seeded_session):
    session = seeded_session
    appointment, _ = await two_appointments(session)
    created_at = appointment.created_at
    previous_update = appointment.updated_at
    assert previous_update == created_at

    for status in ("completed", "cancelled"):
        await session.execute(select(func.pg_sleep(0.01)))
        appointment.status = status
        await session.flush()
        await session.refresh(appointment)
        assert appointment.created_at == created_at
        assert appointment.updated_at > previous_update
        previous_update = appointment.updated_at


async def test_complete_sms_and_workflow_relationships(seeded_session):
    session = seeded_session
    appointment, _ = await two_appointments(session)
    conversation = Conversation(tenant_id=appointment.tenant_id, customer_id=appointment.customer_id)
    session.add(conversation)
    await session.flush()

    for direction in ("inbound", "outbound"):
        session.add(Message(
            tenant_id=appointment.tenant_id,
            conversation_id=conversation.id,
            direction=direction,
            from_number="+12065550191" if direction == "inbound" else "+12065550192",
            to_number="+12065550192" if direction == "inbound" else "+12065550191",
            body=f"{direction} fixture",
        ))
    workflow = Workflow(
        tenant_id=appointment.tenant_id, conversation_id=conversation.id, workflow_type="test"
    )
    session.add(workflow)
    await session.commit()

    history = (await session.scalars(
        select(Message)
        .where(Message.tenant_id == appointment.tenant_id, Message.conversation_id == conversation.id)
        .order_by(Message.created_at, Message.id)
    )).all()
    assert {message.direction: message.body for message in history} == {
        "inbound": "inbound fixture",
        "outbound": "outbound fixture",
    }
    assert all(message.created_at.utcoffset() is not None for message in history)
    assert await session.scalar(select(Workflow.status).where(Workflow.id == workflow.id)) == "pending"


@pytest.mark.parametrize("reference", [
    "missing_tenant",
    "vehicle_customer",
    "appointment_customer",
    "appointment_vehicle",
    "conversation_customer",
    "message_conversation",
    "workflow_conversation",
])
async def test_database_rejects_cross_tenant_references(seeded_session, reference):
    session = seeded_session
    local, foreign = await two_appointments(session)
    conversation = Conversation(tenant_id=foreign.tenant_id, customer_id=foreign.customer_id)
    session.add(conversation)
    await session.flush()

    appointment_values = {
        "tenant_id": local.tenant_id,
        "customer_id": local.customer_id,
        "vehicle_id": local.vehicle_id,
        "scheduled_start": local.scheduled_start,
        "scheduled_end": local.scheduled_end,
        "service_type": local.service_type,
    }
    if reference == "missing_tenant":
        record = Customer(tenant_id=uuid4(), name="Test", phone_number="+12065550199")
    elif reference == "vehicle_customer":
        record = Vehicle(
            tenant_id=local.tenant_id, customer_id=foreign.customer_id, vin="SPDEMO00000099999",
            make="Ford", model="Escape", model_year=2024,
        )
    elif reference in ("appointment_customer", "appointment_vehicle"):
        column = reference.removeprefix("appointment_") + "_id"
        appointment_values[column] = getattr(foreign, column)
        record = Appointment(**appointment_values)
    elif reference == "conversation_customer":
        record = Conversation(tenant_id=local.tenant_id, customer_id=foreign.customer_id)
    elif reference == "message_conversation":
        record = Message(
            tenant_id=local.tenant_id, conversation_id=conversation.id, direction="inbound",
            from_number="+12065550191", to_number="+12065550192", body="Test",
        )
    else:
        record = Workflow(tenant_id=local.tenant_id, conversation_id=conversation.id, workflow_type="test")

    with pytest.raises(IntegrityError) as error:
        async with session.begin_nested():
            session.add(record)
            await session.flush()
    assert error.value.orig.sqlstate == "23503"


@pytest.mark.parametrize(("violation", "sqlstate"), [
    ("wrong_customer_vehicle", "23503"),
    ("schedule_order", "23514"),
    ("duplicate_dms_id", "23505"),
    ("duplicate_vin", "23505"),
])
async def test_appointment_and_vehicle_constraints(seeded_session, violation, sqlstate):
    session = seeded_session
    local, _ = await two_appointments(session)
    if violation == "duplicate_vin":
        vehicle = await session.get(Vehicle, local.vehicle_id)
        record = Vehicle(
            tenant_id=local.tenant_id, customer_id=local.customer_id, vin=vehicle.vin,
            make=vehicle.make, model=vehicle.model, model_year=vehicle.model_year,
        )
    else:
        record = Appointment(
            tenant_id=local.tenant_id,
            customer_id=local.customer_id,
            vehicle_id=local.vehicle_id,
            scheduled_start=local.scheduled_start,
            scheduled_end=local.scheduled_end,
            service_type=local.service_type,
        )
        if violation == "wrong_customer_vehicle":
            record.vehicle_id = await session.scalar(
                select(Vehicle.id).where(
                    Vehicle.tenant_id == local.tenant_id, Vehicle.customer_id != local.customer_id
                ).limit(1)
            )
        elif violation == "schedule_order":
            record.scheduled_end = local.scheduled_start
        else:
            record.external_dms_id = local.external_dms_id

    with pytest.raises(IntegrityError) as error:
        async with session.begin_nested():
            session.add(record)
            await session.flush()
    assert error.value.orig.sqlstate == sqlstate


async def test_integration_event_idempotency_is_scoped_to_tenant_and_provider(seeded_session):
    session = seeded_session
    local, foreign = await two_appointments(session)
    values = {"provider": "future-dms", "external_event_id": "evt-1", "payload": {"type": "test"}}
    event = IntegrationEvent(tenant_id=local.tenant_id, **values)
    session.add_all([
        event,
        IntegrationEvent(tenant_id=foreign.tenant_id, **values),
        IntegrationEvent(tenant_id=local.tenant_id, **(values | {"provider": "other-provider"})),
    ])
    await session.flush()
    assert event.processing_status == "received"
    assert event.payload == {"type": "test"}

    with pytest.raises(IntegrityError) as error:
        async with session.begin_nested():
            session.add(IntegrationEvent(tenant_id=local.tenant_id, **values))
            await session.flush()
    assert error.value.orig.sqlstate == "23505"
