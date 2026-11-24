import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from servicepilot import worker
from servicepilot.agent import execution
from servicepilot.agent.advisor import ModelReply
from servicepilot.agent.models import AgentExecution
from servicepilot.appointments.models import Appointment
from servicepilot.customers.models import Customer
from servicepilot.sms.delivery import deliver_next
from servicepilot.sms.models import SMSDelivery
from servicepilot.sms.transport import SendError, SimulationTransport
from test_agent import agent_path
from test_appointment_actions import audit, scenario
from test_conversations import FOREIGN_HEADERS, LOCAL_HEADERS, SHARED_PHONE, simulate, sms_api, sms_data


def settings_for(api):
    return api._transport.app.state.settings


async def state(api, receipt):
    response = await api.get(agent_path(receipt), headers=LOCAL_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def delivery_path(receipt, reply):
    return f"/simulations/sms/{receipt['conversation_id']}/messages/{reply['id']}/delivery"


async def automatic_turn(api, sessions, body, **fields):
    receipt = await simulate(api, body=body, **fields)
    settings = settings_for(api)
    assert await execution.process_next(sessions, settings)
    result = await state(api, receipt)
    assert result["status"] == "completed"
    assert await deliver_next(sessions, settings, {"simulation": SimulationTransport()})
    return receipt, result


@pytest.fixture
def simple_model(monkeypatch):
    async def reply(*_):
        return ModelReply("How can I help?", 1, 1)

    monkeypatch.setattr(execution, "run_advisor", reply)


async def test_ingestion_to_delivery_and_tenant_diagnostics(simple_model, sms_api, sms_data):
    sessions = sms_data[0]
    receipt = await simulate(sms_api, external_message_id="retry-same-provider-id")
    assert (await state(sms_api, receipt))["status"] == "pending"
    assert await execution.process_next(sessions, settings_for(sms_api))
    done = await state(sms_api, receipt)
    assert done["status"] == "completed" and done["attempts"] == 1
    path = delivery_path(receipt, done["reply"])
    assert (await sms_api.get(path, headers=LOCAL_HEADERS)).json()["status"] == "pending"
    assert (await sms_api.get(path, headers=FOREIGN_HEADERS)).status_code == 404
    transports = {"simulation": SimulationTransport()}
    assert sum(await asyncio.gather(*(
        deliver_next(sessions, settings_for(sms_api), transports) for _ in range(4)
    ))) == 1
    delivery = (await sms_api.get(path, headers=LOCAL_HEADERS)).json()
    assert delivery["status"] == "delivered" and delivery["attempts"] == 1
    params = {"sender_phone": SHARED_PHONE, "include_all": "true"}
    inbox = (await sms_api.get("/simulations/sms", params=params, headers=LOCAL_HEADERS)).json()
    assert datetime.fromisoformat(inbox["outbound"][0].pop("delivered_at")) == datetime.fromisoformat(delivery["delivered_at"])
    assert inbox["outbound"] == [{
        "id": done["reply"]["id"], "body": done["reply"]["body"], "source": "advisor",
        "status": "delivered", "error_code": None,
    }]
    foreign = (await sms_api.get("/simulations/sms", params=params, headers=FOREIGN_HEADERS)).json()
    assert foreign["outbound"] == []
    duplicate = await simulate(sms_api, external_message_id="retry-same-provider-id")
    assert duplicate["status"] == "duplicate" and duplicate["message_id"] == receipt["message_id"]
    assert not await execution.process_next(sessions, settings_for(sms_api))
    assert not await deliver_next(sessions, settings_for(sms_api), transports)


async def test_concurrent_workers_keep_thread_order(monkeypatch, sms_api, sms_data):
    sessions = sms_data[0]
    settings = settings_for(sms_api)
    entered, release = asyncio.Event(), asyncio.Event()
    turns = []

    async def model(_, context, model_input):
        turns.append(json.loads(model_input)["current_inbound_sms"])
        if len(turns) == 1:
            entered.set()
            await release.wait()
        return ModelReply("Reply", 1, 1)

    monkeypatch.setattr(execution, "run_advisor", model)
    first = await simulate(sms_api, body="First")
    second = await simulate(sms_api, body="Second")
    worker = asyncio.create_task(execution.process_next(sessions, settings))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not any(await asyncio.gather(*(execution.process_next(sessions, settings) for _ in range(3))))
        assert (await state(sms_api, second))["status"] == "pending"
    finally:
        release.set()
        await worker
    assert await execution.process_next(sessions, settings)
    assert (await state(sms_api, first))["attempts"] == (await state(sms_api, second))["attempts"] == 1
    assert turns == ["First", "Second"]


async def test_queued_reply_cannot_send_after_customer_phone_changes(simple_model, sms_api, sms_data):
    sessions, _, _, customer, _ = sms_data
    settings = settings_for(sms_api)
    receipt = await simulate(sms_api, phone=customer.phone_number)
    assert await execution.process_next(sessions, settings)
    done = await state(sms_api, receipt)
    assert done["status"] == "completed"

    async with sessions.begin() as session:
        row = await session.get_one(Customer, customer.id)
        row.phone_number = "+12065550199"

    assert await deliver_next(sessions, settings, {"simulation": SimulationTransport()})
    response = await sms_api.get(delivery_path(receipt, done["reply"]), headers=LOCAL_HEADERS)
    delivery = response.json()
    assert (delivery["status"], delivery["error_code"], delivery["attempts"]) == ("failed", "identity_changed", 0)


async def test_restart_reclaims_expired_agent_lease(simple_model, sms_api, sms_data):
    sessions = sms_data[0]
    receipt = await simulate(sms_api)
    async with sessions.begin() as session:
        row = await session.get_one(AgentExecution, UUID(receipt["message_id"]))
        row.status, row.attempts = "running", 1
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await execution.process_next(sessions, settings_for(sms_api))
    assert (await state(sms_api, receipt))["attempts"] == 2


async def test_worker_process_loop_drains_work_and_stops(
    monkeypatch, tmp_path, migrated_database, simple_model, sms_api, sms_data,
):
    settings = settings_for(sms_api).model_copy(update={"worker_poll_seconds": 0.1})
    monkeypatch.setattr(worker, "build_engine", lambda _: migrated_database)
    monkeypatch.setattr(worker, "HEARTBEAT", tmp_path / "heartbeat")
    receipt = await simulate(sms_api, body="Queued before startup")
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run_worker(settings, stop))
    try:
        async with asyncio.timeout(10):
            while True:
                result = await state(sms_api, receipt)
                if result["reply"]:
                    delivery = await sms_api.get(delivery_path(receipt, result["reply"]), headers=LOCAL_HEADERS)
                    if delivery.json()["status"] == "delivered":
                        break
                await asyncio.sleep(0.05)
        assert worker.HEARTBEAT.exists()
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    assert not worker.HEARTBEAT.exists()


