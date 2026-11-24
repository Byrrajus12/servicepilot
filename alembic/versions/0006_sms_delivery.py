"""Add the SMS outbox, consent state and scheduled agent retries."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_executions", sa.Column("next_attempt_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        op.f("ck_agent_executions_retry"), "agent_executions", "next_attempt_at IS NULL OR status = 'failed'",
    )
    op.drop_index("ix_agent_executions_unfinished_conversation", table_name="agent_executions")
    op.create_index(
        "ix_agent_executions_unfinished_conversation", "agent_executions", ["tenant_id", "conversation_id"],
        postgresql_where=sa.text("status IN ('pending', 'running') OR next_attempt_at IS NOT NULL"),
    )
    op.create_table(
        "sms_deliveries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("account_sid", sa.String(34)),
        sa.Column("sender", sa.String(16), nullable=False),
        sa.Column("recipient", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("confirmation_for_id", sa.Uuid()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("provider_message_id", sa.String(64)),
        sa.Column("error_code", sa.String(40)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "id"],
            ["messages.tenant_id", "messages.conversation_id", "messages.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "confirmation_for_id"],
            ["messages.tenant_id", "messages.conversation_id", "messages.id"],
        ),
        sa.UniqueConstraint("provider", "account_sid", "provider_message_id"),
        sa.CheckConstraint("provider IN ('simulation', 'twilio')", name="provider"),
        sa.CheckConstraint("(provider = 'twilio') = (account_sid IS NOT NULL)", name="account"),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'uncertain', 'accepted', 'sent', 'delivered', 'failed', 'undelivered')",
            name="status",
        ),
        sa.CheckConstraint("(status = 'sending') = (lease_expires_at IS NOT NULL)", name="lease"),
        sa.CheckConstraint("(status = 'pending') = (next_attempt_at IS NOT NULL)", name="retry"),
        sa.CheckConstraint("(status = 'delivered') = (delivered_at IS NOT NULL)", name="delivered_at"),
        sa.CheckConstraint("(confirmation_for_id IS NULL) = (expires_at IS NULL)", name="confirmation"),
    )
    op.create_index("ix_sms_deliveries_work", "sms_deliveries", ["status", "next_attempt_at", "lease_expires_at"])
    op.create_index(
        "ix_sms_deliveries_unfinished_thread", "sms_deliveries", ["tenant_id", "conversation_id", "created_at", "id"],
        postgresql_where=sa.text("status IN ('pending', 'sending')"),
    )
    op.create_table(
        "sms_consents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.String(64), nullable=False),
        sa.Column("sender", sa.String(16), nullable=False),
        sa.Column("recipient", sa.String(16), nullable=False),
        sa.Column("suppressed", sa.Boolean(), nullable=False),
        sa.Column("last_opt_out_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "channel", "sender", "recipient"),
    )


def downgrade() -> None:
    op.drop_table("sms_consents")
    op.drop_table("sms_deliveries")
    op.drop_index("ix_agent_executions_unfinished_conversation", table_name="agent_executions")
    op.create_index(
        "ix_agent_executions_unfinished_conversation", "agent_executions", ["tenant_id", "conversation_id"],
        postgresql_where=sa.text("status != 'completed'"),
    )
    op.drop_constraint(op.f("ck_agent_executions_retry"), "agent_executions", type_="check")
    op.drop_column("agent_executions", "next_attempt_at")
