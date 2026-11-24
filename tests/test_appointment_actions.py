import asyncio
import json
import re
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from agents import RunContextWrapper
from sqlalchemy import func, select, update

from servicepilot.agent import execution
from servicepilot.agent.actions import confirmation_prompt, handle_confirmation, lock_conversation
from servicepilot.agent.advisor import AdvisorOutput, ModelReply, ProposalInformationCheck
from servicepilot.agent.models import AgentExecution
from servicepilot.agent.tools import (
    get_available_slots, get_customer_appointments, get_service_information, interpret_appointment_reply, propose_booking,
    propose_cancellation, propose_reschedule,
)
from servicepilot.conversations.pending import pending_proposal
from servicepilot.db.models import Appointment, Conversation, Customer, Message, Tenant, Vehicle
from test_agent import agent_path, history_path, invoke, mock_advisor_runner, process
from test_conversations import (
    AMBIGUOUS_PHONE, FOREIGN_HEADERS, LOCAL_HEADERS, UNKNOWN_PHONE, simulate, sms_api, sms_data,
)


def audit_path(receipt):
    return f"/simulations/sms/{receipt['conversation_id']}/appointment-actions"


async def audit(api, receipt):
    response = await api.get(audit_path(receipt), headers=LOCAL_HEADERS)
    assert response.status_code == 200
    return response.json()


@pytest.fixture
async def scenario(monkeypatch, sms_api, sms_data):
    sessions, local, _, customer, _ = sms_data
    async with sessions() as session:
        original = await session.scalar(select(Appointment).where(Appointment.customer_id == customer.id))
    future_day = (datetime.now(UTC) + timedelta(days=14)).date()
    future_day += timedelta(days=(4 - future_day.weekday()) % 7)
    zone = ZoneInfo(local.timezone)
    start = datetime.combine(future_day, local.opens_at, tzinfo=zone)
    later = start + timedelta(hours=1)
    requests = {
        "Book my Escape for an alignment": (
            propose_booking, dict(vehicle_id=str(original.vehicle_id), service_type="Alignment", scheduled_start=start),
        ),
        "Move my oil change": (
            propose_reschedule, dict(appointment_id=str(original.id), scheduled_start=start),
        ),
        "Yes, but make it an hour later.": (
            propose_reschedule, dict(appointment_id=str(original.id), scheduled_start=later),
        ),
        "Cancel my oil change": (propose_cancellation, dict(appointment_id=str(original.id))),
    }

    async def model(settings, context, model_input):
        data = json.loads(model_input)
        body = data["current_inbound_sms"]
        if data["active_confirmation_question"]:
            if body in {"ye go ahead", "Yeah, go ahead.", "Yes, please.", "Book it", "Please cancel it", "Sounds good."}:
                decision = "confirm"
            elif body == "No, thanks.":
                decision = "decline"
            elif body in requests or body == "Actually, make it next month":
                decision = "revise"
            else:
                decision = "clarify"
            assert await invoke(interpret_appointment_reply, context, decision=decision) == "intent_recorded"
            return ModelReply(None, 2, 2)
        if body not in requests:
            return ModelReply("What would you like to change?", 2, 2)
        tool, kwargs = requests[body]
        result = await invoke(tool, context, **{
            key: value.isoformat() if isinstance(value, datetime) else value for key, value in kwargs.items()
        })
        if result["status"] == "identity_required":
            return ModelReply("Please contact the dealership for help verifying your account.", 2, 2)
        return ModelReply(f"Tool status: {result['status']}", 2, 2)

    monkeypatch.setattr(execution, "run_advisor", model)
    receipt = await simulate(sms_api, body="Hi")
    assert (await process(sms_api, receipt))["status"] == "completed"
    return original, start, later, receipt, requests


async def send(api, body):
    receipt = await simulate(api, body=body)
    return receipt, await process(api, receipt)


def sdk_result(context, output):
    return SimpleNamespace(final_output=output, context_wrapper=RunContextWrapper(context), raw_responses=[])


def next_thursday(snapshot):
    today = date.fromisoformat(snapshot["dealership_local_date"])
    return today + timedelta(days=(3 - today.weekday()) % 7 + 7)


@pytest.mark.parametrize(("checked_availability", "structured"), [(False, False), (True, False), (True, True)])
async def test_unprepared_confirmation_and_later_agreement_cannot_move_appointment(
    monkeypatch, sms_api, sms_data, checked_availability, structured,
):
    sessions, _, _, customer, _ = sms_data
    async with sessions() as session:
        original = await session.scalar(select(Appointment).where(Appointment.customer_id == customer.id))

    async def model(agent, *, input, context, **_):
        snapshot = json.loads(input)
        assert snapshot["active_confirmation_question"] is None
        assert "interpret_appointment_reply" not in {tool.name for tool in agent.tools}
        if snapshot["current_inbound_sms"] == "Yeah, go ahead.":
            return sdk_result(context, AdvisorOutput(sms="How can I help with your service appointment?"))
        if checked_availability:
            slots = await invoke(get_available_slots, context, day=next_thursday(snapshot).isoformat())
            assert slots["status"] == "ok" and slots["slots"]
            assert agent.tool_use_behavior == "run_llm_again"
        if structured:
            return sdk_result(context, AdvisorOutput(sms=None))
        return sdk_result(context, "Should I move your oil change to Thursday at 2?")

    mock_advisor_runner(monkeypatch, model)
    receipt, result = await send(sms_api, "Move my oil change to next Thursday at 2.")
    assert result["reply"] is None and result["error_code"] == "invalid_reply"
    assert result["tool_calls"] == ([{"name": "get_available_slots", "outcome": "ok"}] if checked_availability else [])
    assert await audit(sms_api, receipt) == {"pending": None, "results": []}
    _, agreement = await send(sms_api, "Yeah, go ahead.")
    assert agreement["reply"]["body"] == "How can I help with your service appointment?"
    assert await audit(sms_api, receipt) == {"pending": None, "results": []}
    async with sessions() as session:
        assert (await session.get_one(Appointment, original.id)).scheduled_start == original.scheduled_start


