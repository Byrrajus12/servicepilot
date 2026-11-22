from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_ignore_empty=True, hide_input_in_errors=True,
    )

    database_url: str
    dealership_api_keys: dict[str, SecretStr] = Field(default_factory=dict)
    enable_sms_simulation: bool = False

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
