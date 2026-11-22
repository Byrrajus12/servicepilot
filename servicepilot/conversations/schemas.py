from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints, field_validator

from servicepilot.appointments.schemas import PhoneNumber


class SMSContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sender_phone: PhoneNumber
    body: Annotated[str, StringConstraints(min_length=1, max_length=1600)]
    external_message_id: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255, pattern=r"^[^\x00]+$"),
    ] | None = None

    @field_validator("body")
    @classmethod
    def validate_body(cls, body: str) -> str:
        if not body.strip() or "\x00" in body:
            raise ValueError("SMS body must contain text and cannot contain NUL")
        return body


class InboundSMS(SMSContent):
    recipient_phone: PhoneNumber
    provider: Annotated[
        str, StringConstraints(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]*$"),
    ]
    occurred_at: AwareDatetime | None = None


class InboundResult(BaseModel):
    conversation_id: UUID
    message_id: UUID
    status: Literal["persisted", "duplicate"]