async def test_agent_backoff_exhaustion_and_next_inbound(monkeypatch, sms_api, sms_data):
    sessions = sms_data[0]
    settings = settings_for(sms_api).model_copy(update={"agent_max_attempts": 2})

    async def model(_, context, model_input):
        if json.loads(model_input)["current_inbound_sms"] == "Will fail":
            raise TimeoutError
        return ModelReply("Okay", 1, 1)

    monkeypatch.setattr(execution, "run_advisor", model)
    first = await simulate(sms_api, body="Will fail")
    second = await simulate(sms_api, body="Next")
    assert await execution.process_next(sessions, settings)
    assert (await state(sms_api, first))["next_attempt_at"]
    assert not await execution.process_next(sessions, settings)
    async with sessions.begin() as session:
        await session.execute(update(AgentExecution).where(
            AgentExecution.id == UUID(first["message_id"]),
        ).values(next_attempt_at=func.now() - timedelta(seconds=1)))
    assert await execution.process_next(sessions, settings)
    failed = await state(sms_api, first)
    assert failed["status"] == "failed" and failed["attempts"] == 2 and failed["next_attempt_at"] is None
    assert await execution.process_next(sessions, settings)
    assert (await state(sms_api, second))["status"] == "completed"


async def test_delivery_retry_exhaustion_and_order(simple_model, sms_api, sms_data):
    sessions = sms_data[0]
    settings = settings_for(sms_api).model_copy(update={"sms_delivery_max_attempts": 2})
    first = await simulate(sms_api, body="First")
    second = await simulate(sms_api, body="Second")
    assert await execution.process_next(sessions, settings)
    assert await execution.process_next(sessions, settings)
    first_id = UUID((await state(sms_api, first))["reply"]["id"])
    attempts = []

    class ScriptedTransport(SimulationTransport):
        async def send(self, sms):
            attempts.append(sms.id)
            if sms.id == first_id:
                raise SendError("fixture_retry", "retryable")
            return await super().send(sms)

    transports = {"simulation": ScriptedTransport()}
    assert await deliver_next(sessions, settings, transports)
    assert not await deliver_next(sessions, settings, transports)
    async with sessions.begin() as session:
        row = await session.get_one(SMSDelivery, first_id)
        assert row.status == "pending" and row.next_attempt_at is not None
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await deliver_next(sessions, settings, transports)
    assert await deliver_next(sessions, settings, transports)
    assert not await deliver_next(sessions, settings, transports)
    second_id = UUID((await state(sms_api, second))["reply"]["id"])
    assert attempts == [first_id, first_id, second_id]
    async with sessions() as session:
        row = await session.get_one(SMSDelivery, first_id)
        assert row.status == "failed" and row.error_code == "fixture_retry" and row.attempts == 2


