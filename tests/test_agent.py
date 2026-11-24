import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from agents import RunContextWrapper
from agents.tool_context import ToolContext
from pydantic import SecretStr
from sqlalchemy import func, select, update

from servicepilot.agent import advisor, execution
from servicepilot.agent.advisor import AdvisorOutput, ModelReply
from servicepilot.agent.models import AgentExecution
from servicepilot.agent.tools import (
    AdvisorContext, get_appointment_details, get_available_slots,
    get_customer_appointments, get_customer_vehicles,
)
from servicepilot.appointments.service import AppointmentService, InvalidSlot
from servicepilot.db.models import Appointment, Customer, Message, Vehicle
from test_conversations import (
    AMBIGUOUS_PHONE, FOREIGN_HEADERS, LOCAL_HEADERS, SIMULATION_PATH, UNKNOWN_PHONE,
    simulate, sms_api, sms_data,
)


def agent_path(inbound):
    return f"{SIMULATION_PATH}/{inbound['conversation_id']}/messages/{inbound['message_id']}/agent"


def history_path(inbound):
    return f"{SIMULATION_PATH}/{inbound['conversation_id']}/messages"


async def process(api, inbound):
    response = await api.post(agent_path(inbound), headers=LOCAL_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


async def invoke(tool, context, **arguments):
    tool_arguments = json.dumps(arguments)
    tool_context = ToolContext(
        context=context, tool_name=tool.name, tool_call_id="test-tool-call", tool_arguments=tool_arguments,
    )
    return await tool.on_invoke_tool(tool_context, tool_arguments)


def mock_advisor_runner(monkeypatch, fake_runner):
    async def run_with_test_key(settings, context, model_input):
        settings = settings.model_copy(update={"openai_api_key": SecretStr("test-key"), "agent_max_turns": 5})
        return await advisor.run_advisor(settings, context, model_input)

    monkeypatch.setattr(advisor.Runner, "run", fake_runner)
    monkeypatch.setattr(execution, "run_advisor", run_with_test_key)


async def test_agent_sdk_receives_scoped_context_and_registers_proposal_and_read_tools(monkeypatch, sms_api, sms_data):
    sessions, local, foreign, local_customer, _ = sms_data
    occurred_at = datetime(2020, 1, 7, 2, tzinfo=UTC)
    created_at = datetime(2020, 1, 7, 12, tzinfo=UTC)

    async def fake_runner(agent, *, input, context, max_turns, run_config):
        assert agent.name == "Service Advisor"
        assert agent.output_type is AdvisorOutput
        assert max_turns == 5
        assert run_config.tracing_disabled and not run_config.trace_include_sensitive_data
        assert {tool.name for tool in agent.tools} == {
            "get_customer_vehicles", "get_customer_appointments", "get_appointment_details", "get_available_slots",
            "propose_booking", "propose_reschedule", "propose_cancellation",
            "get_service_information", "ask_dealership_staff",
        }
        for tool in agent.tools:
            assert not {"tenant_id", "customer_id", "conversation_id"} & tool.params_json_schema["properties"].keys()
        assert (context.tenant_id, context.customer_id) == (local.id, local_customer.id)
        model_input = json.loads(input)
        assert model_input["dealership"]["name"] == local.name
        assert model_input["dealership"]["service_days"] == [0, 1, 2, 3, 4]
        assert model_input["customer"] == {"name": local_customer.name}
        assert model_input["current_inbound_sms"] == "What car do I have on file?"
        assert model_input["dealership_local_date"] == "2020-01-06"
        assert str(local.id) not in input and str(context.conversation_id) not in input
        assert str(local_customer.id) not in input and foreign.name not in input
        assert local_customer.phone_number not in input
        vehicles = await invoke(get_customer_vehicles, context)
        assert vehicles["status"] == "ok" and vehicles["vehicles"][0]["model"] == "Escape"
        wrapper = RunContextWrapper(context)
        wrapper.usage.input_tokens = 20
        wrapper.usage.output_tokens = 8
        return SimpleNamespace(final_output=AdvisorOutput(sms="We have a Ford Escape on file."), context_wrapper=wrapper)

    mock_advisor_runner(monkeypatch, fake_runner)
    inbound = await simulate(sms_api, body="What car do I have on file?", external_message_id="vehicle")
    # Simulation timestamps are server assigned. Model an SMS sent before local midnight and stored after it.
    async with sessions.begin() as session:
        message = await session.get_one(Message, UUID(inbound["message_id"]))
        message.occurred_at = occurred_at
        message.created_at = created_at
    pending = await sms_api.get(agent_path(inbound), headers=LOCAL_HEADERS)
    assert pending.json()["status"] == "pending"
    result = await process(sms_api, inbound)
    assert result["status"] == "completed" and result["attempts"] == 1, result
    assert result["reply"]["body"] == "We have a Ford Escape on file."
    assert result["input_tokens"] == 20 and result["output_tokens"] == 8
    assert result["duration_ms"] >= 0 and result["model"]
    assert result["tool_calls"] == [{"name": "get_customer_vehicles", "outcome": "ok"}]
    async with sessions() as session:
        attempt, = (await session.get_one(AgentExecution, UUID(inbound["message_id"]))).advisor_attempts
    assert attempt["reply_source"] == "model" and attempt["reply_id"] == result["reply"]["id"]
    assert attempt["tools"] == result["tool_calls"] and attempt["tool_details"] == []
    duplicate = await simulate(sms_api, body="Duplicate delivery", external_message_id="vehicle")
    assert duplicate["status"] == "duplicate"
    assert await process(sms_api, duplicate) == result
    history = await sms_api.get(history_path(inbound), headers=LOCAL_HEADERS)
    inbound_history, outbound_history = history.json()
    assert (inbound_history["direction"], outbound_history["direction"]) == ("inbound", "outbound")
    assert datetime.fromisoformat(inbound_history["occurred_at"]) == occurred_at
    for path in (agent_path(inbound), history_path(inbound)):
        assert (await sms_api.get(path, headers=FOREIGN_HEADERS)).status_code == 404
    assert (await sms_api.post(agent_path(inbound), headers=FOREIGN_HEADERS)).status_code == 404


async def test_tools_enforce_appointment_and_vehicle_ownership(monkeypatch, sms_api, sms_data):
    sessions, local, foreign, local_customer, _ = sms_data
    async with sessions.begin() as session:
        local_appt = await session.scalar(select(Appointment).where(Appointment.tenant_id == local.id))
        foreign_appt = await session.scalar(select(Appointment).where(Appointment.tenant_id == foreign.id))
        other_customer = await session.scalar(select(Customer).where(
            Customer.tenant_id == local.id, Customer.phone_number == AMBIGUOUS_PHONE,
        ).limit(1))
        other_vehicle = Vehicle(
            tenant_id=local.id, customer_id=other_customer.id, vin="3" * 17,
            make="Ford", model="Escape", model_year=2024,
        )
        session.add(other_vehicle)
        await session.flush()
        other_appt = Appointment(
            tenant_id=local.id, customer_id=other_customer.id, vehicle_id=other_vehicle.id,
            scheduled_start=local_appt.scheduled_start, scheduled_end=local_appt.scheduled_end,
            service_type="Private service",
        )
        session.add(other_appt)

    async def model(settings, context: AdvisorContext, model_input):
        assert context.customer_id == local_customer.id
        appointments = await invoke(get_customer_appointments, context)
        assert [a["id"] for a in appointments["appointments"]] == [str(local_appt.id)]
        details = await invoke(get_appointment_details, context, appointment_id=str(local_appt.id))
        assert details["appointment"]["service_type"] == "Oil change"
        for record in (foreign_appt, other_appt):
            assert await invoke(get_appointment_details, context, appointment_id=str(record.id)) == {
                "status": "not_found",
            }
            assert await invoke(get_available_slots, context, day="2099-01-05", vehicle_id=str(record.vehicle_id)) == {
                "status": "not_found",
            }
        slots = await invoke(get_available_slots, context, day="2099-01-05", vehicle_id=str(local_appt.vehicle_id))
        assert slots["status"] == "ok" and slots["slots"]
        past = await invoke(get_available_slots, context, day="2000-01-01")
        assert past["status"] == "ok" and past["slots"] == [] and past["unavailable_reason"] == "past"
        async def invalid_slot(*args, **kwargs):
            raise InvalidSlot("Internal detail")

        with monkeypatch.context() as patches:
            patches.setattr(AppointmentService, "availability", invalid_slot)
            assert await invoke(get_available_slots, context, day="2099-01-05", vehicle_id=None) == {
                "status": "invalid_date",
            }
        return ModelReply("I found your appointment.", 10, 5)

    monkeypatch.setattr(execution, "run_advisor", model)
    result = await process(sms_api, await simulate(sms_api))
    assert result["status"] == "completed", result
    async with sessions() as session:
        assert (await session.get_one(Appointment, local_appt.id)).status == "scheduled"
        assert (await session.get_one(Appointment, other_appt.id)).service_type == "Private service"


@pytest.mark.parametrize(("phone", "resolution"), [(UNKNOWN_PHONE, "unmatched"), (AMBIGUOUS_PHONE, "ambiguous")])
async def test_unresolved_and_ambiguous_cannot_read_private_records(monkeypatch, sms_api, phone, resolution):
    async def model(settings, context, model_input):
        snapshot = json.loads(model_input)
        assert context.customer_id is None and snapshot["customer_resolution"] == resolution
        assert snapshot["customer"] is None
        assert snapshot["vehicles"] == snapshot["upcoming_appointments"] == []
        assert await invoke(get_customer_appointments, context) == {"status": "identity_required"}
        assert await invoke(get_customer_vehicles, context) == {"status": "identity_required"}
        assert (await invoke(get_available_slots, context, day="2099-01-05", vehicle_id=None))["status"] == "ok"
        return ModelReply("Which service appointment are you asking about?", 12, 9)

    monkeypatch.setattr(execution, "run_advisor", model)
    result = await process(sms_api, await simulate(sms_api, phone=phone))
    assert result["status"] == "completed", result


async def test_concurrent_inbound_and_processing_preserve_order_and_history(monkeypatch, sms_api):
    started = asyncio.Event()
    release = asyncio.Event()
    inputs = []

    async def model(settings, context, model_input):
        inputs.append(json.loads(model_input))
        if len(inputs) == 1:
            started.set()
            await release.wait()
        return ModelReply(f"Reply {len(inputs)}", 10, 3)

    monkeypatch.setattr(execution, "run_advisor", model)
    first = await simulate(sms_api, body="First question")
    task = asyncio.create_task(process(sms_api, first))
    try:
        await asyncio.wait_for(started.wait(), 5)
        second = await asyncio.wait_for(simulate(sms_api, body="Second question"), 5)
        assert first["conversation_id"] == second["conversation_id"]
        duplicate = await asyncio.wait_for(process(sms_api, first), 5)
        assert duplicate["status"] == "running" and duplicate["attempts"] == 1
        blocked = await sms_api.post(agent_path(second), headers=LOCAL_HEADERS)
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["message_id"] == first["message_id"]
    finally:
        release.set()
        await task
    third = await simulate(sms_api, body="Third question")
    assert third["conversation_id"] == first["conversation_id"]
    assert (await process(sms_api, second))["status"] == "completed"
    assert inputs[0]["history"] == []
    assert all(item["local_date"] for item in inputs[1]["history"])
    assert [{key: item[key] for key in ("direction", "body")} for item in inputs[1]["history"]] == [
        {"direction": "inbound", "body": "First question"},
        {"direction": "outbound", "body": "Reply 1"},
    ]
    assert inputs[1]["current_inbound_sms"] == "Second question"
    assert (await process(sms_api, third))["status"] == "completed"
    assert inputs[2]["current_inbound_sms"] == "Third question"


async def test_model_error_has_no_outbound_and_retries_same_inbound(monkeypatch, sms_api, caplog):
    calls = 0
    private_detail = "private provider error text"
    private_sms = "private inbound message text"

    async def model(settings, context, model_input):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("private provider error text")
        if calls == 2:
            raise RuntimeError(private_detail)
        return ModelReply("Here to help.", 8, 4)

    monkeypatch.setattr(execution, "run_advisor", model)
    inbound = await simulate(sms_api, body=private_sms)
    result = await process(sms_api, inbound)
    assert result["status"] == "failed" and result["error_code"] == "timeout" and result["reply"] is None
    assert "private provider" not in json.dumps(result)
    result = await process(sms_api, inbound)
    assert result["status"] == "failed" and result["error_code"] == "execution_error" and result["reply"] is None
    assert private_detail not in json.dumps(result)
    assert "RuntimeError" in caplog.text and "Traceback (most recent call last)" in caplog.text
    assert private_detail not in caplog.text and private_sms not in caplog.text
    history = (await sms_api.get(history_path(inbound), headers=LOCAL_HEADERS)).json()
    assert [item["direction"] for item in history] == ["inbound"]
    result = await process(sms_api, inbound)
    assert result["status"] == "completed" and result["attempts"] == 3
    assert result["error_code"] is None and calls == 3


async def test_expired_attempt_cannot_overwrite_recovered_reply(monkeypatch, sms_api, sms_data):
    sessions, *_ = sms_data
    started = asyncio.Event()
    release = asyncio.Event()

    async def model(settings, context, model_input):
        if context.attempt == 1:
            started.set()
            await release.wait()
            return ModelReply("Expired reply", 10, 3)
        return ModelReply("Recovered reply", 8, 3)

    monkeypatch.setattr(execution, "run_advisor", model)
    inbound = await simulate(sms_api)
    task = asyncio.create_task(process(sms_api, inbound))
    try:
        await asyncio.wait_for(started.wait(), 5)
        async with sessions.begin() as session:
            await session.execute(update(AgentExecution).where(AgentExecution.id == inbound["message_id"]).values(
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            ))
        result = await process(sms_api, inbound)
        assert result["attempts"] == 2 and result["reply"]["body"] == "Recovered reply"
    finally:
        release.set()
        expired_result = await task
    assert expired_result == result
    history = (await sms_api.get(history_path(inbound), headers=LOCAL_HEADERS)).json()
    assert [item["body"] for item in history if item["direction"] == "outbound"] == ["Recovered reply"]


async def test_identity_change_discards_inflight_reply(monkeypatch, sms_api, sms_data):
    sessions, local, _, customer, _ = sms_data

    async def model(settings, context, model_input):
        async with sessions.begin() as session:
            session.add(Customer(tenant_id=local.id, name="New match", phone_number=customer.phone_number))
        assert await invoke(get_customer_appointments, context) == {"status": "identity_required"}
        return ModelReply("Private appointment reply", 10, 3)

    monkeypatch.setattr(execution, "run_advisor", model)
    result = await process(sms_api, await simulate(sms_api))
    assert result["status"] == "failed" and result["error_code"] == "identity_changed"
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Message).where(
            Message.tenant_id == local.id, Message.direction == "outbound",
        )) == 0
