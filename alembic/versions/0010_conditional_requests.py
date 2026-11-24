"""Persist unresolved customer conditions independently of executable proposals."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("conditional_request", JSONB(none_as_null=True), nullable=True))
    op.add_column("tenants", sa.Column("service_settings_managed", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.execute("UPDATE tenants SET service_settings_managed = true WHERE service_policies <> '{}'::jsonb "
               "OR cardinality(closed_dates) > 0")
    op.add_column("consultations", sa.Column("condition_request_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_consultations_condition_request", "consultations", "messages",
        ["tenant_id", "conversation_id", "condition_request_id"], ["tenant_id", "conversation_id", "id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_consultations_condition_request", "consultations", type_="foreignkey")
    op.drop_column("consultations", "condition_request_id")
    op.drop_column("conversations", "conditional_request")
    op.drop_column("tenants", "service_settings_managed")
