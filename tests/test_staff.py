import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select

from servicepilot.agent import execution
from servicepilot.agent.advisor import AdvisorOutput, ModelReply
from servicepilot.agent.models import AgentExecution
from servicepilot.agent.tools import (
    ask_dealership_staff, get_available_slots, get_service_information, interpret_appointment_reply, propose_reschedule,
)
from servicepilot.appointments.service import AppointmentService
from servicepilot.db.models import Appointment, Conversation, Employee, EmployeeSession, Tenant
from servicepilot.integrations.mock_dms import MockDMSAdapter
from servicepilot.main import create_app
from servicepilot.sms.delivery import deliver_next
from servicepilot.sms.transport import SimulationTransport
from servicepilot.staff.auth import hash_password
from test_agent import agent_path, history_path, invoke, process
from test_appointment_actions import audit, scenario, send
from test_conversations import FOREIGN_HEADERS, LOCAL_HEADERS, SHARED_PHONE, UNKNOWN_PHONE, simulate, sms_api, sms_data
from test_sms_worker import settings_for, state

PASSWORD = "phase-four-integration-test"


async def sign_in(api, tenant, username, password=PASSWORD):
    return await api.post("/staff/sessions", json={
        "dealership": tenant.slug, "username": username, "password": password,
    })


@pytest.fixture
async def staff(sms_api, sms_data):
    sessions, local, foreign, *_ = sms_data
    password_hash = hash_password(PASSWORD)
    entries = (
        ("manager", local, "manager"), ("advisor", local, "advisor"),
        ("colleague", local, "advisor"), ("foreign", foreign, "manager"),
    )
    accounts = {}
    async with sessions.begin() as session:
        for username, tenant, role in entries:
            employee = Employee(
                tenant_id=tenant.id, username=username, display_name=username.title(),
                role=role, password_hash=password_hash,
            )
            session.add(employee)
            accounts[username] = employee
    result = {}
    for username, tenant, _ in entries:
        response = await sign_in(sms_api, tenant, username)
        assert response.status_code == 200, response.text
        result[username] = ({"Authorization": f"Bearer {response.json()['access_token']}"}, accounts[username])
    return result


def thread(receipt):
    return f"/staff/conversations/{receipt['conversation_id']}"


async def set_owner(api, receipt, headers, owner, version):
    return await api.put(f"{thread(receipt)}/ownership/{owner}", headers=headers, json={"expected_version": version})


async def list_consultations(api, receipt, headers):
    response = await api.get(
        "/staff/consultations", params={"conversation_id": receipt["conversation_id"]}, headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


class RecordingTransport(SimulationTransport):
    def __init__(self):
        self.bodies = []

    async def send(self, sms):
        self.bodies.append(sms.body)
        return await super().send(sms)


async def drain_delivery(api, sessions, transport):
    while await deliver_next(sessions, settings_for(api), {"simulation": transport}):
        pass


async def test_staff_validation_does_not_echo_password_input():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as api:
        response = await api.post("/staff/sessions", json={
            "dealership": "demo", "username": "advisor", "password": ["synthetic-private-value"],
        })

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "password"]
    assert "synthetic-private-value" not in response.text


async def test_employee_sessions_roles_expiry_and_tenant_isolation(sms_api, sms_data, staff):
    sessions, local, _, *_ = sms_data
    manager, _ = staff["manager"]
    advisor, employee = staff["advisor"]
    foreign, _ = staff["foreign"]
    assert (await sign_in(sms_api, local, "advisor", "wrong")).status_code == 401
    assert (await sms_api.get("/staff/me", headers=LOCAL_HEADERS)).status_code == 401
    profile = await sms_api.get("/staff/me", headers=advisor)
    assert profile.json()["id"] == str(employee.id)
    assert "password_hash" not in profile.text

    receipt = await simulate(sms_api, body="Need service")
    path = thread(receipt)
    for suffix in ("", "/notes", "/messages", "/ownership-events"):
        assert (await sms_api.get(path + suffix, headers=foreign)).status_code == 404
    assert (await sms_api.get("/staff/conversations", headers=foreign)).json() == []
    assert (await set_owner(sms_api, receipt, foreign, "human", 0)).status_code == 404

    account = {"username": "new", "display_name": "New advisor", "password": PASSWORD}
    assert (await sms_api.post("/staff/employees", headers=advisor, json=account)).status_code == 403
    assert (await sms_api.post("/staff/employees", headers=manager, json=account)).status_code == 201
    assert (await sms_api.patch(
        f"/staff/employees/{employee.id}", headers=manager, json={"disabled": True},
    )).status_code == 200
    assert (await sms_api.get("/staff/me", headers=advisor)).status_code == 401
    assert (await sign_in(sms_api, local, "advisor")).status_code == 401
    colleague, colleague_account = staff["colleague"]
    async with sessions.begin() as session:
        token = await session.scalar(select(EmployeeSession).where(EmployeeSession.employee_id == colleague_account.id))
        token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert (await sms_api.get("/staff/me", headers=colleague)).status_code == 401
    assert (await sms_api.delete("/staff/sessions/current", headers=manager)).status_code == 204
    assert (await sms_api.get("/staff/me", headers=manager)).status_code == 401


