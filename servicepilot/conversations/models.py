from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from servicepilot.db.base import Base, TenantOwned


class Conversation(TenantOwned, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
        UniqueConstraint("tenant_id", "id"),
        Index(None, "tenant_id", "customer_id"),
    )

    customer_id: Mapped[UUID]


class Message(TenantOwned, Base):
    __tablename__ = "messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"], ["conversations.tenant_id", "conversations.id"]
        ),
        CheckConstraint("direction IN ('inbound', 'outbound')", name="direction"),
        Index(None, "tenant_id", "conversation_id", "created_at", "id"),
    )

    conversation_id: Mapped[UUID]
    direction: Mapped[str] = mapped_column(String(8))
    from_number: Mapped[str] = mapped_column(String(16))
    to_number: Mapped[str] = mapped_column(String(16))
    body: Mapped[str] = mapped_column(Text)
