from base64 import b64encode
from hmac import compare_digest, digest
from logging import getLogger
from urllib.parse import parse_qsl
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy import func, select

from servicepilot.conversations.schemas import InboundSMS
from servicepilot.sms.delivery import apply_receipt
from servicepilot.sms.ingest import ingest_sms
from servicepilot.sms.models import SMSDelivery
from servicepilot.sms.policy import twilio_channel
from servicepilot.sms.request import read_sms_body
from servicepilot.sms.transport import INBOUND_PATH, STATUS_PATH, TWILIO_STATUSES, SendReceipt, valid_message_sid
from servicepilot.tenancy.models import Tenant

router = APIRouter()
logger = getLogger(__name__)


def form_signature(url: str, params: dict[str, str], token: str) -> str:
    # Twilio's documented signature algorithm for uniquely named URL-encoded form fields.
    signed = url + "".join(name + params[name] for name in sorted(params))
    return b64encode(digest(token.encode(), signed.encode(), "sha1")).decode("ascii")


async def authenticated_form(request: Request, path: str) -> dict[str, str]:
    settings = request.app.state.settings
    if settings.sms_transport != "twilio":
        raise HTTPException(404, "Twilio SMS is disabled")
    if request.scope["query_string"] or request.scope.get("raw_path", b"") != path.encode("ascii"):
        raise HTTPException(400, "Unexpected Twilio webhook URL")
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
        raise HTTPException(415, "Expected a URL-encoded form")
    signatures = request.headers.getlist("x-twilio-signature")
    if len(signatures) != 1 or not signatures[0].isascii():
        raise HTTPException(403, "Invalid Twilio signature")
    try:
        pairs = parse_qsl(
            (await read_sms_body(request)).decode("utf-8"), keep_blank_values=True, strict_parsing=True,
            errors="strict", max_num_fields=100,
        )
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid Twilio form") from None
    form = dict(pairs)
    if len(form) != len(pairs):
        raise HTTPException(400, "Duplicate Twilio form fields")
    expected = form_signature(
        settings.sms_public_webhook_base_url + path, form, settings.twilio_auth_token.get_secret_value(),
    )
    if not compare_digest(expected, signatures[0]) or form.get("AccountSid") != settings.twilio_account_sid:
        raise HTTPException(403, "Invalid Twilio signature or account")
    return form


def twiml_ack() -> Response:
    return Response("<Response/>", media_type="application/xml")


@router.post(INBOUND_PATH)
async def twilio_sms(request: Request) -> Response:
    form = await authenticated_form(request, INBOUND_PATH)
    settings = request.app.state.settings
    recipient = form.get("To")
    sender = form.get("From")
    sid = form.get("MessageSid", "")
    slug = settings.twilio_number_routes.get(recipient)
    if slug is None:
        raise HTTPException(404, "SMS route not found")
    if not valid_message_sid(sid):
        raise HTTPException(422, "Invalid Twilio message SID")
    if form.get("NumMedia", "0") != "0" or sid.startswith("MM") or sender in settings.twilio_number_routes:
        return twiml_ack()
    command = form.get("OptOutType")
    if command not in (None, "START", "STOP", "HELP"):
        raise HTTPException(422, "Invalid Twilio consent command")
    try:
        sms = InboundSMS(
            sender_phone=sender, recipient_phone=recipient, external_message_id=sid.lower(),
            body=form.get("Body"), provider=twilio_channel(settings.twilio_account_sid),
        )
    except ValidationError:
        raise HTTPException(422, "Invalid Twilio SMS") from None
    async with request.app.state.session_factory.begin() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == slug))
        if tenant_id is None:
            raise HTTPException(404, "SMS route not found")
        await ingest_sms(session, settings, tenant_id, sms, provider_command=command)
    return twiml_ack()


@router.post(STATUS_PATH + "/{delivery_id}", status_code=204)
async def twilio_status(delivery_id: UUID, request: Request) -> Response:
    form = await authenticated_form(request, f"{STATUS_PATH}/{delivery_id}")
    sid = form.get("MessageSid", "")
    if not valid_message_sid(sid):
        raise HTTPException(422, "Invalid Twilio message SID")
    status = TWILIO_STATUSES.get(form.get("MessageStatus"))
    if status is None:
        return Response(status_code=204)
    error_code = form.get("ErrorCode", "")
    error = None
    if status in {"failed", "undelivered"}:
        error = f"twilio_{error_code}" if error_code.isascii() and error_code.isdigit() and len(error_code) <= 8 else f"twilio_{status}"
    async with request.app.state.session_factory.begin() as session:
        row = await session.get(SMSDelivery, delivery_id, with_for_update=True)
        if row is None or row.provider != "twilio" or row.account_sid != form["AccountSid"]:
            raise HTTPException(404, "Delivery not found")
        if form.get("From", row.sender) != row.sender or form.get("To", row.recipient) != row.recipient:
            raise HTTPException(409, "Delivery addresses do not match")
        if row.provider_message_id not in (None, sid):
            raise HTTPException(409, "Delivery SID does not match")
        now = await session.scalar(select(func.clock_timestamp()))
        if apply_receipt(row, SendReceipt(sid, status, error), now):
            logger.info("Twilio callback for SMS delivery %s status %s code %s", row.id, row.status, row.error_code)
    return Response(status_code=204)
