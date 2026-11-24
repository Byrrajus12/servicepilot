from dataclasses import dataclass
from re import fullmatch
from typing import Literal, Protocol
from uuid import UUID

import httpx

from servicepilot.config import Settings

INBOUND_PATH = "/webhooks/twilio/sms"
STATUS_PATH = "/webhooks/twilio/status"
SEND_TIMEOUT_SECONDS = 10
TWILIO_STATUSES = {
    "accepted": "accepted", "scheduled": "accepted", "queued": "accepted", "sending": "accepted",
    "sent": "sent", "delivered": "delivered", "failed": "failed", "undelivered": "undelivered",
    "canceled": "failed",
}


def valid_message_sid(sid: str) -> bool:
    return fullmatch(r"(?:SM|MM)[0-9a-fA-F]{32}", sid) is not None


@dataclass(frozen=True)
class OutboundSMS:
    id: UUID
    sender: str
    recipient: str
    body: str
    validity_seconds: int


@dataclass(frozen=True)
class SendReceipt:
    provider_id: str
    status: str
    error_code: str | None = None


class SendError(Exception):
    def __init__(self, code: str, kind: Literal["retryable", "permanent", "uncertain"]) -> None:
        super().__init__(code)
        self.code = code
        self.kind = kind


class SMSTransport(Protocol):
    async def send(self, sms: OutboundSMS) -> SendReceipt: ...


class SimulationTransport:
    async def send(self, sms: OutboundSMS) -> SendReceipt:
        return SendReceipt(f"sim-{sms.id}", "delivered")


class TwilioTransport:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.client = client
        self.account = settings.twilio_account_sid
        self.token = settings.twilio_auth_token.get_secret_value()
        self.callback_base = settings.sms_public_webhook_base_url

    async def send(self, sms: OutboundSMS) -> SendReceipt:
        try:
            response = await self.client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{self.account}/Messages.json",
                auth=(self.account, self.token),
                data={
                    "From": sms.sender, "To": sms.recipient, "Body": sms.body,
                    "StatusCallback": f"{self.callback_base}{STATUS_PATH}/{sms.id}",
                    "ValidityPeriod": str(sms.validity_seconds),
                },
                timeout=SEND_TIMEOUT_SECONDS,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise SendError("twilio_connection", "retryable") from None
        except httpx.RequestError:
            raise SendError("twilio_response_unknown", "uncertain") from None

        status = response.status_code
        if status == 429:
            raise SendError("twilio_http_429", "retryable")
        if 400 <= status < 500 and status != 408:
            try:
                code = response.json()["code"]
            except (KeyError, TypeError, ValueError):
                code = None
            safe_code = f"twilio_{code}" if type(code) is int and 10000 <= code <= 99999 else f"twilio_http_{status}"
            raise SendError(safe_code, "permanent")
        if not response.is_success:
            raise SendError(f"twilio_http_{status}", "uncertain")
        try:
            payload = response.json()
            sid = payload["sid"]
            if not valid_message_sid(sid):
                raise ValueError
            provider_status = TWILIO_STATUSES[payload["status"]]
        except (KeyError, ValueError, TypeError):
            raise SendError("twilio_receipt_invalid", "uncertain") from None
        error = f"twilio_{provider_status}" if provider_status in {"failed", "undelivered"} else None
        return SendReceipt(sid, provider_status, error)
