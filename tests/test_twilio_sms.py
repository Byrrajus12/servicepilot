import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import func, select

from servicepilot.agent import execution
from servicepilot.agent.advisor import ModelReply
from servicepilot.agent.models import AgentExecution
from servicepilot.appointments.models import Appointment
from servicepilot.config import Settings
from servicepilot.conversations.models import Message
from servicepilot.sms.delivery import deliver_next
from servicepilot.sms.models import SMSDelivery
from servicepilot.sms.routes import form_signature
from servicepilot.sms.transport import INBOUND_PATH, STATUS_PATH, OutboundSMS, SendError, TwilioTransport
from test_appointment_actions import scenario
from test_conversations import SHARED_PHONE, simulation_app, sms_api, sms_data

ACCOUNT = "AC" + "a" * 32
TOKEN = "local-test-token"
BASE = "https://sms.example.test/proxy"
LOCAL_LINE = "+12065550201"
SECOND_LOCAL_LINE = "+12065550202"
FOREIGN_LINE = "+12065550203"


def twilio_settings(database_settings, sms_data):
    _, local, foreign, *_ = sms_data
    return Settings(
        database_url=database_settings.database_url, sms_transport="twilio", sms_public_webhook_base_url=BASE,
        twilio_account_sid=ACCOUNT, twilio_auth_token=TOKEN,
        twilio_number_routes={LOCAL_LINE: local.slug, SECOND_LOCAL_LINE: local.slug, FOREIGN_LINE: foreign.slug},
    )


@pytest.fixture
async def twilio_api(database_settings, sms_data):
    app = simulation_app(database_settings, sms_data, enabled=False)
    settings = twilio_settings(database_settings, sms_data)
    app.state.settings = settings
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://internal") as client:
        yield client, settings


def message(number=LOCAL_LINE, **fields):
    return {
        "AccountSid": ACCOUNT, "To": number, "From": SHARED_PHONE,
        "MessageSid": "SM" + "b" * 32, "Body": " Need an oil change. ", "NumMedia": "0",
    } | fields


async def signed_post(api, path, form, *, signature_url=None, extra_headers=None):
    signature = form_signature(signature_url or BASE + path, form, TOKEN)
    return await api.post(path, data=form, headers={"X-Twilio-Signature": signature} | (extra_headers or {}))


def test_published_twilio_signature_example():
    assert form_signature("https://example.com/myapp.php?foo=1&bar=2", {
        "CallSid": "CA1234567890ABCDE", "Caller": "+14158675310", "Digits": "1234",
        "From": "+14158675310", "To": "+18005551212",
    }, "12345") == "L/OH5YylLD5NRKLltdqwSvS0BnU="


async def test_signature_url_dedup_and_tenant_isolation(twilio_api, sms_data):
    api, _ = twilio_api
    sessions, local, foreign, *_ = sms_data
    assert (await signed_post(api, INBOUND_PATH, message(), signature_url="https://evil.example" + INBOUND_PATH,
                              extra_headers={"X-Forwarded-Host": "evil.example"})).status_code == 403
    assert (await api.post(INBOUND_PATH, data=message())).status_code == 403
    assert (await signed_post(api, INBOUND_PATH, message("+12065550299"))).status_code == 404
    assert (await signed_post(api, INBOUND_PATH, message(AccountSid="AC" + "f" * 32))).status_code == 403
    assert (await signed_post(api, INBOUND_PATH, message(NumMedia="1"))).status_code == 200
    assert (await signed_post(api, INBOUND_PATH, message(From=FOREIGN_LINE))).status_code == 200
    response = await signed_post(api, INBOUND_PATH, message(TenantId=str(foreign.id)))
    assert response.status_code == 200 and response.text == "<Response/>"
    assert (await signed_post(api, INBOUND_PATH, message(SECOND_LOCAL_LINE))).status_code == 200
    assert (await signed_post(api, INBOUND_PATH, message(FOREIGN_LINE))).status_code == 200
    async with sessions() as session:
        inbound = (await session.scalars(select(Message).where(Message.tenant_id.in_((local.id, foreign.id))))).all()
        assert {(row.tenant_id, row.to_number) for row in inbound} == {
            (local.id, LOCAL_LINE), (foreign.id, FOREIGN_LINE),
        }
        assert all(row.body == " Need an oil change. " for row in inbound)
        assert await session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.tenant_id.in_((local.id, foreign.id))
        )) == 2


