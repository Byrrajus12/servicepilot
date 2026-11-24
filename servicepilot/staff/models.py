from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from servicepilot.db.base import Base, TenantOwned

EmployeeRole = Literal["manager", "advisor"]


class Employee(TenantOwned, Base):
    __tablename__ = "employees"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "username"),
        CheckConstraint("role IN ('manager', 'advisor')", name="role"),
    )

    username: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[EmployeeRole] = mapped_column(String(16))
    disabled: Mapped[bool] = mapped_column(server_default="false")


class EmployeeSession(TenantOwned, Base):
    __tablename__ = "employee_sessions"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "employee_id"], ["employees.tenant_id", "employees.id"]),
        Index(None, "tenant_id", "employee_id"),
    )

    employee_id: Mapped[UUID]
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StaffNote(TenantOwned, Base):
    __tablename__ = "staff_notes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"], ["conversations.tenant_id", "conversations.id"],
        ),
        ForeignKeyConstraint(["tenant_id", "employee_id"], ["employees.tenant_id", "employees.id"]),
        UniqueConstraint("tenant_id", "conversation_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "reply_to_id"],
            ["staff_notes.tenant_id", "staff_notes.conversation_id", "staff_notes.id"],
        ),
        Index(None, "tenant_id", "conversation_id", "created_at", "id"),
    )

    conversation_id: Mapped[UUID]
    employee_id: Mapped[UUID]
    reply_to_id: Mapped[UUID | None]
    body: Mapped[str] = mapped_column(Text)


class OwnershipEvent(TenantOwned, Base):
    __tablename__ = "ownership_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"], ["conversations.tenant_id", "conversations.id"],
        ),
        ForeignKeyConstraint(["tenant_id", "employee_id"], ["employees.tenant_id", "employees.id"]),
        ForeignKeyConstraint(["tenant_id", "assigned_id"], ["employees.tenant_id", "employees.id"]),
        Index(None, "tenant_id", "conversation_id", "created_at", "id"),
    )

    conversation_id: Mapped[UUID]
    employee_id: Mapped[UUID]
    assigned_id: Mapped[UUID | None]
    previous_owner: Mapped[str] = mapped_column(String(8))
    owner: Mapped[str] = mapped_column(String(8))
    ownership_version: Mapped[int]


class Consultation(TenantOwned, Base):
    __tablename__ = "consultations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "execution_id"],
            ["agent_executions.tenant_id", "agent_executions.conversation_id", "agent_executions.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "inbound_id"],
            ["messages.tenant_id", "messages.conversation_id", "messages.id"],
        ),
        ForeignKeyConstraint(["tenant_id", "answered_by"], ["employees.tenant_id", "employees.id"]),
        ForeignKeyConstraint(["tenant_id", "assigned_id"], ["employees.tenant_id", "employees.id"]),
        UniqueConstraint("tenant_id", "conversation_id", "execution_id", "ownership_version", "question"),
        CheckConstraint("status IN ('open', 'answered', 'canceled', 'resolved')", name="status"),
        Index(None, "tenant_id", "conversation_id", "status"),
    )

    conversation_id: Mapped[UUID]
    execution_id: Mapped[UUID]
    inbound_id: Mapped[UUID]
    assigned_id: Mapped[UUID | None]
    ownership_version: Mapped[int]
    status: Mapped[str] = mapped_column(String(16), server_default="open")
    question: Mapped[str] = mapped_column(String(500))
    public_fact: Mapped[str | None] = mapped_column(String(500))
    answered_by: Mapped[UUID | None]
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