async def test_natural_reschedule_and_revision_publish_exact_backend_confirmation(monkeypatch, sms_api, sms_data):
    sessions, _, _, customer, _ = sms_data
    async with sessions() as session:
        original = await session.scalar(select(Appointment).where(Appointment.customer_id == customer.id))

    async def model(agent, *, input, context, **_):
        snapshot = json.loads(input)
        body = snapshot["current_inbound_sms"]
        if body == "Yeah, go ahead.":
            return sdk_result(context, await invoke(interpret_appointment_reply, context, decision="confirm"))
        revision = body == "Actually, make it 3 instead."
        appointment = snapshot["upcoming_appointments"][0]
        if revision and context.pending is not None:
            assert snapshot["active_proposal"]["appointment_id"] == appointment["id"]
            assert snapshot["active_confirmation_question"]
            assert snapshot["history"][-1]["body"] == snapshot["active_confirmation_question"]
            return sdk_result(context, await invoke(interpret_appointment_reply, context, decision="revise"))
        if revision:
            assert snapshot["revision_of"]["appointment_id"] == appointment["id"]
            assert snapshot["active_proposal"] is None
            assert agent.model_settings.tool_choice is None
            assert interpret_appointment_reply not in agent.tools
        else:
            assert body == "Move my oil change to next Thursday at 2."
            assert snapshot["active_proposal"] is None
        slots = await invoke(
            get_available_slots, context, day=next_thursday(snapshot).isoformat(),
            vehicle_id=appointment["vehicle_id"], period="afternoon",
        )
        hour = 15 if revision else 14
        slot = next(slot for slot in slots["slots"] if datetime.fromisoformat(slot["start"]).hour == hour)
        assert await invoke(propose_reschedule, context, appointment_id=appointment["id"], scheduled_start=slot["start"]) == {
            "status": "proposal_prepared",
        }
        assert agent.tool_use_behavior == "run_llm_again"
        return sdk_result(context, AdvisorOutput(sms="Should I move your Escape to 7 PM?"))

    mock_advisor_runner(monkeypatch, model)
    proposals = []
    for body, expected_hour in [("Move my oil change to next Thursday at 2.", 14), ("Actually, make it 3 instead.", 15)]:
        receipt, result = await send(sms_api, body)
        state = await audit(sms_api, receipt)
        proposal = state["pending"]["proposal"]
        proposals.append(proposal["id"])
        async with sessions() as session:
            conversation = await session.get_one(Conversation, UUID(receipt["conversation_id"]))
            published = pending_proposal(conversation)
            assert str(published.prompt_id) == result["reply"]["id"]
            assert result["reply"]["body"] == confirmation_prompt(published)
            assert published.scheduled_start.astimezone(ZoneInfo(published.timezone)).hour == expected_hour
            assert (await session.get_one(Appointment, original.id)).scheduled_start == original.scheduled_start
    assert proposals[0] != proposals[1]

    receipt, confirmed = await send(sms_api, "Yeah, go ahead.")
    state = await audit(sms_api, receipt)
    assert state["pending"] is None
    succeeded = next(item for item in state["results"] if item["outcome"] == "succeeded")
    assert succeeded["reply"] == confirmed["reply"]["body"]
    assert "moved to" in confirmed["reply"]["body"] and "3:00 PM" in confirmed["reply"]["body"]


async def test_failed_proposal_cannot_publish_model_success(monkeypatch, sms_api, sms_data):
    sessions, local, _, _, _ = sms_data

    async def model(agent, *, input, context, **_):
        assert await invoke(propose_cancellation, context, appointment_id=str(uuid4())) == {"status": "not_found"}
        return sdk_result(context, "Done! Your appointment was cancelled.")

    mock_advisor_runner(monkeypatch, model)
    receipt, result = await send(sms_api, "Cancel my appointment tomorrow.")
    assert result["reply"]["body"] == execution.UNPREPARED_ACTION_REPLY
    assert await audit(sms_api, receipt) == {"pending": None, "results": []}
    async with sessions() as session:
        assert await session.scalar(select(Appointment.status).where(Appointment.tenant_id == local.id)) == "scheduled"