async def test_takeover_fences_running_and_queued_ai_and_handback_retains_unanswered_sms(
    monkeypatch, sms_api, sms_data, staff,
):
    sessions = sms_data[0]
    settings = settings_for(sms_api)
    advisor, _ = staff["advisor"]
    colleague, _ = staff["colleague"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def model(_, context, model_input):
        body = json.loads(model_input)["current_inbound_sms"]
        if body == "In flight" and context.attempt == 1:
            entered.set()
            await release.wait()
        return ModelReply(f"AI response to {body}", 1, 1)

    monkeypatch.setattr(execution, "run_advisor", model)
    first = await simulate(sms_api, body="Queued")
    assert await execution.process_next(sessions, settings)
    second = await simulate(sms_api, body="In flight")
    worker = asyncio.create_task(execution.process_next(sessions, settings))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        updates = await asyncio.gather(*(
            set_owner(sms_api, first, headers, "human", 0) for headers in (advisor, colleague)
        ))
        assert sorted(response.status_code for response in updates) in ([200, 403], [200, 409])
        owner = advisor if updates[0].status_code == 200 else colleague
        third = await simulate(sms_api, body="Arrived during takeover")
        assert not await execution.process_next(sessions, settings)
        assert (await state(sms_api, third))["status"] == "pending"
    finally:
        release.set()
        await worker
    assert (await state(sms_api, second))["reply"] is None
    transport = RecordingTransport()
    await drain_delivery(sms_api, sessions, transport)
    assert transport.bodies == []

    payload = {"body": "I'm the service advisor handling this.", "request_id": str(uuid4())}
    replies = [
        await sms_api.post(thread(first) + "/replies", headers=owner, json=payload) for _ in range(2)
    ]
    assert all(response.status_code == 201 for response in replies)
    assert replies[0].json()["id"] == replies[1].json()["id"]
    assert (await state(sms_api, second))["error_code"] == "addressed_by_employee"

    unanswered = await simulate(sms_api, body="Another question after staff replied")
    assert (await set_owner(sms_api, first, owner, "ai", 1)).status_code == 200
    assert await execution.process_next(sessions, settings)
    assert (await state(sms_api, unanswered))["status"] == "completed"
    await drain_delivery(sms_api, sessions, transport)
    assert transport.bodies == [payload["body"], "AI response to Another question after staff replied"]
    assert not await execution.process_next(sessions, settings)


async def test_takeover_between_outbox_claim_and_send(monkeypatch, sms_api, sms_data, staff):
    from contextlib import asynccontextmanager

    from servicepilot.sms import delivery

    sessions = sms_data[0]
    manager, _ = staff["manager"]
    claimed, release = asyncio.Event(), asyncio.Event()
    real_fence = delivery.delivery_fence

    @asynccontextmanager
    async def paused_fence(*args):
        claimed.set()
        await release.wait()
        async with real_fence(*args) as connection:
            yield connection

    async def model(*_):
        return ModelReply("Stale answer", 1, 1)

    monkeypatch.setattr(execution, "run_advisor", model)
    monkeypatch.setattr(delivery, "delivery_fence", paused_fence)
    receipt = await simulate(sms_api)
    settings = settings_for(sms_api)
    assert await execution.process_next(sessions, settings)
    transport = RecordingTransport()
    worker = asyncio.create_task(deliver_next(sessions, settings, {"simulation": transport}))
    try:
        await asyncio.wait_for(claimed.wait(), 5)
        assert (await set_owner(sms_api, receipt, manager, "human", 0)).status_code == 200
        assert (await set_owner(sms_api, receipt, manager, "ai", 1)).status_code == 200
    finally:
        release.set()
        await worker
    assert transport.bodies == []


async def test_consultation_answer_is_private_until_resumed_and_reclaims_expired_lease(
    monkeypatch, sms_api, sms_data, staff,
):
    sessions, _, _, customer, _ = sms_data
    manager, _ = staff["manager"]
    advisor, advisor_account = staff["advisor"]
    colleague, _ = staff["colleague"]
    foreign, _ = staff["foreign"]
    fact = "There is no rescheduling fee."
    private = "Private service team note. Do not quote this."
    question = "Can the service team verify the applicable fee?"
    model_inputs = []

    async def model(_, context, model_input):
        model_inputs.append(json.loads(model_input))
        assert private not in model_input
        if context.staff_fact is None:
            assert (await invoke(ask_dealership_staff, context, question=question))["status"] == "consultation_prepared"
            assert (await invoke(ask_dealership_staff, context, question=question))["status"] == "consultation_staged"
            await invoke(ask_dealership_staff, context, question="Is there a grace period?")
            return ModelReply("I can help move your appointment. Let me check the fee with our team.", 1, 1)
        assert context.staff_fact == fact and context.pending is None and context.awaiting_staff
        return ModelReply("Good news, we can move your appointment for free.", 1, 1)

    monkeypatch.setattr(execution, "run_advisor", model)
    settings = settings_for(sms_api)
    receipt = await simulate(sms_api, body="Does it cost extra to reschedule?")
    path = thread(receipt)
    assert (await sms_api.put(path + "/assignment", headers=manager, json={
        "employee_id": str(advisor_account.id),
    })).status_code == 200
    note = await sms_api.post(path + "/notes", headers=advisor, json={"body": private})
    assert note.status_code == 201
    assert await execution.process_next(sessions, settings)
    row = (await sms_api.get(path, headers=advisor)).json()
    assert row["owner"] == "ai" and row["waiting_for_staff"]
    questions = await list_consultations(sms_api, receipt, advisor)
    assert len(questions) == 2
    item = next(item for item in questions if item["question"] == question)
    answer_path = path + f"/consultations/{item['id']}/answer"
    for headers, status in ((foreign, 404), (colleague, 403)):
        assert (await sms_api.put(answer_path, headers=headers, json={"public_fact": fact})).status_code == status
    response = await sms_api.put(answer_path, headers=advisor, json={"public_fact": fact})
    assert response.status_code == 200 and response.json()["status"] == "answered"
    assert (await sms_api.put(answer_path, headers=advisor, json={"public_fact": fact})).json() == response.json()

    async with sessions.begin() as session:
        source = await session.get_one(AgentExecution, UUID(item["id"]))
        assert source.resume_source_id == UUID(receipt["message_id"])
        source.status, source.attempts = "running", 1
        source.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await execution.process_next(sessions, settings)
    assert not await execution.process_next(sessions, settings)
    assert {item["status"] for item in await list_consultations(sms_api, receipt, advisor)} == {"resolved", "open"}
    transport = RecordingTransport()
    await drain_delivery(sms_api, sessions, transport)
    assert transport.bodies == [
        "I can help move your appointment. Let me check the fee with our team.",
        "Good news, we can move your appointment for free.",
    ]
    history = await sms_api.get(history_path(receipt), headers=LOCAL_HEADERS)
    for text in (private, question, "Is there a grace period?"):
        assert text not in history.text
    assert len(model_inputs) == 2
    async with sessions() as session:
        assert await session.scalar(select(func.count(Appointment.id)).where(
            Appointment.customer_id == customer.id, Appointment.status == "scheduled",
        )) == 1


@pytest.mark.parametrize(("already_resumed", "withdrawal_sms"), [
    (False, "New response"), (True, "New response"), (False, None),
])
async def test_semantic_change_invalidates_old_consultation_work(
    monkeypatch, sms_api, sms_data, staff, already_resumed, withdrawal_sms,
):
    sessions = sms_data[0]
    settings = settings_for(sms_api)
    advisor, _ = staff["advisor"]
    fact = "There is an old fee."

    async def model(_, context, model_input):
        payload = json.loads(model_input)
        body = payload["current_inbound_sms"]
        if body == "Original request" and context.staff_fact is None:
            await invoke(ask_dealership_staff, context, question="What is the fee?")
            return ModelReply("I'll check on the fee.", 1, 1)
        if body == "Original request":
            return ModelReply("There is an old fee.", 1, 1)
        assert payload["consultations"][0]["staff_question"] == "What is the fee?"
        consultation_id, = [item["id"] for item in context.consultations]
        draft = AdvisorOutput(sms=withdrawal_sms, superseded_consultations=[consultation_id])
        return ModelReply(draft.sms, 1, 1, draft)

    monkeypatch.setattr(execution, "run_advisor", model)
    receipt = await simulate(sms_api, body="Original request")
    assert await execution.process_next(sessions, settings)
    item, = await list_consultations(sms_api, receipt, advisor)
    response = await sms_api.put(thread(receipt) + f"/consultations/{item['id']}/answer", headers=advisor, json={
        "public_fact": fact,
    })
    assert response.status_code == 200
    if already_resumed:
        assert await execution.process_next(sessions, settings)
    later = await simulate(sms_api, body="Forget the fee question. I need something else.")
    while await execution.process_next(sessions, settings):
        pass
    transport = RecordingTransport()
    await drain_delivery(sms_api, sessions, transport)
    assert transport.bodies == ([withdrawal_sms] if withdrawal_sms else [])
    updated, = await list_consultations(sms_api, receipt, advisor)
    assert updated["status"] == "canceled"
    assert (await state(sms_api, later))["status"] == ("completed" if withdrawal_sms else "failed")


@pytest.mark.parametrize("answer_timing", ["before", "during", "after"])
async def test_relevant_update_keeps_staff_work(monkeypatch, sms_api, sms_data, staff, answer_timing):
    sessions = sms_data[0]
    settings = settings_for(sms_api)
    advisor, _ = staff["advisor"]
    processing = asyncio.Event()
    release = asyncio.Event()

    async def model(_, context, model_input):
        payload = json.loads(model_input)
        if context.staff_fact:
            assert payload["latest_customer_sms_during_wait"] == "Any update?"
            return ModelReply("The part arrived this morning.", 1, 1)
        if payload["current_inbound_sms"] == "Any update?":
            assert context.consultations
            processing.set()
            if answer_timing == "during":
                await release.wait()
            return ModelReply("I'm checking with the parts team.", 1, 1)
        await invoke(ask_dealership_staff, context, question="Did the part arrive?")
        return ModelReply("I'll ask the parts team.", 1, 1)

    monkeypatch.setattr(execution, "run_advisor", model)
    receipt = await simulate(sms_api, body="Has my part arrived?")
    assert await execution.process_next(sessions, settings)
    item, = await list_consultations(sms_api, receipt, advisor)

    async def answer():
        result = await sms_api.put(f"{thread(receipt)}/consultations/{item['id']}/answer", headers=advisor,
                                   json={"public_fact": "The part arrived this morning."})
        assert result.status_code == 200

    if answer_timing == "before":
        await answer()
    await simulate(sms_api, body="Any update?")
    update_task = asyncio.create_task(execution.process_next(sessions, settings))
    if answer_timing == "during":
        try:
            await asyncio.wait_for(processing.wait(), timeout=5)
            await answer()
        finally:
            release.set()
    assert await update_task
    if answer_timing == "after":
        await answer()
    assert await execution.process_next(sessions, settings)
    assert not await execution.process_next(sessions, settings)
    item, = await list_consultations(sms_api, receipt, advisor)
    assert item["status"] == "resolved"
    transport = RecordingTransport()
    await drain_delivery(sms_api, sessions, transport)
    assert transport.bodies == [
        "I'll ask the parts team.", "I'm checking with the parts team.", "The part arrived this morning.",
    ]


async def test_inflight_resumption_yields_to_customer_withdrawal(monkeypatch, sms_api, sms_data, staff):
    sessions = sms_data[0]
    settings = settings_for(sms_api)
    advisor, _ = staff["advisor"]
    resuming, release = asyncio.Event(), asyncio.Event()

    async def model(_, context, model_input):
        if context.staff_fact:
            resuming.set()
            await release.wait()
            return ModelReply("The part arrived.", 1, 1)
        if json.loads(model_input)["current_inbound_sms"] == "Has the part arrived?":
            await invoke(ask_dealership_staff, context, question="Did the part arrive?")
            return ModelReply("I'll ask the parts team.", 1, 1)
        return ModelReply("Okay, I'll drop that request.", 1, 1, AdvisorOutput(
            sms="Okay, I'll drop that request.", superseded_consultations=[context.consultations[0]["id"]],
        ))

    monkeypatch.setattr(execution, "run_advisor", model)
    receipt = await simulate(sms_api, body="Has the part arrived?")
    assert await execution.process_next(sessions, settings)
    item, = await list_consultations(sms_api, receipt, advisor)
    answer = await sms_api.put(f"{thread(receipt)}/consultations/{item['id']}/answer", headers=advisor,
                               json={"public_fact": "The part arrived."})
    assert answer.status_code == 200
    task = asyncio.create_task(execution.process_next(sessions, settings))
    try:
        await asyncio.wait_for(resuming.wait(), timeout=5)
        await simulate(sms_api, body="I don't need the part anymore.")
    finally:
        release.set()
    assert await task
    while await execution.process_next(sessions, settings):
        pass
    transport = RecordingTransport()
    await drain_delivery(sms_api, sessions, transport)
    assert transport.bodies == ["Okay, I'll drop that request."]
    async with sessions() as session:
        row = await session.get_one(AgentExecution, UUID(item["id"]))
        assert [attempt["error_code"] for attempt in row.advisor_attempts] == [
            "consultation_interrupted", "consultation_superseded",
        ]


async def test_exact_lookup_offers_verified_alternatives_for_closure_duration_and_full_slot(
    monkeypatch, sms_api, sms_data,
):
    sessions, dealer, _, customer, _ = sms_data
    future = datetime.now(UTC).date() + timedelta(days=21)
    monday = future + timedelta(days=(-future.weekday()) % 7)
    tuesday = monday + timedelta(days=1)
    zone = ZoneInfo(dealer.timezone)
    busy = datetime.combine(tuesday, dealer.opens_at, zone)
    async with sessions.begin() as session:
        row = await session.get_one(Tenant, dealer.id)
        row.closed_dates = [monday]
        row.service_catalog = {"oil change": {"name": "Oil change", "description": "Oil and filter.", "minutes": 90}}
        visit = await session.scalar(select(Appointment).where(Appointment.customer_id == customer.id))
        visit.scheduled_start, visit.scheduled_end = busy, busy + timedelta(hours=1)

    async def model(_, context, model_input):
        result = await invoke(
            get_available_slots, context, day=monday.isoformat(), requested_start=(
                datetime.combine(monday, dealer.opens_at, zone).astimezone(UTC).isoformat()
            ), service_type="Oil change",
        )
        assert not result["requested_available"] and result["unavailable_reason"] == "closed"
        assert result["day"] == monday.isoformat()
        assert result["timezone"] == zone.key and result["scope"] == "customer"
        assert datetime.fromisoformat(result["requested_start"]).date() == monday
        async with sessions() as session:
            actual = await AppointmentService(MockDMSAdapter(session, dealer.id)).availability(
                tuesday, service_type="Oil change",
            )
        starts = [datetime.fromisoformat(slot["start"]) for slot in result["slots"]]
        assert starts == [slot.start for slot in actual]
        assert starts[0] == busy + timedelta(hours=1)
        assert datetime.fromisoformat(result["slots"][0]["end"]) - starts[0] == timedelta(minutes=90)
        lunch = await invoke(get_available_slots, context, day=tuesday.isoformat(), period="lunch")
        lunch_starts = [datetime.fromisoformat(slot["start"]) for slot in lunch["slots"]]
        assert lunch_starts and all(11 <= start.hour < 14 for start in lunch_starts)
        assert lunch_starts[0].hour == 12
        first = starts[0].strftime("%A at %I:%M %p")
        return ModelReply(f"Monday isn't available. I can offer {first}.", 1, 1)

    monkeypatch.setattr(execution, "run_advisor", model)
    receipt = await simulate(sms_api, body="Is Monday morning available for an oil change?")
    result = await process(sms_api, receipt)
    assert result["status"] == "completed"
    assert "isn't available" in result["reply"]["body"] and "Tuesday" in result["reply"]["body"]


async def test_service_knowledge_comes_from_the_tenant_configuration(monkeypatch, sms_api, sms_data, staff):
    manager, _ = staff["manager"]
    advisor, _ = staff["advisor"]
    information = {
        "services": [{"name": "Tire rotation", "description": "Rotate all four tires.", "minutes": 30}],
        "policies": {"rescheduling": "There is no rescheduling fee."},
        "closed_dates": ["2099-01-05"],
    }
    assert (await sms_api.put("/staff/service-settings", headers=advisor, json=information)).status_code == 403
    assert (await sms_api.put("/staff/service-settings", headers=manager, json=information)).status_code == 200

    async def model(_, context, model_input):
        result = await invoke(get_service_information, context, day="2099-01-05")
        assert result["catalog_status"] == "configured"
        assert result["services"] == [{
            "service_id": "tire rotation", "name": "Tire rotation",
            "duration_minutes": 30, "description": "Rotate all four tires.",
        }]
        assert result["hours"]["closed"] and result["hours"]["date"] == "2099-01-05"
        assert "pricing" not in result["policies"] and result["missing_policies_are_unknown"]
        return ModelReply(result["policies"]["rescheduling"], 1, 1)

    monkeypatch.setattr(execution, "run_advisor", model)
    result = await process(sms_api, await simulate(sms_api, body="Do you charge to reschedule?"))
    assert result["reply"]["body"] == information["policies"]["rescheduling"]


@pytest.mark.parametrize("case", ["missing_staff_tool", "failed_staff_tool", "availability", "appointment"])
async def test_unsubstantiated_operational_commitment_is_not_delivered(monkeypatch, sms_api, sms_data, case):
    async def model(_, context, model_input):
        if case == "failed_staff_tool":
            failed = await invoke(ask_dealership_staff, context, question="Bad\x00question")
            assert failed["status"] == "consultation_unavailable"
        if case == "availability":
            draft = AdvisorOutput(sms="I checked, noon is free.", availability_checked=True)
        elif case == "appointment":
            draft = AdvisorOutput(sms="Your oil change is booked.", appointment_commitment="changed")
        else:
            draft = AdvisorOutput(sms="I asked staff and will follow up.", staff_commitment="new_request")
        return ModelReply(draft.sms, 1, 1, draft)

    monkeypatch.setattr(execution, "run_advisor", model)
    receipt = await simulate(sms_api, body="Can you check?")
    result = await process(sms_api, receipt)
    assert result["reply"]["body"] == execution.UNVERIFIED_OPERATION_REPLY
    inbox = await sms_api.get("/simulations/sms", headers=LOCAL_HEADERS, params={"sender_phone": SHARED_PHONE})
    assert inbox.json()["outbound"][0]["body"] == execution.UNVERIFIED_OPERATION_REPLY
    assert not inbox.json()["waiting_for_staff"]


async def test_consultation_reuses_open_answered_and_recent_resolved_work(monkeypatch, sms_api, sms_data, staff):
    sessions = sms_data[0]
    manager, _ = staff["manager"]
    question = "What is the oil change quote for this customer's appointment?"
    fact = "The oil change quote is $69.95 before tax."
    states = []
    claim_historical_followup = False

    async def model(_, context, model_input):
        if context.staff_fact is not None:
            return ModelReply(context.staff_fact, 1, 1)
        repeated_question = f" {question.upper().replace(' ', '  ')} " if states else question
        result = await invoke(ask_dealership_staff, context, question=repeated_question)
        status = result["status"]
        states.append(status)
        if status == "consultation_prepared":
            draft = AdvisorOutput(sms="I'll check the quote for you.", staff_commitment="new_request")
        elif status in ("consultation_open", "consultation_answered"):
            draft = AdvisorOutput(
                sms="I'm still waiting on the quote.", staff_commitment="existing_request",
                consultation_reference=result["consultation_id"],
            )
        else:
            assert status == "consultation_resolved" and result["approved_fact"] == fact
            if claim_historical_followup:
                draft = AdvisorOutput(
                    sms="I'm still waiting on the quote.", staff_commitment="existing_request",
                    consultation_reference=result["consultation_id"],
                )
            else:
                draft = AdvisorOutput(sms=result["approved_fact"])
        return ModelReply(draft.sms, 1, 1, draft)

    monkeypatch.setattr(execution, "run_advisor", model)
    receipt = await simulate(sms_api, body="How much does my oil change cost?")
    await process(sms_api, receipt)
    duplicate = await process(sms_api, await simulate(sms_api, body="Any update on the price?"))
    assert duplicate["reply"]["body"] == "I'm still waiting on the quote."
    item, = await list_consultations(sms_api, receipt, manager)
    path = thread(receipt) + f"/consultations/{item['id']}/answer"
    assert (await sms_api.put(path, headers=manager, json={"public_fact": fact})).status_code == 200
    duplicate = await process(sms_api, await simulate(sms_api, body="Did the quote come in?"))
    assert duplicate["reply"]["body"] == "I'm still waiting on the quote."
    assert await execution.process_next(sessions, settings_for(sms_api))
    await drain_delivery(sms_api, sessions, RecordingTransport())
    reused = await process(sms_api, await simulate(sms_api, body="What was the oil change quote?"))
    assert reused["reply"]["body"] == fact
    assert states == ["consultation_prepared", "consultation_open", "consultation_answered", "consultation_resolved"]
    claim_historical_followup = True
    stale = await process(sms_api, await simulate(sms_api, body="Could you confirm the earlier quote?"))
    assert stale["reply"]["body"] == execution.UNVERIFIED_OPERATION_REPLY
    assert len(await list_consultations(sms_api, receipt, manager)) == 1

    params = {"sender_phone": SHARED_PHONE}
    local = (await sms_api.get("/simulations/sms", headers=LOCAL_HEADERS, params=params)).json()
    assert not local["waiting_for_staff"]
    assert any(row["source"] == "advisor_followup" and row["body"] == fact for row in local["outbound"])
    foreign = (await sms_api.get("/simulations/sms", headers=FOREIGN_HEADERS, params=params)).json()
    assert foreign["conversation_id"] is None and foreign["outbound"] == []


async def test_consultation_reference_reuses_only_eligible_work_in_the_current_thread(
    monkeypatch, sms_api, sms_data, staff,
):
    manager, _ = staff["manager"]
    foreign_manager, _ = staff["foreign"]
    loaner_question = "Is a loaner available this Friday?"
    shuttle_question = "What time does the shuttle stop on Friday?"

    async def stage_loaner(_, context, model_input):
        result = await invoke(ask_dealership_staff, context, question=loaner_question)
        assert result == {"status": "consultation_prepared"}
        return ModelReply("I'll check whether a loaner is available.", 1, 1)

    monkeypatch.setattr(execution, "run_advisor", stage_loaner)
    receipt = await simulate(sms_api, body=loaner_question)
    await process(sms_api, receipt)
    loaner, = await list_consultations(sms_api, receipt, manager)
    unrelated = await simulate(sms_api, phone=UNKNOWN_PHONE, body=loaner_question)
    await process(sms_api, unrelated)
    other, = await list_consultations(sms_api, unrelated, manager)
    foreign = await simulate(sms_api, headers=FOREIGN_HEADERS, body=loaner_question)
    response = await sms_api.post(agent_path(foreign), headers=FOREIGN_HEADERS)
    assert response.status_code == 200, response.text
    foreign_loaner, = await list_consultations(sms_api, foreign, foreign_manager)

    async def follow_up(_, context, model_input):
        assert {str(item["id"]) for item in context.consultations} == {loaner["id"]}
        for reference in (other["id"], foreign_loaner["id"], str(uuid4())):
            assert await invoke(
                ask_dealership_staff, context, question=shuttle_question, consultation_reference=reference,
            ) == {"status": "consultation_unavailable"}
        malformed = await invoke(
            ask_dealership_staff, context, question=shuttle_question, consultation_reference="invalid-id",
        )
        assert json.loads(malformed) == {"status": "invalid_arguments"}
        assert await invoke(
            ask_dealership_staff, context, consultation_reference=loaner["id"], addresses_condition=True,
        ) == {"status": "consultation_unavailable"}
        assert await invoke(
            ask_dealership_staff, context, question="Can they lend me a car Friday?",
            consultation_reference=loaner["id"],
        ) == {"status": "consultation_open", "consultation_id": loaner["id"]}
        assert not context.consultation_questions
        draft = AdvisorOutput(
            sms="The loaner check is still open.", staff_commitment="existing_request",
            consultation_reference=loaner["id"],
        )
        return ModelReply(draft.sms, 1, 1, draft)

    monkeypatch.setattr(execution, "run_advisor", follow_up)
    result = await process(sms_api, await simulate(sms_api, body="Any word on borrowing a car Friday?"))
    assert result["reply"]["body"] == "The loaner check is still open."
    assert await list_consultations(sms_api, receipt, manager) == [loaner]

    async def stage_shuttle(_, context, model_input):
        assert str(context.consultations[0]["id"]) == loaner["id"]
        assert await invoke(ask_dealership_staff, context, question=shuttle_question) == {
            "status": "consultation_prepared",
        }
        return ModelReply("I'll check the shuttle hours.", 1, 1)

    monkeypatch.setattr(execution, "run_advisor", stage_shuttle)
    result = await process(sms_api, await simulate(sms_api, body="When does the shuttle stop Friday?"))
    assert result["reply"]["body"] == "I'll check the shuttle hours."
    items = await list_consultations(sms_api, receipt, manager)
    assert {(item["question"], item["status"]) for item in items} == {
        (loaner_question, "open"), (shuttle_question, "open"),
    }

    async def after_cancellation(_, context, model_input):
        assert any(str(item["id"]) == loaner["id"] for item in context.consultations)
        path = thread(receipt) + f"/consultations/{loaner['id']}/cancel"
        assert (await sms_api.put(path, headers=manager)).status_code == 200
        assert await invoke(
            ask_dealership_staff, context, question=loaner_question, consultation_reference=loaner["id"],
        ) == {"status": "consultation_unavailable"}
        assert not context.consultation_questions
        draft = AdvisorOutput(
            sms="The loaner check is still open.", staff_commitment="existing_request",
            consultation_reference=loaner["id"],
        )
        return ModelReply(draft.sms, 1, 1, draft)

    monkeypatch.setattr(execution, "run_advisor", after_cancellation)
    result = await process(sms_api, await simulate(sms_api, body="Did you hear anything on the loaner?"))
    assert result["reply"]["body"] == execution.UNVERIFIED_OPERATION_REPLY
    items = await list_consultations(sms_api, receipt, manager)
    assert {(item["question"], item["status"]) for item in items} == {
        (loaner_question, "canceled"), (shuttle_question, "open"),
    }


async def test_conditional_reply_retires_authorization_even_if_customer_later_agrees(
    monkeypatch, sms_api, sms_data, scenario,
):
    original, _, _, receipt, _ = scenario
    await send(sms_api, "Move my oil change")

    async def model(_, context, model_input):
        if context.pending:
            await invoke(interpret_appointment_reply, context, decision="conditional")
            return ModelReply(None, 1, 1)
        if context.requires_fresh_consent:
            assert json.loads(model_input)["revision_of"]["appointment_id"] == str(original.id)
            await invoke(ask_dealership_staff, context, question="Is there a rescheduling fee?")
            return ModelReply(None, 1, 1)
        return ModelReply("What should I check for you?", 1, 1)

    monkeypatch.setattr(execution, "run_advisor", model)
    _, conditional = await send(sms_api, "Sure, if there is no fee.")
    assert conditional["confirmation_attempts"][0]["decision"] == "conditional"
    assert conditional["reply"]["body"] == execution.STAFF_WAIT_REPLY
    _, followup = await send(sms_api, "yeah")
    assert followup["confirmation_attempts"] == []
    assert (await audit(sms_api, receipt))["pending"] is None
    async with sms_data[0]() as session:
        appointment = await session.get_one(Appointment, original.id)
        assert appointment.scheduled_start == original.scheduled_start
        assert appointment.status == "scheduled"


async def test_unrelated_reply_answers_question_and_preserves_the_exact_proposal(
    monkeypatch, sms_api, sms_data, scenario,
):
    original, _, _, receipt, _ = scenario
    _, proposed = await send(sms_api, "Move my oil change")
    active = (await audit(sms_api, receipt))["pending"]

    async def model(_, context, model_input):
        if context.pending is not None:
            assert str(context.pending.prompt_id) == proposed["reply"]["id"]
            await invoke(interpret_appointment_reply, context, decision="unrelated")
            return ModelReply(None, 1, 1)
        assert context.requires_fresh_consent
        assert json.loads(model_input)["revision_of"] is None
        return ModelReply("A tire rotation usually takes about 30 minutes here.", 1, 1)

    with monkeypatch.context() as patch:
        patch.setattr(execution, "run_advisor", model)
        _, information = await send(sms_api, "By the way, how long is a tire rotation?")
    assert information["confirmation_attempts"][0]["action_outcome"] == "unrelated"
    assert "30 minutes" in information["reply"]["body"]
    still_pending = (await audit(sms_api, receipt))["pending"]["proposal"]
    assert still_pending == {**active["proposal"], "reference_required": True}
    async with sms_data[0]() as session:
        assert (await session.get_one(Appointment, original.id)).scheduled_start == original.scheduled_start

    _, ambiguous = await send(sms_api, "ye go ahead")
    assert ambiguous["confirmation_attempts"][0]["action_outcome"] == "confirmation_required"
    assert (await audit(sms_api, receipt))["pending"] is not None

    async def explicit(_, context, model_input):
        assert json.loads(model_input)["proposal_reference_required"]
        await invoke(interpret_appointment_reply, context, decision="confirm", explicit_proposal_reference=True)
        return ModelReply(None, 1, 1)

    monkeypatch.setattr(execution, "run_advisor", explicit)
    _, confirmed = await send(sms_api, "Yes, move the oil change to the time in your proposal.")
    assert confirmed["confirmation_attempts"][0]["action_outcome"] == "succeeded"
    assert (await audit(sms_api, receipt))["pending"] is None


async def test_condition_survives_history_and_staff_answer_requires_fresh_confirmation(
    monkeypatch, sms_api, sms_data, staff, scenario,
):
    sessions, *_ = sms_data
    original, _, revised_start, receipt, _ = scenario
    advisor, _ = staff["advisor"]
    settings_for(sms_api).sms_messages_per_minute = 60
    await send(sms_api, "Move my oil change")
    fact = "There is no fee for moving this appointment."

    async def model(_, context, model_input):
        payload = json.loads(model_input)
        if context.pending:
            if payload["current_inbound_sms"] == "Sure, only if there is no extra fee.":
                await invoke(interpret_appointment_reply, context, decision="conditional", condition="No extra fee")
            else:
                await invoke(interpret_appointment_reply, context, decision="confirm")
            return ModelReply(None, 1, 1)
        assert payload["conditional_request"]["condition"] == "No extra fee"
        assert payload["conditional_request"]["operation"]["appointment_id"] == str(original.id)
        if context.staff_fact:
            assert context.evidence["staff_fact"] == fact
            assert datetime.fromisoformat(payload["conditional_request"]["operation"]["scheduled_start"]) == revised_start
            assert (await invoke(propose_reschedule, context, appointment_id=str(original.id),
                                 scheduled_start=revised_start.isoformat()))["status"] == "proposal_prepared"
            return ModelReply("We can move it without a fee.", 1, 1,
                              AdvisorOutput(sms="We can move it without a fee.", condition_evidence="staff_fact"),
                              verified_proposal_information="We can move it without a fee.")
        if payload["current_inbound_sms"] == "Sure, only if there is no extra fee.":
            await invoke(ask_dealership_staff, context, question="Does that move incur a fee?", addresses_condition=True)
            return ModelReply("I'll check with our team about the fee.", 1, 1)
        if payload["current_inbound_sms"] == "Actually make it one hour later, same fee condition.":
            assert (await invoke(propose_reschedule, context, appointment_id=str(original.id),
                                 scheduled_start=revised_start.isoformat()))["status"] == "proposal_prepared"
            return ModelReply("I can check the later time while the fee is still being verified.", 1, 1)
        return ModelReply("Still waiting on that.", 1, 1)

    monkeypatch.setattr(execution, "run_advisor", model)
    _, conditional = await send(sms_api, "Sure, only if there is no extra fee.")
    assert conditional["confirmation_attempts"][0]["action_outcome"] == "conditional"
    await send(sms_api, "Actually make it one hour later, same fee condition.")
    for _ in range(13):
        await send(sms_api, "Thanks")
    await send(sms_api, "yeah")
    assert (await audit(sms_api, receipt))["pending"] is None
    item, = await list_consultations(sms_api, receipt, advisor)
    answer = await sms_api.put(f"{thread(receipt)}/consultations/{item['id']}/answer", headers=advisor,
                               json={"public_fact": fact})
    assert answer.status_code == 200
    assert await execution.process_next(sessions, settings_for(sms_api))
    await drain_delivery(sms_api, sessions, RecordingTransport())
    fresh = (await audit(sms_api, receipt))["pending"]
    assert fresh["proposal"]["id"] != conditional["confirmation_attempts"][0]["proposal_id"]
    async with sessions() as session:
        assert (await session.get_one(Appointment, original.id)).scheduled_start == original.scheduled_start
    _, confirmed = await send(sms_api, "ye go ahead")
    assert confirmed["confirmation_attempts"][0]["action_outcome"] == "succeeded"


async def test_condition_and_retirement_commit_before_dialogue_retry(monkeypatch, sms_api, sms_data, scenario):
    sessions, *_ = sms_data
    original, _, _, receipt, _ = scenario
    await send(sms_api, "Move my oil change")
    turns = 0

    async def model(_, context, model_input):
        nonlocal turns
        if context.pending:
            await invoke(interpret_appointment_reply, context, decision="conditional", condition="Can wait onsite")
            return ModelReply(None, 1, 1)
        turns += 1
        condition = json.loads(model_input)["conditional_request"]
        assert condition["condition"] == "Can wait onsite"
        assert condition["operation"]["appointment_id"] == str(original.id)
        if turns == 1:
            raise TimeoutError
        return ModelReply("I'll need to verify whether you can wait onsite.", 1, 1)

    monkeypatch.setattr(execution, "run_advisor", model)
    inbound, failed = await send(sms_api, "Yes, if I can wait onsite.")
    assert failed["status"] == "failed" and failed["error_code"] == "timeout"
    assert (await audit(sms_api, receipt))["pending"] is None
    recovered = await process(sms_api, inbound)
    assert recovered["status"] == "completed" and recovered["attempts"] == 2
    assert recovered["confirmation_attempts"][-1]["interpretation"] == "recovered"
    async with sessions() as session:
        row = await session.get_one(AgentExecution, UUID(inbound["message_id"]))
        assert [item["error_code"] for item in row.advisor_attempts] == ["timeout", None]
        assert (await session.get_one(Appointment, original.id)).scheduled_start == original.scheduled_start


async def test_handback_recomputes_a_suppressed_reply_when_no_employee_replied(
    monkeypatch, sms_api, sms_data, staff,
):
    sessions = sms_data[0]
    settings = settings_for(sms_api)
    advisor, _ = staff["advisor"]

    async def model(_, context, model_input):
        if context.attempt > 1:
            assert "Stale AI text" not in model_input
        return ModelReply("Stale AI text" if context.attempt == 1 else "Fresh AI text", 1, 1)

    monkeypatch.setattr(execution, "run_advisor", model)
    receipt = await simulate(sms_api, body="I have a service question")
    assert await execution.process_next(sessions, settings)
    assert (await set_owner(sms_api, receipt, advisor, "human", 0)).status_code == 200
    assert (await set_owner(sms_api, receipt, advisor, "ai", 1)).status_code == 200
    assert await execution.process_next(sessions, settings)
    assert (await state(sms_api, receipt))["attempts"] == 2
    transport = RecordingTransport()
    await drain_delivery(sms_api, sessions, transport)
    assert transport.bodies == ["Fresh AI text"]
