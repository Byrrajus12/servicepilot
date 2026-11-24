from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import CheckConstraint, Computed, DateTime, ForeignKeyConstraint, Index, String, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from servicepilot.db.base import Base, TenantOwned

ExecutionStatus = Literal["pending", "running", "completed", "failed"]


class AgentExecution(TenantOwned, Base):
    __tablename__ = "agent_executions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "source_message_id"],
            ["messages.tenant_id", "messages.conversation_id", "messages.id"],
        ),
        UniqueConstraint("tenant_id", "conversation_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "reply_id"],
            ["messages.tenant_id", "messages.conversation_id", "messages.id"],
        ),
        Index(
            "ix_agent_executions_unfinished_conversation", "tenant_id", "conversation_id",
            postgresql_where=text("status IN ('pending', 'running') OR next_attempt_at IS NOT NULL"),
        ),
        Index(
            "uq_agent_executions_running_conversation", "tenant_id", "conversation_id",
            unique=True, postgresql_where=text("status = 'running'"),
        ),
        CheckConstraint("status IN ('pending', 'running', 'completed', 'failed')", name="status"),
        CheckConstraint("(status = 'running') = (lease_expires_at IS NOT NULL)", name="lease"),
        CheckConstraint("(status = 'completed') = (reply_id IS NOT NULL)", name="reply"),
        CheckConstraint("next_attempt_at IS NULL OR status = 'failed'", name="retry"),
    )

    # Direct work uses the inbound ID. Resumption uses a consultation ID and the original SMS for routing.
    id: Mapped[UUID] = mapped_column(primary_key=True)
    conversation_id: Mapped[UUID]
    resume_source_id: Mapped[UUID | None]
    source_message_id: Mapped[UUID] = mapped_column(Uuid, Computed("coalesce(resume_source_id, id)"))
    status: Mapped[ExecutionStatus] = mapped_column(String(16), server_default="pending")
    attempts: Mapped[int] = mapped_column(server_default="0")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reply_id: Mapped[UUID | None]
    model: Mapped[str | None] = mapped_column(String(120))
    duration_ms: Mapped[int | None]
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    tool_calls: Mapped[list[dict[str, str]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    confirmation_attempts: Mapped[list[dict]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    error_code: Mapped[str | None] = mapped_column(String(40))