@pytest.mark.parametrize("safe", [True, False])
async def test_informational_answer_accompanies_only_the_authoritative_proposal(monkeypatch, sms_api, sms_data, safe):
    sessions, dealer, _, customer, _ = sms_data
    async with sessions.begin() as session:
        tenant = await session.get_one(Tenant, dealer.id)
        tenant.service_catalog = {"oil change": {"name": "Oil change", "description": "Oil and filter.", "minutes": 30}}
        original = await session.scalar(select(Appointment).where(Appointment.customer_id == customer.id))
    draft = "Plan on about half an hour for the oil change." if safe else "I moved your oil change to 7 PM."
    passes = []

    async def runner(agent, *, input, context, **_):
        passes.append(agent.output_type)
        if agent.output_type is ProposalInformationCheck:
            assert json.loads(input)["information"] == draft
            assert json.loads(input)["configured_service_durations"]["oil change"][1] == 30
            return sdk_result(context, ProposalInformationCheck(safe=safe))
        appointment_id = json.loads(input)["upcoming_appointments"][0]["id"]
        day = next_thursday(json.loads(input))
        slot = datetime.combine(day, dealer.opens_at, ZoneInfo(dealer.timezone))
        assert (await invoke(propose_reschedule, context, appointment_id=appointment_id,
                             scheduled_start=slot.isoformat()))["status"] == "proposal_prepared"
        assert (await invoke(get_service_information, context))["services"][0]["duration_minutes"] == 30
        return sdk_result(context, AdvisorOutput(sms=None, proposal_information=draft, proposal_duration_for="oil change"))

    mock_advisor_runner(monkeypatch, runner)
    receipt, result = await send(sms_api, "Move my oil change to Thursday at 8. How long does it take?")
    assert passes == [AdvisorOutput, ProposalInformationCheck]
    async with sessions() as session:
        proposal = pending_proposal(await session.get_one(Conversation, UUID(receipt["conversation_id"])))
        assert str(proposal.prompt_id) == result["reply"]["id"]
        assert (await session.get_one(Appointment, original.id)).scheduled_start == original.scheduled_start
    expected = draft if safe else "Oil change typically takes about 30 minutes."
    assert result["reply"]["body"] == f"{expected} {confirmation_prompt(proposal)}"
    assert result["reply"]["body"].count("?") == 1
    assert (await audit(sms_api, receipt))["pending"]["prompt_delivery_status"] == "delivered"


@pytest.mark.parametrize("specific_time_unavailable", [False, True])
async def test_broad_request_or_unavailable_time_offers_actual_slots(
    monkeypatch, sms_api, sms_data, specific_time_unavailable,
):
    sessions, local, _, customer, _ = sms_data
    zone = ZoneInfo(local.timezone)
    day = next_thursday({"dealership_local_date": datetime.now(zone).date().isoformat()})
    if specific_time_unavailable:
        async with sessions.begin() as session:
            original = await session.scalar(select(Appointment).where(Appointment.customer_id == customer.id))
            blocked = datetime.combine(day, local.opens_at, tzinfo=zone).replace(hour=14)
            session.add(Appointment(
                tenant_id=local.id, customer_id=customer.id, vehicle_id=original.vehicle_id, service_type="Alignment",
                scheduled_start=blocked, scheduled_end=blocked + timedelta(hours=1),
            ))

    async def model(agent, *, input, context, **_):
        slots = await invoke(get_available_slots, context, day=day.isoformat(), period="afternoon")
        assert slots["status"] == "ok"
        hours = [datetime.fromisoformat(slot["start"]).hour for slot in slots["slots"]]
        assert (14 in hours) is not specific_time_unavailable
        times = ", ".join(datetime.fromisoformat(item["start"]).strftime("%I:%M %p").lstrip("0")
                          for item in slots["slots"][:3])
        return sdk_result(context, AdvisorOutput(sms=f"I have {times}. Which time works for you?"))

    mock_advisor_runner(monkeypatch, model)
    body = (
        "Move my oil change to next Thursday at 2."
        if specific_time_unavailable else "Do you have anything next Thursday afternoon?"
    )
    receipt, result = await send(sms_api, body)
    reply = result["reply"]["body"]
    assert "Which time works for you?" in reply and "12:00 PM" in reply and "1:00 PM" in reply
    offered_two_pm = re.search(r"(?<!\d)2:00 PM\b", reply) is not None
    assert offered_two_pm is (not specific_time_unavailable)
    assert await audit(sms_api, receipt) == {"pending": None, "results": []}


async def test_unknown_appointment_prompts_for_clarification(monkeypatch, sms_api):
    async def model(agent, *, input, context, **_):
        snapshot = json.loads(input)
        appointments = await invoke(get_customer_appointments, context)
        tomorrow = date.fromisoformat(snapshot["dealership_local_date"]) + timedelta(days=1)
        assert all(
            datetime.fromisoformat(item["scheduled_start"]).date() != tomorrow for item in appointments["appointments"]
        )
        return sdk_result(context, AdvisorOutput(sms="Which appointment do you mean?"))

    mock_advisor_runner(monkeypatch, model)
    receipt, result = await send(sms_api, "Cancel my appointment tomorrow.")
    assert result["reply"]["body"].startswith("Which appointment do you mean?")
    assert await audit(sms_api, receipt) == {"pending": None, "results": []}


