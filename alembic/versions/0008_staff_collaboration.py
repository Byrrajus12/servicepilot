"""Add employee access, ownership fencing, consultations and service knowledge."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def record_columns() -> list:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def thread_constraints() -> list:
    return [sa.ForeignKeyConstraint(
        ["tenant_id", "conversation_id"], ["conversations.tenant_id", "conversations.id"],
    )]


def employee_reference(column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["tenant_id", column], ["employees.tenant_id", "employees.id"])


def upgrade() -> None:
    op.create_table(
        "employees", *record_columns(),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("password_hash", sa.String(200), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("disabled", sa.Boolean(), server_default="false", nullable=False),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "username"),
        sa.CheckConstraint("role IN ('manager', 'advisor')", name="role"),
    )
    op.create_table(
        "employee_sessions", *record_columns(),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        employee_reference("employee_id"),
    )
    op.create_index(None, "employee_sessions", ["tenant_id", "employee_id"])

    op.add_column("tenants", sa.Column("closed_dates", ARRAY(sa.Date()), server_default="{}", nullable=False))
    op.add_column("tenants", sa.Column("service_catalog", JSONB(), server_default="{}", nullable=False))
    op.add_column("tenants", sa.Column("service_policies", JSONB(), server_default="{}", nullable=False))
    op.add_column("conversations", sa.Column("owner", sa.String(8), server_default="ai", nullable=False))
    op.add_column("conversations", sa.Column("ownership_version", sa.Integer(), server_default="0", nullable=False))
    op.add_column("conversations", sa.Column("assigned_employee_id", sa.Uuid()))
    op.create_foreign_key(None, "conversations", "employees", ["tenant_id", "assigned_employee_id"], ["tenant_id", "id"])
    op.create_check_constraint("owner", "conversations", "owner IN ('ai', 'human')")
    op.create_check_constraint(
        "human_assignment", "conversations", "owner != 'human' OR assigned_employee_id IS NOT NULL",
    )
    op.create_index(None, "conversations", ["tenant_id", "assigned_employee_id"])

    op.add_column("messages", sa.Column("employee_id", sa.Uuid()))
    op.add_column("messages", sa.Column("employee_request_id", sa.Uuid()))
    op.create_foreign_key(None, "messages", "employees", ["tenant_id", "employee_id"], ["tenant_id", "id"])
    op.create_unique_constraint(None, "messages", ["tenant_id", "conversation_id", "employee_request_id"])
    op.create_check_constraint(
        "employee_reply", "messages",
        "(employee_id IS NULL AND employee_request_id IS NULL) OR "
        "(direction = 'outbound' AND employee_id IS NOT NULL AND employee_request_id IS NOT NULL)",
    )
    op.add_column("sms_deliveries", sa.Column("ai_ownership_version", sa.Integer(), server_default="0"))
    op.add_column("sms_deliveries", sa.Column("submission_started", sa.Boolean(), server_default="false", nullable=False))
    op.execute("UPDATE sms_deliveries SET submission_started = true WHERE status IN ('sending', 'uncertain')")
    op.add_column("sms_deliveries", sa.Column("inbound_guard_id", sa.Uuid()))
    op.create_foreign_key(
        None, "sms_deliveries", "messages",
        ["tenant_id", "conversation_id", "inbound_guard_id"], ["tenant_id", "conversation_id", "id"],
    )

    op.add_column("agent_executions", sa.Column("resume_source_id", sa.Uuid()))
    op.add_column("agent_executions", sa.Column(
        "source_message_id", sa.Uuid(), sa.Computed("coalesce(resume_source_id, id)"), nullable=False,
    ))
    op.create_foreign_key(
        None, "agent_executions", "messages",
        ["tenant_id", "conversation_id", "source_message_id"], ["tenant_id", "conversation_id", "id"],
    )
    op.drop_constraint("fk_agent_executions_tenant_id_conversation_id_id", "agent_executions", type_="foreignkey")
    op.create_unique_constraint(None, "agent_executions", ["tenant_id", "conversation_id", "id"])

    op.create_table(
        "staff_notes", *record_columns(),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("reply_to_id", sa.Uuid()),
        sa.UniqueConstraint("tenant_id", "conversation_id", "id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "reply_to_id"],
            ["staff_notes.tenant_id", "staff_notes.conversation_id", "staff_notes.id"],
        ),
        *thread_constraints(), employee_reference("employee_id"),
    )
    op.create_index(None, "staff_notes", ["tenant_id", "conversation_id", "created_at", "id"])
    op.create_table(
        "ownership_events", *record_columns(),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("assigned_id", sa.Uuid()),
        sa.Column("previous_owner", sa.String(8), nullable=False),
        sa.Column("owner", sa.String(8), nullable=False),
        sa.Column("ownership_version", sa.Integer(), nullable=False),
        *thread_constraints(), employee_reference("employee_id"), employee_reference("assigned_id"),
    )
    op.create_index(None, "ownership_events", ["tenant_id", "conversation_id", "created_at", "id"])
    op.create_table(
        "consultations", *record_columns(),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("inbound_id", sa.Uuid(), nullable=False),
        sa.Column("assigned_id", sa.Uuid()),
        sa.Column("ownership_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), server_default="open", nullable=False),
        sa.Column("question", sa.String(500), nullable=False),
        sa.Column("public_fact", sa.String(500)),
        sa.Column("answered_by", sa.Uuid()),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        employee_reference("assigned_id"), employee_reference("answered_by"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "execution_id"],
            ["agent_executions.tenant_id", "agent_executions.conversation_id", "agent_executions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "inbound_id"],
            ["messages.tenant_id", "messages.conversation_id", "messages.id"],
        ),
        sa.UniqueConstraint("tenant_id", "conversation_id", "execution_id", "ownership_version", "question"),
        sa.CheckConstraint("status IN ('open', 'answered', 'canceled', 'resolved')", name="status"),
    )
    op.create_index(None, "consultations", ["tenant_id", "conversation_id", "status"])


def downgrade() -> None:
    op.drop_table("consultations")
    op.drop_table("ownership_events")
    op.drop_table("staff_notes")

    op.execute("DELETE FROM agent_executions WHERE resume_source_id IS NOT NULL")
    op.create_foreign_key(
        "fk_agent_executions_tenant_id_conversation_id_id", "agent_executions", "messages",
        ["tenant_id", "conversation_id", "id"], ["tenant_id", "conversation_id", "id"],
    )
    op.drop_column("agent_executions", "source_message_id")
    op.drop_column("agent_executions", "resume_source_id")
    op.drop_constraint("uq_agent_executions_tenant_id_conversation_id_id", "agent_executions", type_="unique")
    op.drop_column("sms_deliveries", "ai_ownership_version")
    op.drop_column("sms_deliveries", "submission_started")
    op.drop_column("sms_deliveries", "inbound_guard_id")
    op.drop_column("messages", "employee_request_id")
    op.drop_column("messages", "employee_id")
    op.drop_column("conversations", "assigned_employee_id")
    op.drop_column("conversations", "ownership_version")
    op.drop_column("conversations", "owner")
    op.drop_column("tenants", "service_policies")
    op.drop_column("tenants", "service_catalog")
    op.drop_column("tenants", "closed_dates")
    op.drop_table("employee_sessions")
    op.drop_table("employees")
