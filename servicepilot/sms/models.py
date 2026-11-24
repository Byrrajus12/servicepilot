from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from servicepilot.db.base import Base, TenantOwned

DeliveryStatus = Literal[
    "pending", "sending", "uncertain", "accepted", "sent", "delivered", "failed", "undelivered",
]


class SMSDelivery(TenantOwned, Base):
    __tablename__ = "sms_deliveries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "id"],
            ["messages.tenant_id", "messages.conversation_id", "messages.id"], ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "confirmation_for_id"],
            ["messages.tenant_id", "messages.conversation_id", "messages.id"],
        ),
        UniqueConstraint("provider", "account_sid", "provider_message_id"),
        CheckConstraint("provider IN ('simulation', 'twilio')", name="provider"),
        CheckConstraint(
            "(provider = 'twilio') = (account_sid IS NOT NULL)", name="account",
        ),
        CheckConstraint(
            "status IN ('pending', 'sending', 'uncertain', 'accepted', 'sent', 'delivered', 'failed', 'undelivered')",
            name="status",
        ),
        CheckConstraint("(status = 'sending') = (lease_expires_at IS NOT NULL)", name="lease"),
        CheckConstraint("(status = 'pending') = (next_attempt_at IS NOT NULL)", name="retry"),
        CheckConstraint("(status = 'delivered') = (delivered_at IS NOT NULL)", name="delivered_at"),
        CheckConstraint("(confirmation_for_id IS NULL) = (expires_at IS NULL)", name="confirmation"),
        Index("ix_sms_deliveries_work", "status", "next_attempt_at", "lease_expires_at"),
        Index(
            "ix_sms_deliveries_unfinished_thread", "tenant_id", "conversation_id", "created_at", "id",
            postgresql_where=text("status IN ('pending', 'sending')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    conversation_id: Mapped[UUID]
    provider: Mapped[str] = mapped_column(String(16))
    account_sid: Mapped[str | None] = mapped_column(String(34))
    sender: Mapped[str] = mapped_column(String(16))
    recipient: Mapped[str] = mapped_column(String(16))
    status: Mapped[DeliveryStatus] = mapped_column(String(16), server_default="pending")
    attempts: Mapped[int] = mapped_column(server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmation_for_id: Mapped[UUID | None]
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(40))


class SMSConsent(TenantOwned, Base):
    __tablename__ = "sms_consents"
    __table_args__ = (UniqueConstraint("tenant_id", "channel", "sender", "recipient"),)

    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(64))
    sender: Mapped[str] = mapped_column(String(16))
    recipient: Mapped[str] = mapped_column(String(16))
    suppressed: Mapped[bool]
    last_opt_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
