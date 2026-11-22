"""Add dealership scheduling settings and a customer phone lookup index."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = (
        sa.Column("timezone", sa.String(100), server_default="America/Detroit", nullable=False),
        sa.Column("service_days", ARRAY(sa.Integer()), server_default=sa.text("'{0,1,2,3,4}'"), nullable=False),
        sa.Column("opens_at", sa.Time(), server_default="08:00", nullable=False),
        sa.Column("closes_at", sa.Time(), server_default="17:00", nullable=False),
        sa.Column("appointment_minutes", sa.Integer(), server_default="60", nullable=False),
        sa.Column("service_capacity", sa.Integer(), server_default="1", nullable=False),
    )
    for column in columns:
        op.add_column("tenants", column)

    op.create_check_constraint("business_hours", "tenants", "opens_at < closes_at")
    op.create_check_constraint(
        "service_days", "tenants",
        "cardinality(service_days) > 0 AND service_days <@ ARRAY[0,1,2,3,4,5,6]",
    )
    op.create_check_constraint("appointment_minutes", "tenants", "appointment_minutes BETWEEN 1 AND 720")
    op.create_check_constraint("service_capacity", "tenants", "service_capacity > 0")
    op.create_index("ix_customers_tenant_id_phone_number", "customers", ["tenant_id", "phone_number"])


def downgrade() -> None:
    op.drop_index("ix_customers_tenant_id_phone_number", table_name="customers")
    for constraint in ("business_hours", "service_days", "appointment_minutes", "service_capacity"):
        op.drop_constraint(constraint, "tenants", type_="check")
    for column in ("timezone", "service_days", "opens_at", "closes_at", "appointment_minutes", "service_capacity"):
        op.drop_column("tenants", column)
