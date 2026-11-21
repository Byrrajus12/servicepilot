from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from servicepilot.db.base import Base, TenantOwned


class Workflow(TenantOwned, Base):
    __tablename__ = "workflows"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"], ["conversations.tenant_id", "conversations.id"]
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')", name="status"
        ),
        Index(None, "tenant_id", "conversation_id"),
    )

    conversation_id: Mapped[UUID | None]
    workflow_type: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(32), server_default="pending")