async def test_twilio_opt_out_and_opt_in(twilio_api, sms_data):
    api, _ = twilio_api
    sessions, local, *_ = sms_data
    for index, (body, command) in enumerate([
        ("PAUSE", "STOP"), ("My appointment", None), ("RESUME", "START"), ("My appointment", None),
    ], start=1):
        form = message(Body=body, MessageSid="SM" + f"{index:032x}")
        if command:
            form["OptOutType"] = command
        assert (await signed_post(api, INBOUND_PATH, form)).status_code == 200
    async with sessions() as session:
        runs = (await session.scalars(select(AgentExecution).where(AgentExecution.tenant_id == local.id))).all()
        assert len(runs) == 1
        assert (await session.get_one(Message, runs[0].id)).external_message_id.endswith("0004")


@pytest.fixture
async def queued_twilio_delivery(monkeypatch, twilio_api, sms_data):
    api, settings = twilio_api
    sessions, local, *_ = sms_data

    async def model(*_):
        return ModelReply("Yes, we can help.", 1, 1)

    monkeypatch.setattr(execution, "run_advisor", model)
    assert (await signed_post(api, INBOUND_PATH, message())).status_code == 200
    assert await execution.process_next(sessions, settings)
    async with sessions() as session:
        delivery = await session.scalar(select(SMSDelivery).where(SMSDelivery.tenant_id == local.id))
        assert delivery is not None and delivery.status == "pending"
    return sessions, settings, delivery.id