async def test_multi_turn_booking_rescheduling_and_cancellation(sms_api, sms_data, scenario):
    sessions, local, _, _, _ = sms_data
    original, start, later, _, requests = scenario
    receipt, book = await send(sms_api, "Book my Escape for an alignment")
    assert "2024 Ford Escape" in book["reply"]["body"] and local.name in book["reply"]["body"]
    assert book["reply"]["body"].startswith("Should I book") and book["reply"]["body"].count("?") == 1
    assert start.strftime("%I:%M %p %Z").lstrip("0") in book["reply"]["body"]
    pending = (await audit(sms_api, receipt))["pending"]
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Appointment).where(
            Appointment.tenant_id == local.id,
        )) == 1
    assert str(pending["proposal"]["id"]) not in book["reply"]["body"]
    confirmed, booked = await send(sms_api, "Book it")
    saved = (await audit(sms_api, receipt))["results"][0]
    assert saved["decision_message_id"] == confirmed["message_id"]
    assert saved["proposal"]["id"] == pending["proposal"]["id"]
    assert booked["reply"]["body"] == saved["reply"]
    booked_id = UUID(saved["appointment_id"])
    async with sessions() as session:
        appointment = await session.get_one(Appointment, booked_id)
        assert appointment.service_type == "Alignment" and appointment.scheduled_start == start
        assert appointment.customer_id == original.customer_id and appointment.vehicle_id == original.vehicle_id

    requests["Move my alignment"] = (
        propose_reschedule, dict(appointment_id=str(booked_id), scheduled_start=later),
    )
    _, move = await send(sms_api, "Move my alignment")
    assert "from" in move["reply"]["body"] and "to" in move["reply"]["body"]
    _, moved = await send(sms_api, "Yeah, go ahead.")
    assert "moved to" in moved["reply"]["body"]
    async with sessions() as session:
        assert (await session.get_one(Appointment, booked_id)).scheduled_start == later

    requests["Cancel my alignment"] = (propose_cancellation, dict(appointment_id=str(booked_id)))
    await send(sms_api, "Cancel my alignment")
    _, cancelled = await send(sms_api, "Please cancel it")
    assert "was cancelled" in cancelled["reply"]["body"]
    state = await audit(sms_api, receipt)
    assert state["pending"] is None
    assert {row["proposal"]["kind"] for row in state["results"]} == {"book", "reschedule", "cancel"}
    assert all(row["outcome"] == "succeeded" and UUID(row["appointment_id"]) == booked_id for row in state["results"])
    async with sessions() as session:
        assert (await session.get_one(Appointment, booked_id)).status == "cancelled"
        assert (await session.get_one(Appointment, original.id)).status == "scheduled"
    history = (await sms_api.get(history_path(receipt), headers=LOCAL_HEADERS)).json()
    assert len(history) == 14 and history[-1]["body"] == cancelled["reply"]["body"]


async def test_unclear_reply_repeats_prompt_and_natural_decline_retires_it(sms_api, sms_data, scenario):
    original, _, _, _, _ = scenario
    receipt, proposed = await send(sms_api, "Cancel my oil change")
    for text in ("Maybe", "Wait, which appointment?", "Yes, if it's free", "Yes. No, hold on."):
        _, result = await send(sms_api, text)
        assert result["reply"]["body"] == proposed["reply"]["body"]
        assert result["tool_calls"][0]["outcome"] == "confirmation_required"
        diagnostic, = result["confirmation_attempts"]
        assert diagnostic["interpretation"] == "decided" and diagnostic["decision"] == "clarify"
        assert diagnostic["authorization"] == "accepted" and diagnostic["error_code"] is None
    _, declined = await send(sms_api, "No, thanks.")
    assert declined["tool_calls"][0]["outcome"] == "declined"
    assert declined["confirmation_attempts"][0]["decision"] == "decline"
    _, stale = await send(sms_api, "Yes, please.")
    assert stale["tool_calls"] == []
    state = await audit(sms_api, receipt)
    assert state["pending"] is None and state["results"][0]["outcome"] == "declined"
    async with sms_data[0]() as session:
        assert (await session.get_one(Appointment, original.id)).status == "scheduled"


@pytest.mark.parametrize("deliver_clarification", [True, False])
async def test_hesitation_preserves_proposal_and_later_assent_requires_clarification_delivery(
    monkeypatch, sms_api, sms_data, scenario, deliver_clarification,
):
    original, start, _, _, _ = scenario
    sessions = sms_data[0]
    receipt, _ = await send(sms_api, "Move my oil change")
    initial = (await audit(sms_api, receipt))["pending"]
    clarification = "Take your time. Let me know if you'd like me to move it."
    calls = []

    async def model(_, context, model_input):
        data = json.loads(model_input)
        calls.append(data["current_inbound_sms"])
        if len(calls) == 1:
            await invoke(interpret_appointment_reply, context, decision="clarify", clarification_sms=clarification)
        else:
            assert data["active_confirmation_question"] == clarification
            await invoke(interpret_appointment_reply, context, decision="confirm")
        return ModelReply(None, 1, 1)

    monkeypatch.setattr(execution, "run_advisor", model)
    with monkeypatch.context() as patch:
        if not deliver_clarification:
            async def hold_delivery(*args, **kwargs):
                return False

            patch.setattr("servicepilot.conversations.routes.deliver_next", hold_delivery)
        _, clarified = await send(sms_api, "umm maybe, give me a bit")
    assert clarified["reply"]["body"] == clarification
    assert clarified["confirmation_attempts"][0]["action_outcome"] == "confirmation_required"
    state = await audit(sms_api, receipt)
    refreshed = state["pending"]
    assert refreshed["proposal"] == {**initial["proposal"], "prompt_id": clarified["reply"]["id"]}
    assert refreshed["expires_at"] == initial["expires_at"]
    assert refreshed["prompt_delivery_status"] == ("delivered" if deliver_clarification else "pending")
    assert not state["results"]
    async with sessions() as session:
        conversation = await session.get_one(Conversation, UUID(receipt["conversation_id"]))
        assert conversation.conditional_request is None
        assert (await session.get_one(Appointment, original.id)).scheduled_start == original.scheduled_start

    _, agreed = await send(sms_api, "Yes, please move it.")
    diagnostic, = agreed["confirmation_attempts"]
    assert diagnostic["prompt_id"] == clarified["reply"]["id"]
    state = await audit(sms_api, receipt)
    assert state["pending"] is None
    if deliver_clarification:
        assert calls == ["umm maybe, give me a bit", "Yes, please move it."]
        assert diagnostic["action_outcome"] == "succeeded"
        assert state["results"][0]["appointment_id"] == str(original.id)
    else:
        assert calls == ["umm maybe, give me a bit"]
        assert diagnostic["action_outcome"] == "delivery_unverified"
        assert state["results"][0]["outcome"] == "superseded"
    async with sessions() as session:
        expected_start = start if deliver_clarification else original.scheduled_start
        assert (await session.get_one(Appointment, original.id)).scheduled_start == expected_start


