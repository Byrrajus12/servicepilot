"""Record redacted advisor attempt diagnostics."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_executions", sa.Column(
        "advisor_attempts", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False,
    ))


def downgrade() -> None:
    op.drop_column("agent_executions", "advisor_attempts")