async def test_opt_out_opt_in_and_queued_reply_suppression(simple_model, sms_api, sms_data):
    sessions = sms_data[0]
    settings = settings_for(sms_api)
    receipt = await simulate(sms_api, body="A question")
    assert await execution.process_next(sessions, settings)
    done = await state(sms_api, receipt)
    assert (await simulate(sms_api, body="STOP"))["processing"] == "control"
    assert (await simulate(sms_api, body="Another question"))["processing"] == "suppressed"
    assert not await execution.process_next(sessions, settings)
    assert (await simulate(sms_api, body="START"))["processing"] == "control"
    assert await deliver_next(sessions, settings, {"simulation": SimulationTransport()})
    old = await sms_api.get(delivery_path(receipt, done["reply"]), headers=LOCAL_HEADERS)
    assert old.json()["status"] == "failed" and old.json()["error_code"] == "sms_suppressed"
    await automatic_turn(sms_api, sessions, "Question after opting in")


async def test_request_and_per_thread_rate_limits(simple_model, sms_api, sms_data):
    app = sms_api._transport.app
    app.state.settings = settings_for(sms_api).model_copy(update={"sms_messages_per_minute": 2})
    first = await simulate(sms_api, body="One", external_message_id="one")
    duplicate = await simulate(sms_api, body="One", external_message_id="one")
    assert first["message_id"] == duplicate["message_id"]
    assert (await simulate(sms_api, body="Two"))["processing"] == "queued"
    limited = await simulate(sms_api, body="Three")
    assert limited["processing"] == "rate_limited"
    assert (await sms_api.get(agent_path(limited), headers=LOCAL_HEADERS)).status_code == 404
    assert (await sms_api.post("/simulations/sms", headers=LOCAL_HEADERS, content=b"x" * 16385)).status_code == 413