@pytest.mark.parametrize("failure", ["missing", "invalid", "duplicate", "tool_error", "refused"])
async def test_confirmation_contract_failures_never_publish_or_mutate(
    monkeypatch, sms_api, sms_data, scenario, failure,
):
    original, start, *_ = scenario
    proposal_receipt, proposed = await send(sms_api, "Move my oil change")
    proposal_id = (await audit(sms_api, proposal_receipt))["pending"]["proposal"]["id"]
    valid_model = execution.run_advisor

    async def failed_model(settings, context, model_input):
        if failure == "invalid":
            await invoke(interpret_appointment_reply, context, decision="maybe")
        elif failure == "duplicate":
            await invoke(interpret_appointment_reply, context, decision="confirm")
            await invoke(interpret_appointment_reply, context, decision="decline")
        elif failure in ("tool_error", "refused"):
            context.confirmation.interpretation = failure
            if failure == "tool_error":
                raise RuntimeError("fixture tool failure")
        return ModelReply("The appointment is moved.", 2, 2)

    monkeypatch.setattr(execution, "run_advisor", failed_model)
    receipt, failed = await send(sms_api, "ye go ahead")
    assert failed["status"] == "failed" and failed["reply"] is None
    assert failed["error_code"] == f"confirmation_{failure}"
    assert (failed["next_attempt_at"] is not None) == (failure != "refused")
    diagnostic, = failed["confirmation_attempts"]
    assert diagnostic["attempt"] == 1 and diagnostic["interpretation"] == failure
    assert diagnostic["decision"] is None and diagnostic["authorization"] is None
    assert diagnostic["proposal_id"] == proposal_id and diagnostic["prompt_id"] == proposed["reply"]["id"]
    assert diagnostic["error_code"] == failed["error_code"]
    assert (await audit(sms_api, receipt))["results"] == []
    async with sms_data[0]() as session:
        assert (await session.get_one(Appointment, original.id)).scheduled_start == original.scheduled_start

    monkeypatch.setattr(execution, "run_advisor", valid_model)
    retried = await process(sms_api, receipt)
    if failure == "refused":
        assert retried["confirmation_attempts"] == [diagnostic]
    else:
        failed_attempt, confirmed = retried["confirmation_attempts"]
        assert failed_attempt == diagnostic
        assert confirmed["attempt"] == 2 and confirmed["decision"] == "confirm"
        assert confirmed["authorization"] == "accepted" and confirmed["action_outcome"] == "succeeded"
        assert retried["status"] == "completed"
        async with sms_data[0]() as session:
            assert (await session.get_one(Appointment, original.id)).scheduled_start == start


async def test_revision_retry_reuses_retired_proposal_without_reinterpreting(monkeypatch, sms_api, sms_data, scenario):
    original, _, later, *_ = scenario
    receipt, _ = await send(sms_api, "Move my oil change")
    old_id = (await audit(sms_api, receipt))["pending"]["proposal"]["id"]
    model = execution.run_advisor

    async def interrupt_revision(settings, context, model_input):
        if json.loads(model_input)["revision_of"] is not None:
            raise TimeoutError
        return await model(settings, context, model_input)

    monkeypatch.setattr(execution, "run_advisor", interrupt_revision)
    revision_receipt, interrupted = await send(sms_api, "Yes, but make it an hour later.")
    assert interrupted["status"] == "failed" and interrupted["error_code"] == "timeout"
    assert interrupted["confirmation_attempts"][0]["decision"] == "revise"
    state = await audit(sms_api, receipt)
    assert state["pending"] is None and state["results"][0]["proposal"]["id"] == old_id

    async def resume_revision(settings, context, model_input):
        assert context.pending is None
        assert json.loads(model_input)["revision_of"]["appointment_id"] == str(original.id)
        return await model(settings, context, model_input)

    monkeypatch.setattr(execution, "run_advisor", resume_revision)
    resumed = await process(sms_api, revision_receipt)
    assert resumed["status"] == "completed"
    assert resumed["confirmation_attempts"][1]["interpretation"] == "recovered"
    state = await audit(sms_api, receipt)
    assert state["pending"]["proposal"]["id"] != old_id
    assert datetime.fromisoformat(state["pending"]["proposal"]["scheduled_start"]) == later
    async with sms_data[0]() as session:
        assert (await session.get_one(Appointment, original.id)).scheduled_start == original.scheduled_start


