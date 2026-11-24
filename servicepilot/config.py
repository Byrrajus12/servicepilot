from re import fullmatch
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

SIMULATED_RECIPIENT = "+12065550100"
DEFAULT_ADVISOR_MODEL = "gpt-6-luna"
DEFAULT_ADVISOR_REASONING_EFFORT = "low"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_ignore_empty=True, hide_input_in_errors=True,
    )

    database_url: str
    dealership_api_keys: dict[str, SecretStr] = Field(default_factory=dict)
    enable_sms_simulation: bool = False
    openai_api_key: SecretStr | None = None
    agent_model: str = Field(default=DEFAULT_ADVISOR_MODEL, min_length=1, max_length=120)
    agent_reasoning_effort: Literal["none", "low", "medium", "high"] | None = None
    agent_max_output_tokens: int | None = Field(default=None, ge=256, le=8192)
    agent_timeout_seconds: float = Field(default=45, ge=1, le=120)
    agent_max_turns: int = Field(default=7, ge=1, le=10)
    agent_max_attempts: int = Field(default=3, ge=1, le=10)
    worker_poll_seconds: float = Field(default=1, ge=0.1, le=30)
    worker_concurrency: int = Field(default=4, ge=1, le=16)
    worker_retry_base_seconds: float = Field(default=2, ge=0.1, le=60)
    sms_delivery_max_attempts: int = Field(default=5, ge=1, le=10)
    sms_messages_per_minute: int = Field(default=12, ge=1, le=120)
    sms_transport: Literal["simulation", "twilio"] = "simulation"
    twilio_account_sid: str | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_number_routes: dict[str, str] = Field(default_factory=dict)
    sms_public_webhook_base_url: str | None = None

    @model_validator(mode="after")
    def validate_twilio(self) -> "Settings":
        if self.sms_transport != "twilio":
            return self
        if not fullmatch(r"AC[0-9a-fA-F]{32}", self.twilio_account_sid or "") or not (
            self.twilio_auth_token and self.twilio_auth_token.get_secret_value()
        ):
            raise ValueError("Twilio requires an account SID and auth token")
        if not self.twilio_number_routes or any(
            number == SIMULATED_RECIPIENT or not fullmatch(r"\+[1-9][0-9]{1,14}", number) or not slug.strip()
            for number, slug in self.twilio_number_routes.items()
        ):
            raise ValueError("Twilio requires a mapping from real E.164 numbers to dealership slugs")
        base = self.sms_public_webhook_base_url or ""
        url = urlsplit(base)
        if (
            url.scheme != "https" or not url.hostname or url.username or url.password
            or any(char.isspace() for char in base) or "?" in base or "#" in base
        ):
            raise ValueError("Twilio requires an HTTPS public webhook base URL")
        self.sms_public_webhook_base_url = base.rstrip("/")
        self.twilio_account_sid = "AC" + self.twilio_account_sid[2:].lower()
        return self

    @field_validator("dealership_api_keys")
    @classmethod
    def validate_api_keys(cls, keys: dict[str, SecretStr]) -> dict[str, SecretStr]:
        values = [key.get_secret_value() for key in keys.values()]
        if any(not key or any(not 33 <= ord(char) <= 126 for char in key) for key in values):
            raise ValueError("Dealership API keys must contain visible ASCII characters only")
        if len(set(values)) != len(values):
            raise ValueError("Dealership API keys must be unique")
        return keys

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        try:
            url = make_url(value)
        except ArgumentError:
            raise ValueError("DATABASE_URL must be a PostgreSQL URL") from None
        if url.drivername != "postgresql+asyncpg" or not url.database:
            raise ValueError("DATABASE_URL must use postgresql+asyncpg and name a database")
        return value
