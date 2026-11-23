"""Track leased Service Advisor executions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_messages_tenant_id_conversation_id_id", "messages", ["tenant_id", "conversation_id", "id"],
    )
    op.create_table(
        "agent_executions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("reply_id", sa.Uuid()),
        sa.Column("model", sa.String(120)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("tool_calls", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("error_code", sa.String(40)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "id"],
            ["messages.tenant_id", "messages.conversation_id", "messages.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "reply_id"],
            ["messages.tenant_id", "messages.conversation_id", "messages.id"],
        ),
        sa.CheckConstraint("status IN ('pending', 'running', 'completed', 'failed')", name="status"),
        sa.CheckConstraint("(status = 'running') = (lease_expires_at IS NOT NULL)", name="lease"),
        sa.CheckConstraint("(status = 'completed') = (reply_id IS NOT NULL)", name="reply"),
    )
    op.create_index(
        "ix_agent_executions_unfinished_conversation", "agent_executions", ["tenant_id", "conversation_id"],
        postgresql_where=sa.text("status != 'completed'"),
    )
    op.create_index(
        "uq_agent_executions_running_conversation", "agent_executions", ["tenant_id", "conversation_id"],
        unique=True, postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_table("agent_executions")
    op.drop_constraint("uq_messages_tenant_id_conversation_id_id", "messages", type_="unique")