async def test_revision_invalidates_old_proposal_even_if_referenced_after_new_prompt(sms_api, sms_data, scenario):
    original, _, later, _, _ = scenario
    receipt, first = await send(sms_api, "Move my oil change")
    prior = (await audit(sms_api, receipt))["pending"]["proposal"]["id"]
    _, revised = await send(sms_api, "Yes, but make it an hour later.")
    assert first["reply"]["body"] != revised["reply"]["body"]
    assert (await audit(sms_api, receipt))["pending"]["proposal"]["id"] != prior
    _, rejected = await send(sms_api, "Yes, the first time you suggested")
    assert rejected["tool_calls"][0]["outcome"] == "confirmation_required"
    assert rejected["confirmation_attempts"][0]["decision"] == "clarify"
    saved = (await audit(sms_api, receipt))["results"][0]
    assert saved["outcome"] == "superseded" and saved["proposal"]["id"] == prior
    async with sms_data[0]() as session:
        assert (await session.get_one(Appointment, original.id)).scheduled_start == original.scheduled_start
    await send(sms_api, "Sounds good.")
    async with sms_data[0]() as session:
        assert (await session.get_one(Appointment, original.id)).scheduled_start == later


async def test_revision_without_replacement_retires_old_proposal(sms_api, sms_data, scenario):
    original, *_ = scenario
    receipt, _ = await send(sms_api, "Move my oil change")
    _, revise = await send(sms_api, "Actually, make it next month")
    assert revise["reply"]["body"] == "What would you like to change?"
    await send(sms_api, "Yes, please.")
    state = await audit(sms_api, receipt)
    assert state["pending"] is None and state["results"][0]["outcome"] == "superseded"
    async with sms_data[0]() as session:
        assert (await session.get_one(Appointment, original.id)).scheduled_start == original.scheduled_start


@pytest.mark.parametrize("change", ["id", "scheduled_start"])
async def test_model_decision_cannot_authorize_a_changed_proposal(monkeypatch, sms_api, sms_data, scenario, change):
    original, _, later, *_ = scenario
    receipt, _ = await send(sms_api, "Move my oil change")
    proposal_id = (await audit(sms_api, receipt))["pending"]["proposal"]["id"]
    changed_value = str(uuid4()) if change == "id" else later.isoformat()

    async def replace_during_model_call(settings, context, model_input):
        assert str(context.pending.id) == proposal_id
        assert proposal_id not in model_input
        schema = interpret_appointment_reply.params_json_schema
        assert set(schema["properties"]) == set(schema["required"]) == {
            "decision", "condition", "clarification_sms", "explicit_proposal_reference",
        }
        assert schema["additionalProperties"] is False
        assert await invoke(
            interpret_appointment_reply, context, decision="confirm", condition=None, clarification_sms=None,
            explicit_proposal_reference=False,
        ) == "intent_recorded"
        async with sms_data[0].begin() as session:
            conversation = await session.get_one(Conversation, UUID(receipt["conversation_id"]))
            conversation.pending_action_payload = {**conversation.pending_action_payload, change: changed_value}
        return ModelReply("I moved it.", 2, 2)

    monkeypatch.setattr(execution, "run_advisor", replace_during_model_call)
    _, result = await send(sms_api, "Yeah, go ahead.")
    assert result["status"] == "completed", result
    assert result["tool_calls"] == [{"name": "appointment_confirmation", "outcome": "stale_confirmation"}]
    diagnostic, = result["confirmation_attempts"]
    assert diagnostic["decision"] == "confirm" and diagnostic["authorization"] == "rejected"
    assert diagnostic["proposal_id"] == proposal_id
    assert result["reply"]["body"] == "Please reply to the latest appointment confirmation text."
    state = await audit(sms_api, receipt)
    assert state["pending"]["proposal"][change] == changed_value and state["results"] == []
    async with sms_data[0]() as session:
        assert (await session.get_one(Appointment, original.id)).scheduled_start == original.scheduled_start


async def test_confirm_is_rejected_if_identity_changes_during_interpretation(
    monkeypatch, sms_api, sms_data, scenario,
):
    original, *_ = scenario
    sessions, local, _, customer, _ = sms_data
    await send(sms_api, "Cancel my oil change")

    async def confirm_after_identity_change(settings, context, model_input):
        await invoke(interpret_appointment_reply, context, decision="confirm")
        async with sessions.begin() as session:
            session.add(Customer(tenant_id=local.id, name="New ambiguity", phone_number=customer.phone_number))
        return ModelReply(None, 2, 2)

    monkeypatch.setattr(execution, "run_advisor", confirm_after_identity_change)
    _, result = await send(sms_api, "ye go ahead")
    diagnostic, = result["confirmation_attempts"]
    assert diagnostic["decision"] == "confirm" and diagnostic["authorization"] == "rejected"
    assert diagnostic["action_outcome"] == "identity_required"
    assert result["error_code"] == "identity_changed" and result["reply"] is None
    async with sessions() as session:
        assert (await session.get_one(Appointment, original.id)).status == "scheduled"


