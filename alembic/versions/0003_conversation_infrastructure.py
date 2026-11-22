"""Add SMS threads, pending conversation state, and message provenance."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("conversations", "customer_id", nullable=True)
    for column in (
        sa.Column("sender_number", sa.String(16)),
        sa.Column("recipient_number", sa.String(16)),
        sa.Column("status", sa.String(16), server_default="open", nullable=False),
        sa.Column("customer_resolution", sa.String(16), server_default="resolved", nullable=False),
        sa.Column("pending_action_type", sa.String(100)),
        sa.Column("pending_action_payload", JSONB(none_as_null=True)),
        sa.Column("pending_action_expires_at", sa.DateTime(timezone=True)),
        sa.Column("pending_confirmation", sa.String(32)),
    ):
        op.add_column("conversations", column)

    op.create_check_constraint("status", "conversations", "status IN ('open', 'closed')")
    op.create_check_constraint(
        "sms_addresses", "conversations", "(sender_number IS NULL) = (recipient_number IS NULL)",
    )
    op.create_check_constraint(
        "customer_resolution", "conversations",
        "(customer_resolution = 'resolved' AND customer_id IS NOT NULL) OR "
        "(customer_resolution IN ('unmatched', 'ambiguous') AND customer_id IS NULL)",
    )
    op.create_check_constraint(
        "pending_action", "conversations",
        "(pending_action_type IS NULL AND pending_action_payload IS NULL AND "
        "pending_action_expires_at IS NULL AND pending_confirmation IS NULL) OR "
        "(pending_action_type IS NOT NULL AND pending_action_payload IS NOT NULL AND "
        "jsonb_typeof(pending_action_payload) = 'object' AND "
        "pending_action_expires_at IS NOT NULL AND pending_confirmation IS NOT NULL AND "
        "pending_confirmation IN ('awaiting_confirmation', 'confirmed'))",
    )
    op.create_index(
        "uq_conversations_open_sms_thread", "conversations",
        ["tenant_id", "sender_number", "recipient_number"], unique=True,
        postgresql_where=sa.text("status = 'open' AND sender_number IS NOT NULL"),
    )
    op.add_column("messages", sa.Column("provider", sa.String(100), server_default="legacy", nullable=False))
    op.add_column("messages", sa.Column("external_message_id", sa.String(255)))
    op.add_column("messages", sa.Column("occurred_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE messages SET occurred_at = created_at")
    op.alter_column("messages", "occurred_at", nullable=False, server_default=sa.text("clock_timestamp()"))
    op.drop_index("ix_messages_tenant_id_conversation_id_created_at_id", table_name="messages")
    op.create_index(
        "ix_messages_tenant_id_conversation_id_occurred_at_created_at_id", "messages",
        ["tenant_id", "conversation_id", "occurred_at", "created_at", "id"],
    )
    op.create_index(
        "uq_messages_inbound_provider_id", "messages", ["tenant_id", "provider", "external_message_id"],
        unique=True, postgresql_where=sa.text("direction = 'inbound' AND external_message_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.alter_column("conversations", "customer_id", nullable=False)

    op.drop_index("uq_messages_inbound_provider_id", table_name="messages")
    op.drop_index("ix_messages_tenant_id_conversation_id_occurred_at_created_at_id", table_name="messages")
    op.create_index(
        "ix_messages_tenant_id_conversation_id_created_at_id", "messages",
        ["tenant_id", "conversation_id", "created_at", "id"],
    )
    for column in ("occurred_at", "external_message_id", "provider"):
        op.drop_column("messages", column)

    op.drop_index("uq_conversations_open_sms_thread", table_name="conversations")
    for constraint in ("pending_action", "customer_resolution", "sms_addresses", "status"):
        op.drop_constraint(constraint, "conversations", type_="check")
    for column in (
        "pending_confirmation", "pending_action_expires_at", "pending_action_payload",
        "pending_action_type", "customer_resolution", "status", "recipient_number", "sender_number",
    ):
        op.drop_column("conversations", column)