@pytest.mark.parametrize(("code", "kind"), [(429, "retryable"), (400, "permanent"), (503, "uncertain")])
async def test_twilio_http_failure_classification(queued_twilio_delivery, code, kind):
    _, settings, delivery_id = queued_twilio_delivery

    def send(request):
        return httpx.Response(code, json={"message": "private reply details"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        transport = TwilioTransport(settings, client)
        with pytest.raises(SendError) as error:
            await transport.send(OutboundSMS(delivery_id, LOCAL_LINE, SHARED_PHONE, "Hello", 300))
    assert error.value.kind == kind and "private" not in str(error.value)


def callback_form(provider_id, status, **fields):
    return {
        "AccountSid": ACCOUNT, "From": LOCAL_LINE, "To": SHARED_PHONE,
        "MessageSid": provider_id, "MessageStatus": status,
    } | fields


async def test_send_status_callbacks_are_authenticated_and_monotonic(queued_twilio_delivery, twilio_api):
    sessions, settings, delivery_id = queued_twilio_delivery
    api, _ = twilio_api
    path = f"{STATUS_PATH}/{delivery_id}"
    sid = "SM" + "9" * 32
    requests = []

    def send(request):
        requests.append(request)
        form = parse_qs(request.content.decode())
        assert request.url.path.endswith(f"/Accounts/{ACCOUNT}/Messages.json")
        assert request.headers["Authorization"].startswith("Basic ")
        assert form["StatusCallback"] == [BASE + path]
        assert form["From"] == [LOCAL_LINE] and form["To"] == [SHARED_PHONE]
        return httpx.Response(201, json={"sid": sid, "status": "queued"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        transports = {"twilio": TwilioTransport(settings, client)}
        assert await deliver_next(sessions, settings, transports)
        assert not await deliver_next(sessions, settings, transports)

    async with sessions() as session:
        assert (await session.get_one(SMSDelivery, delivery_id)).status == "accepted"

    assert (await signed_post(api, path, callback_form(sid, "delivered"), signature_url=BASE + "/wrong")).status_code == 403
    assert (await signed_post(api, path, callback_form(sid, "delivered", From=FOREIGN_LINE))).status_code == 409
    assert (await signed_post(api, path, callback_form("SM" + "1" * 32, "delivered"))).status_code == 409
    for provider_status in ("sent", "delivered", "delivered", "queued", "failed"):
        assert (await signed_post(api, path, callback_form(sid, provider_status))).status_code == 204
    async with sessions() as session:
        row = await session.get_one(SMSDelivery, delivery_id)
        assert row.status == "delivered" and row.provider_message_id == sid and row.delivered_at is not None
    assert len(requests) == 1
    assert (await signed_post(api, INBOUND_PATH, message(MessageSid="SM" + "7" * 32))).status_code == 200
    assert not await execution.process_next(sessions, settings)


async def test_crash_after_acceptance_waits_for_callback_without_resending(queued_twilio_delivery, twilio_api):
    sessions, settings, delivery_id = queued_twilio_delivery
    api, _ = twilio_api
    sid = "SM" + "c" * 32
    calls = 0

    class CrashedAfterAccept:
        async def send(self, sms):
            nonlocal calls
            calls += 1
            assert sms.id == delivery_id
            # The create reached Twilio and obtained a SID, then the process exited before saving it.
            raise asyncio.CancelledError

    transports = {"twilio": CrashedAfterAccept()}
    with pytest.raises(asyncio.CancelledError):
        await deliver_next(sessions, settings, transports)
    async with sessions.begin() as session:
        row = await session.get_one(SMSDelivery, delivery_id)
        assert row.status == "sending" and row.provider_message_id is None
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await deliver_next(sessions, settings, transports)
    assert not await deliver_next(sessions, settings, transports)
    async with sessions() as session:
        assert (await session.get_one(SMSDelivery, delivery_id)).status == "uncertain"

    assert (await signed_post(api, f"{STATUS_PATH}/{delivery_id}", callback_form(
        sid, "undelivered", ErrorCode="30003",
    ))).status_code == 204
    assert (await signed_post(api, f"{STATUS_PATH}/{delivery_id}", callback_form(sid, "sent"))).status_code == 204
    async with sessions() as session:
        row = await session.get_one(SMSDelivery, delivery_id)
        assert row.status == "undelivered" and row.error_code == "twilio_30003"
    assert calls == 1


@pytest.mark.parametrize("receipt_before_confirmation", [True, False])
async def test_twilio_appointment_requires_delivery_before_confirmation(
    twilio_api, sms_data, scenario, receipt_before_confirmation,
):
    api, settings = twilio_api
    sessions = sms_data[0]
    original, *_ = scenario
    assert (await signed_post(api, INBOUND_PATH, message(Body="Cancel my oil change"))).status_code == 200
    assert await execution.process_next(sessions, settings)
    async with sessions() as session:
        prompt_id = await session.scalar(select(SMSDelivery.id).where(SMSDelivery.provider == "twilio"))
    sid = "SM" + "d" * 32
    path = f"{STATUS_PATH}/{prompt_id}"
    attempt_count = 0

    async def send(request):
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            return httpx.Response(429)
        return httpx.Response(201, json={"sid": sid, "status": "queued"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        transports = {"twilio": TwilioTransport(settings, client)}
        assert await deliver_next(sessions, settings, transports)
        assert not await deliver_next(sessions, settings, transports)
        async with sessions.begin() as session:
            row = await session.get_one(SMSDelivery, prompt_id)
            assert row.status == "pending" and row.error_code == "twilio_http_429"
            row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        assert await deliver_next(sessions, settings, transports)

    if receipt_before_confirmation:
        assert (await signed_post(api, path, callback_form(sid, "delivered"))).status_code == 204
    decision = message(Body="Yes, please.", MessageSid="SM" + "e" * 32)
    assert (await signed_post(api, INBOUND_PATH, decision)).status_code == 200
    if not receipt_before_confirmation:
        assert (await signed_post(api, path, callback_form(sid, "delivered"))).status_code == 204
    assert await execution.process_next(sessions, settings)
    async with sessions() as session:
        updated = await session.get_one(Appointment, original.id)
        assert updated.status == ("cancelled" if receipt_before_confirmation else "scheduled")