async def test_expired_proposal_never_mutates(sms_api, sms_data, scenario):
    original, _, _, _, _ = scenario
    receipt, _ = await send(sms_api, "Cancel my oil change")
    async with sms_data[0].begin() as session:
        await session.execute(update(Conversation).where(Conversation.id == UUID(receipt["conversation_id"])).values({
            "pending_action_expires_at": datetime.now(UTC) - timedelta(seconds=1),
        }))
    _, result = await send(sms_api, "Yes, please.")
    diagnostic, = result["confirmation_attempts"]
    assert diagnostic["interpretation"] == "not_run"
    assert diagnostic["authorization"] == "rejected" and diagnostic["action_outcome"] == "expired"
    state = await audit(sms_api, receipt)
    assert state["pending"] is None
    assert state["results"][0]["outcome"] == "expired"
    async with sms_data[0]() as session:
        assert (await session.get_one(Appointment, original.id)).status == "scheduled"
    _, fresh = await send(sms_api, "Cancel my oil change")
    assert fresh["reply"]["body"].startswith("Should I cancel")


async def test_concurrent_confirmation_and_duplicate_delivery_apply_once(sms_api, sms_data, scenario):
    original, _, _, _, _ = scenario
    receipt, _ = await send(sms_api, "Cancel my oil change")
    body = "Please cancel it"
    first = await simulate(sms_api, body=body, external_message_id="conf-1")
    duplicate = await simulate(sms_api, body=body, external_message_id="conf-1")
    second = await simulate(sms_api, body=body, external_message_id="conf-2")
    assert duplicate["status"] == "duplicate" and duplicate["message_id"] == first["message_id"]
    first_reply, second_reply = await asyncio.gather(*[
        sms_api.post(agent_path(item), headers=LOCAL_HEADERS) for item in (first, second)
    ])
    assert first_reply.status_code == 200
    assert second_reply.status_code in (200, 409)
    assert (await process(sms_api, duplicate))["reply"]["body"] == first_reply.json()["reply"]["body"]
    assert (await process(sms_api, second))["tool_calls"] == []
    state = await audit(sms_api, receipt)
    assert len(state["results"]) == 1 and state["results"][0]["outcome"] == "succeeded"
    async with sms_data[0]() as session:
        assert (await session.get_one(Appointment, original.id)).status == "cancelled"


async def test_retry_recovers_committed_mutation_after_crash_before_outbound(monkeypatch, sms_api, sms_data, scenario):
    original, start, _, _, _ = scenario
    receipt, _ = await send(sms_api, "Move my oil change")
    inbound = await simulate(sms_api, body="Yes, please.")

    async def crash(*args, **kwargs):
        raise RuntimeError("crash with business transaction already committed")

    with monkeypatch.context() as patches:
        patches.setattr(execution, "_finish", crash)
        with pytest.raises(RuntimeError, match="business transaction already committed"):
            await process(sms_api, inbound)

    sessions = sms_data[0]
    async with sessions.begin() as session:
        moved = await session.get_one(Appointment, original.id)
        assert moved.scheduled_start == start
        version = moved.updated_at
        row = await session.get_one(AgentExecution, UUID(inbound["message_id"]))
        assert row.reply_id is None and row.status == "running"
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert (await audit(sms_api, receipt))["results"][0]["outcome"] == "succeeded"

    monkeypatch.setattr(execution, "run_advisor", crash)
    retried = await process(sms_api, inbound)
    assert retried["attempts"] == 2 and "moved to" in retried["reply"]["body"]
    first, recovered = retried["confirmation_attempts"]
    assert (first["decision"], first["action_outcome"]) == ("confirm", "succeeded")
    assert (recovered["interpretation"], recovered["action_outcome"]) == ("recovered", "succeeded")
    async with sessions() as session:
        moved = await session.get_one(Appointment, original.id)
        assert moved.updated_at == version


async def test_expired_and_superseded_attempts_cannot_confirm(sms_api, sms_data, scenario):
    original, _, _, _, _ = scenario
    receipt, _ = await send(sms_api, "Cancel my oil change")
    inbound = await simulate(sms_api, body="Yes, please.")
    sessions, local, *_ = sms_data
    conversation_id, inbound_id = UUID(receipt["conversation_id"]), UUID(inbound["message_id"])
    async with sessions.begin() as session:
        conversation = await lock_conversation(session, local.id, conversation_id)
        row = await session.get_one(AgentExecution, inbound_id)
        message = await session.get_one(Message, inbound_id)
        row.status = "running"
        row.attempts = 1
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        proposal = pending_proposal(conversation)
        assert await handle_confirmation(
            session, conversation, row, 1, message, expected_proposal=proposal, decision="confirm",
        ) == ("stale_attempt", None)
        row.attempts = 2
        row.lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
        assert await handle_confirmation(
            session, conversation, row, 1, message, expected_proposal=proposal, decision="confirm",
        ) == ("stale_attempt", None)
        assert (await session.get_one(Appointment, original.id)).status == "scheduled"


