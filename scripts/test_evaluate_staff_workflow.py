from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest

from scripts.evaluate_staff_workflow import benchmark_scenario, submit_customer_sms, verified_slot
from servicepilot.config import SIMULATED_RECIPIENT
from servicepilot.conversations import routes
from servicepilot.conversations.schemas import InboundResult
from servicepilot.db.models import Message
from servicepilot.main import create_app
from servicepilot.tenancy.auth import dealership_transaction


@pytest.mark.parametrize(("offered_starts", "expected"), [
    (["2026-11-27T12:00:00-05:00", "2026-11-27T11:00:00-05:00"], True),
    (["2026-11-27T17:00:00+00:00"], True),
    (["2026-11-27T11:00:00-05:00", "2026-11-27T13:00:00-05:00"], False),
    (["2026-11-27T12:00:00-08:00", "2026-11-28T12:00:00-05:00"], False),
])
def test_lunch_lookup_verifies_the_returned_noon_slot(offered_starts, expected):
    noon = datetime(2026, 11, 27, 12, tzinfo=ZoneInfo("America/Detroit"))
    lookup = {"name": "get_available_slots", "outcome": "ok", "has_service": True, "day": "2026-11-27",
              "period": "lunch", "requested_start": None, "offered_starts": offered_starts}
    assert verified_slot([{"tool_details": [lookup]}], noon) is expected


@pytest.mark.parametrize("status_outcome", [None, "consultation_open", "consultation_prepared"])
async def test_loaner_status_checks_persisted_work_regardless_of_tool_use(status_outcome):
    question = {"id": "original", "status": "open", "question": "Is a loaner available Friday?"}
    consultations = [question]
    events = []

    async def turn(text, *, phone):
        if text.startswith("Do you guys"):
            reply, outcome = "I'll check on a loaner.", "consultation_prepared"
        elif text.startswith("Any update"):
            reply, outcome = "I'm still waiting to hear back about the loaner.", status_outcome
            if outcome == "consultation_prepared":
                consultations.append({**question, "id": "duplicate"})
        elif "brake inspection" in text:
            reply, outcome = "A brake inspection takes 45 minutes.", None
        else:
            reply, outcome = "Hey! What's up?", None
        tools = [{"name": "ask_dealership_staff", "outcome": outcome}] if outcome else []
        events.append({"conversation_id": "thread", "customer_turn": True, "attempts": [{"tools": tools}]})
        return {"conversation_id": "thread"}, {"reply": {"body": reply}, "tool_calls": tools}

    def handle(request):
        path = request.url.path
        if path == "/staff/consultations":
            return httpx.Response(200, json=consultations)
        if path == "/staff/conversations/thread/consultations/original/answer":
            consultations[0] = {**question, "status": "resolved"}
            return httpx.Response(200, json=consultations[0])
        if path == "/simulations/sms":
            return httpx.Response(200, json={"outbound": [
                {"source": "advisor_followup", "status": "delivered", "body": "A loaner is available. Bring your license."},
            ]})
        assert path == "/simulations/sms/thread/appointment-actions"
        return httpx.Response(200, json={"pending": None, "results": []})

    drain = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://simulation") as api:
        report = await benchmark_scenario(
            api, SimpleNamespace(agent_model="gpt-6-luna", agent_reasoning_effort="low"), turn, None,
            drain, AsyncMock(), events, {}, {}, None, None, 1, "K_consultation_relevance",
            AsyncMock(return_value={}), "this Friday",
        )

    assert events[-1]["consultations_after_turn"][0] == question
    if status_outcome == "consultation_prepared":
        assert report["error"] == "status inquiry preserves only the original open consultation"
        drain.assert_not_awaited()
    else:
        assert report["passed_operational_checks"]
        assert consultations == [{**question, "status": "resolved"}]
        drain.assert_awaited_once()


async def test_evaluator_anchors_timestamp_after_public_sms_validation(monkeypatch):
    app = create_app()
    app.state.settings = SimpleNamespace(enable_sms_simulation=True)
    receipt = InboundResult(conversation_id=uuid4(), message_id=uuid4(), status="persisted")
    ingest = AsyncMock(return_value=receipt)
    monkeypatch.setattr(routes, "ingest_sms", ingest)

    async def dealership_override():
        return None, uuid4()

    app.dependency_overrides[dealership_transaction] = dealership_override
    arrival = datetime(2026, 11, 24, 5, tzinfo=UTC)
    anchor = datetime(2026, 11, 24, 4, 59, tzinfo=UTC)
    message = SimpleNamespace(occurred_at=arrival, created_at=arrival)
    get_one = AsyncMock(return_value=message)

    @asynccontextmanager
    async def begin():
        yield SimpleNamespace(get_one=get_one)

    sessions = SimpleNamespace(begin=begin)
    headers = {"X-API-Key": "synthetic-test-key"}
    text = "Can I get an oil change this Friday at noon?"
    phone = "+12065550110"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://simulation") as api:
        old_request = await api.post("/simulations/sms", headers=headers, json={
            "sender_phone": phone, "body": text, "external_message_id": uuid4().hex,
            "occurred_at": anchor.isoformat(),
        })
        assert old_request.status_code == 422 and old_request.json() == {"detail": "Invalid SMS content"}
        ingest.assert_not_awaited()

        result = await submit_customer_sms(api, sessions, headers, phone=phone, text=text, occurred_at=anchor)
        assert result == receipt.model_dump(mode="json")
        assert ingest.await_args.args[3].occurred_at is None
        get_one.assert_awaited_once_with(Message, receipt.message_id)
        assert message.occurred_at == anchor and message.created_at == arrival

        await submit_customer_sms(api, sessions, headers, phone=phone, text="Yeah that works.")
        get_one.assert_awaited_once_with(Message, receipt.message_id)

        with pytest.raises(httpx.HTTPStatusError, match="HTTP 422. Sender cannot be the simulation recipient") as error:
            await submit_customer_sms(api, sessions, headers, phone=SIMULATED_RECIPIENT, text=text)
        assert headers["X-API-Key"] not in str(error.value) and text not in str(error.value)
