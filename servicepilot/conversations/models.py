from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import (
    CheckConstraint, DateTime, ForeignKeyConstraint, Index, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from servicepilot.db.base import Base, TenantOwned

ConversationStatus = Literal["open", "closed"]
CustomerResolution = Literal["resolved", "unmatched", "ambiguous"]
ConfirmationState = Literal["awaiting_confirmation", "confirmed"]


class Conversation(TenantOwned, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "assigned_employee_id"], ["employees.tenant_id", "employees.id"]),
        CheckConstraint("owner IN ('ai', 'human')", name="owner"),
        CheckConstraint("owner != 'human' OR assigned_employee_id IS NOT NULL", name="human_assignment"),
        Index(None, "tenant_id", "assigned_employee_id"),
        Index(None, "tenant_id", "customer_id"),
        Index(
            "uq_conversations_open_sms_thread", "tenant_id", "sender_number", "recipient_number",
            unique=True, postgresql_where=text("status = 'open' AND sender_number IS NOT NULL"),
        ),
        CheckConstraint("status IN ('open', 'closed')", name="status"),
        CheckConstraint(
            "(sender_number IS NULL) = (recipient_number IS NULL)", name="sms_addresses",
        ),
        CheckConstraint(
            "(customer_resolution = 'resolved' AND customer_id IS NOT NULL) OR "
            "(customer_resolution IN ('unmatched', 'ambiguous') AND customer_id IS NULL)",
            name="customer_resolution",
        ),
        CheckConstraint(
            "(pending_action_type IS NULL AND pending_action_payload IS NULL AND "
            "pending_action_expires_at IS NULL AND pending_confirmation IS NULL) OR "
            "(pending_action_type IS NOT NULL AND pending_action_payload IS NOT NULL AND "
            "jsonb_typeof(pending_action_payload) = 'object' AND "
            "pending_action_expires_at IS NOT NULL AND pending_confirmation IS NOT NULL AND "
            "pending_confirmation IN ('awaiting_confirmation', 'confirmed'))",
            name="pending_action",
        ),
    )

    customer_id: Mapped[UUID | None]
    owner: Mapped[Literal["ai", "human"]] = mapped_column(String(8), server_default="ai")
    ownership_version: Mapped[int] = mapped_column(server_default="0")
    assigned_employee_id: Mapped[UUID | None]
    sender_number: Mapped[str | None] = mapped_column(String(16))
    recipient_number: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[ConversationStatus] = mapped_column(String(16), server_default="open")
    customer_resolution: Mapped[CustomerResolution] = mapped_column(
        String(16), server_default="resolved",
    )
    pending_action_type: Mapped[str | None] = mapped_column(String(100))
    pending_action_payload: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    pending_action_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_confirmation: Mapped[ConfirmationState | None] = mapped_column(String(32))
    conditional_request: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))


class Message(TenantOwned, Base):
    __tablename__ = "messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"], ["conversations.tenant_id", "conversations.id"]
        ),
        UniqueConstraint("tenant_id", "conversation_id", "id"),
        ForeignKeyConstraint(["tenant_id", "employee_id"], ["employees.tenant_id", "employees.id"]),
        UniqueConstraint("tenant_id", "conversation_id", "employee_request_id"),
        CheckConstraint(
            "(employee_id IS NULL AND employee_request_id IS NULL) OR "
            "(direction = 'outbound' AND employee_id IS NOT NULL AND employee_request_id IS NOT NULL)",
            name="employee_reply",
        ),
        CheckConstraint("direction IN ('inbound', 'outbound')", name="direction"),
        Index(None, "tenant_id", "conversation_id", "occurred_at", "created_at", "id"),
        Index(
            "uq_messages_inbound_provider_id", "tenant_id", "provider", "external_message_id",
            unique=True, postgresql_where=text("direction = 'inbound' AND external_message_id IS NOT NULL"),
        ),
    )

    conversation_id: Mapped[UUID]
    employee_id: Mapped[UUID | None]
    employee_request_id: Mapped[UUID | None]
    direction: Mapped[str] = mapped_column(String(8))
    from_number: Mapped[str] = mapped_column(String(16))
    to_number: Mapped[str] = mapped_column(String(16))
    body: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(100), server_default="legacy")
    external_message_id: Mapped[str | None] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(),
    )