@pytest.mark.parametrize("timestamp", ["occurred_at", "created_at"])
async def test_confirmation_with_timestamp_before_prompt_is_stale(sms_api, sms_data, scenario, timestamp):
    original, _, _, _, _ = scenario
    receipt, _ = await send(sms_api, "Cancel my oil change")
    inbound = await simulate(sms_api, body="Yes, please.")
    async with sms_data[0].begin() as session:
        message = await session.get_one(Message, UUID(inbound["message_id"]))
        setattr(message, timestamp, getattr(message, timestamp) - timedelta(minutes=5))
    rejected = await process(sms_api, inbound)
    assert rejected["tool_calls"][0]["outcome"] == "stale_confirmation"
    assert (await audit(sms_api, receipt))["pending"]["status"] == "awaiting_confirmation"
    async with sms_data[0]() as session:
        assert (await session.get_one(Appointment, original.id)).status == "scheduled"


@pytest.mark.parametrize("phone", [UNKNOWN_PHONE, AMBIGUOUS_PHONE])
async def test_no_mutations_without_unique_customer_match(sms_api, sms_data, scenario, phone):
    original, *_ = scenario
    _, _, _, customer, _ = sms_data
    body = f"Cancel my oil change. My name is {customer.name} and my customer ID is {customer.id}."
    scenario[-1][body] = (propose_cancellation, dict(appointment_id=str(original.id)))
    receipt = await simulate(sms_api, body=body, phone=phone)
    result = await process(sms_api, receipt)
    assert result["tool_calls"][0]["outcome"] == "identity_required"
    assert "contact the dealership" in result["reply"]["body"]
    assert (await audit(sms_api, receipt))["pending"] is None
    assert (await sms_api.get(audit_path(receipt), headers=FOREIGN_HEADERS)).status_code == 404
    async with sms_data[0]() as session:
        assert (await session.get_one(Appointment, original.id)).status == "scheduled"


async def test_revalidation_blocks_stale_identity_and_foreign_or_unowned_appointments(sms_api, sms_data, scenario):
    original, _, _, _, requests = scenario
    sessions, local, foreign, customer, _ = sms_data
    async with sessions() as session:
        foreign_record = await session.scalar(select(Appointment).where(Appointment.tenant_id == foreign.id))
        other = await session.scalar(select(Customer).where(
            Customer.tenant_id == local.id, Customer.id != customer.id,
        ).limit(1))
    async with sessions.begin() as session:
        vehicle = Vehicle(
            tenant_id=local.id, customer_id=other.id, vin="5" * 17, make="Ford", model="Focus", model_year=2024,
        )
        session.add(vehicle)
        await session.flush()
        other_record = Appointment(
            tenant_id=local.id, customer_id=other.id, vehicle_id=vehicle.id,
            scheduled_start=original.scheduled_start, scheduled_end=original.scheduled_end, service_type="Private",
        )
        session.add(other_record)
    for record in (foreign_record, other_record):
        requests["Cancel different appointment"] = (propose_cancellation, dict(appointment_id=str(record.id)))
        _, result = await send(sms_api, "Cancel different appointment")
        assert result["tool_calls"][0]["outcome"] == "not_found"

    receipt, _ = await send(sms_api, "Cancel my oil change")
    inbound = await simulate(sms_api, body="Yes, please.")
    async with sessions.begin() as session:
        session.add(Customer(tenant_id=local.id, name="New ambiguity", phone_number=customer.phone_number))
    result = await process(sms_api, inbound)
    assert result["tool_calls"][0]["outcome"] == "identity_required"
    diagnostic, = result["confirmation_attempts"]
    assert diagnostic["interpretation"] == "not_run"
    assert diagnostic["authorization"] == "rejected" and diagnostic["action_outcome"] == "identity_required"
    assert (await audit(sms_api, receipt))["results"][0]["outcome"] == "identity_required"
    async with sessions() as session:
        assert (await session.get_one(Appointment, original.id)).status == "scheduled"


@pytest.mark.parametrize("kind", ["book", "reschedule", "cancelled", "completed"])
async def test_conflicting_slot_or_changed_source_after_proposal(sms_api, sms_data, scenario, kind):
    original, start, _, _, _ = scenario
    sessions = sms_data[0]
    body = {
        "book": "Book my Escape for an alignment", "reschedule": "Move my oil change",
        "cancelled": "Cancel my oil change", "completed": "Cancel my oil change",
    }[kind]
    receipt, _ = await send(sms_api, body)
    async with sessions.begin() as session:
        if kind in {"book", "reschedule"}:
            session.add(Appointment(
                tenant_id=original.tenant_id, customer_id=original.customer_id, vehicle_id=original.vehicle_id,
                scheduled_start=start, scheduled_end=start + timedelta(hours=1), service_type="Conflicting booking",
            ))
        else:
            record = await session.get_one(Appointment, original.id)
            record.status = kind
    _, result = await send(sms_api, "Yes, please.")
    state = (await audit(sms_api, receipt))["results"][0]
    assert state["outcome"] == ("unavailable" if kind in {"book", "reschedule"} else "appointment_changed")
    assert state["appointment_id"] is None and "was cancelled" not in result["reply"]["body"]
    async with sessions() as session:
        assert (await session.get_one(Appointment, original.id)).scheduled_start == original.scheduled_start
