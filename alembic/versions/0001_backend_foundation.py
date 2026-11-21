"""Create the ServicePilot backend foundation.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def record_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def create_tenant_table(table: str, *columns_and_constraints) -> None:
    op.create_table(
        table,
        *record_columns(),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        *columns_and_constraints,
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f(f"fk_{table}_tenant_id")),
    )


def upgrade() -> None:
    op.create_table(
        "tenants",
        *record_columns(),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
        sa.UniqueConstraint("slug", name=op.f("uq_tenants_slug")),
    )
    create_tenant_table(
        "customers",
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("phone_number", sa.String(16), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_customers_tenant_id_id")),
    )
    create_tenant_table(
        "vehicles",
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("vin", sa.String(17), nullable=False),
        sa.Column("make", sa.String(80), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("model_year", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customers.tenant_id", "customers.id"],
            name=op.f("fk_vehicles_tenant_id_customer_id"),
        ),
        sa.UniqueConstraint("tenant_id", "vin", name=op.f("uq_vehicles_tenant_id_vin")),
        sa.UniqueConstraint(
            "tenant_id", "customer_id", "id", name=op.f("uq_vehicles_tenant_id_customer_id_id")
        ),
    )
    create_tenant_table(
        "appointments",
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("vehicle_id", sa.Uuid(), nullable=False),
        sa.Column("external_dms_id", sa.String(255), nullable=True),
        sa.Column("scheduled_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("service_type", sa.String(120), nullable=False),
        sa.Column("status", sa.String(32), server_default="scheduled", nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customers.tenant_id", "customers.id"],
            name=op.f("fk_appointments_tenant_id_customer_id"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "customer_id", "vehicle_id"],
            ["vehicles.tenant_id", "vehicles.customer_id", "vehicles.id"],
            name=op.f("fk_appointments_tenant_id_customer_id_vehicle_id"),
        ),
        sa.UniqueConstraint("tenant_id", "external_dms_id", name=op.f("uq_appointments_tenant_id_external_dms_id")),
        sa.CheckConstraint("scheduled_end > scheduled_start", name=op.f("ck_appointments_schedule_order")),
        sa.CheckConstraint(
            "status IN ('scheduled', 'completed', 'cancelled')", name=op.f("ck_appointments_status")
        ),
    )
    op.create_index(
        op.f("ix_appointments_tenant_id_scheduled_start"), "appointments", ["tenant_id", "scheduled_start"]
    )
    op.create_index(
        op.f("ix_appointments_tenant_id_customer_id_vehicle_id"),
        "appointments",
        ["tenant_id", "customer_id", "vehicle_id"],
    )
    create_tenant_table(
        "conversations",
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customers.tenant_id", "customers.id"],
            name=op.f("fk_conversations_tenant_id_customer_id"),
        ),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_conversations_tenant_id_id")),
    )
    op.create_index(
        op.f("ix_conversations_tenant_id_customer_id"), "conversations", ["tenant_id", "customer_id"]
    )
    create_tenant_table(
        "messages",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("from_number", sa.String(16), nullable=False),
        sa.Column("to_number", sa.String(16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name=op.f("fk_messages_tenant_id_conversation_id"),
        ),
        sa.CheckConstraint("direction IN ('inbound', 'outbound')", name=op.f("ck_messages_direction")),
    )
    op.create_index(
        op.f("ix_messages_tenant_id_conversation_id_created_at_id"),
        "messages",
        ["tenant_id", "conversation_id", "created_at", "id"],
    )
    create_tenant_table(
        "workflows",
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("workflow_type", sa.String(100), nullable=False),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name=op.f("fk_workflows_tenant_id_conversation_id"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')", name=op.f("ck_workflows_status")
        ),
    )
    op.create_index(
        op.f("ix_workflows_tenant_id_conversation_id"), "workflows", ["tenant_id", "conversation_id"]
    )
    create_tenant_table(
        "integration_events",
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("external_event_id", sa.String(255), nullable=False),
        sa.Column("processing_status", sa.String(32), server_default="received", nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "provider", "external_event_id",
            name=op.f("uq_integration_events_tenant_id_provider_external_event_id"),
        ),
        sa.CheckConstraint(
            "processing_status IN ('received', 'processed', 'failed')",
            name=op.f("ck_integration_events_processing_status"),
        ),
    )
    op.create_index(
        op.f("ix_integration_events_tenant_id_processing_status_created_at"),
        "integration_events",
        ["tenant_id", "processing_status", "created_at"],
    )


def downgrade() -> None:
    for table in (
        "integration_events", "workflows", "messages", "conversations",
        "appointments", "vehicles", "customers", "tenants",
    ):
        op.drop_table(table)