async def test_automatic_booking_move_cancel_and_dedup(sms_api, sms_data, scenario):
    sessions = sms_data[0]
    original, start, *_ = scenario
    await automatic_turn(sms_api, sessions, "Book my Escape for an alignment")
    booked, _ = await automatic_turn(sms_api, sessions, "Book it")
    await automatic_turn(sms_api, sessions, "Yes, but make it an hour later.")
    moved, _ = await automatic_turn(sms_api, sessions, "Yeah, go ahead.")
    await automatic_turn(sms_api, sessions, "Cancel my oil change")
    cancelled, _ = await automatic_turn(sms_api, sessions, "Please cancel it", external_message_id="decision-1")
    duplicate = await simulate(sms_api, body="Please cancel it", external_message_id="decision-1")
    assert duplicate["message_id"] == cancelled["message_id"]
    assert not await execution.process_next(sessions, settings_for(sms_api))
    audit_result = await audit(sms_api, booked)
    keys = {row["decision_message_id"] for row in audit_result["results"] if row["outcome"] == "succeeded"}
    assert keys == {booked["message_id"], moved["message_id"], cancelled["message_id"]}
    async with sessions() as session:
        assert (await session.get_one(Appointment, original.id)).status == "cancelled"


@pytest.mark.parametrize("send_fails", [True, False])
async def test_failed_or_late_prompt_cannot_confirm(sms_api, sms_data, scenario, send_fails):
    sessions = sms_data[0]
    settings = settings_for(sms_api)
    original, *_ = scenario
    prompt = await simulate(sms_api, body="Cancel my oil change")
    assert await execution.process_next(sessions, settings)
    result = await state(sms_api, prompt)
    decision = await simulate(sms_api, body="Yes, please.")

    class RejectedPrompt(SimulationTransport):
        async def send(self, sms):
            raise SendError("fixture_rejected", "permanent")

    provider = RejectedPrompt() if send_fails else SimulationTransport()
    assert await deliver_next(sessions, settings, {"simulation": provider})
    delivery = (await sms_api.get(delivery_path(prompt, result["reply"]), headers=LOCAL_HEADERS)).json()
    assert delivery["status"] == ("failed" if send_fails else "delivered")
    assert await execution.process_next(sessions, settings)
    rejected = await state(sms_api, decision)
    assert "request the appointment change again" in rejected["reply"]["body"]
    diagnostic, = rejected["confirmation_attempts"]
    assert diagnostic["interpretation"] == "not_run" and diagnostic["authorization"] == "rejected"
    assert diagnostic["action_outcome"] == "delivery_unverified" and diagnostic["prompt_id"] == result["reply"]["id"]
    async with sessions() as session:
        assert (await session.get_one(Appointment, original.id)).status == "scheduled"


async def test_last_agent_attempt_crash_recovers_only_the_committed_result(monkeypatch, sms_api, sms_data, scenario):
    sessions = sms_data[0]
    original, _, _, _, _ = scenario
    await automatic_turn(sms_api, sessions, "Cancel my oil change")
    decision = await simulate(sms_api, body="Yeah, go ahead.")
    decision_id = UUID(decision["message_id"])
    settings = settings_for(sms_api).model_copy(update={"agent_max_attempts": 1})

    async def crash(*_):
        raise RuntimeError("worker stopped before reply commit")

    with monkeypatch.context() as patch:
        patch.setattr(execution, "_finish", crash)
        with pytest.raises(RuntimeError, match="worker stopped"):
            await execution.process_next(sessions, settings)
    async with sessions.begin() as session:
        appointment = await session.get_one(Appointment, original.id)
        version = appointment.updated_at
        assert appointment.status == "cancelled"
        row = await session.get_one(AgentExecution, decision_id)
        row.lease_expires_at = await session.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
    monkeypatch.setattr(execution, "run_advisor", crash)
    assert await execution.process_next(sessions, settings)
    result = await state(sms_api, decision)
    assert result["attempts"] == 2 and "was cancelled" in result["reply"]["body"]
    async with sessions() as session:
        assert (await session.get_one(Appointment, original.id)).updated_at == version
