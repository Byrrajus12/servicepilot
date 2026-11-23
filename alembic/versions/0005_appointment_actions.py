"""Add durable SMS appointment action results."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_appointments_tenant_id_customer_id_id", "appointments", ["tenant_id", "customer_id", "id"],
    )
    op.create_table(
        "appointment_action_results",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("decision_message_id", sa.Uuid()),
        sa.Column("appointment_id", sa.Uuid()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("proposal", JSONB(), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("reply_body", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"], ["conversations.tenant_id", "conversations.id"],
        ),
        sa.ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "customer_id", "appointment_id"],
            ["appointments.tenant_id", "appointments.customer_id", "appointments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "decision_message_id"],
            ["messages.tenant_id", "messages.conversation_id", "messages.id"],
        ),
        sa.UniqueConstraint("tenant_id", "conversation_id", "decision_message_id"),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'unavailable', 'appointment_changed', 'declined', 'expired', "
            "'superseded', 'identity_required')", name="outcome",
        ),
        sa.CheckConstraint("(outcome = 'succeeded') = (appointment_id IS NOT NULL)", name="appointment"),
    )


def downgrade() -> None:
    # Clear only Phase 3B.2 prompts so an upgrade cannot reactivate them.
    op.execute(
        "UPDATE conversations SET pending_action_type = NULL, pending_action_payload = NULL, "
        "pending_action_expires_at = NULL, pending_confirmation = NULL "
        "WHERE pending_action_payload ? 'prompt_id'"
    )
    op.drop_table("appointment_action_results")
    op.drop_constraint("uq_appointments_tenant_id_customer_id_id", "appointments", type_="unique")
