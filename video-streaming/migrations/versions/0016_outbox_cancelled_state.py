"""Track dispatch intents suppressed by durable Job cancellation.

Revision ID: 0016_outbox_cancelled_state
Revises: 0015_asset_provenance
"""
from alembic import op


revision = "0016_outbox_cancelled_state"
down_revision = "0015_asset_provenance"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        "ck_workflow_outbox_state", "workflow_outbox", type_="check"
    )
    op.create_check_constraint(
        "ck_workflow_outbox_state",
        "workflow_outbox",
        "state IN ('PENDING', 'PROCESSING', 'PUBLISHED', 'DEAD', 'CANCELLED')",
    )


def downgrade():
    cancelled = op.get_bind().exec_driver_sql(
        "SELECT COUNT(*) FROM workflow_outbox WHERE state = 'CANCELLED'"
    ).scalar_one()
    if cancelled:
        raise RuntimeError(
            "Refusing to remove CANCELLED outbox state while cancellation "
            "history exists"
        )
    op.drop_constraint(
        "ck_workflow_outbox_state", "workflow_outbox", type_="check"
    )
    op.create_check_constraint(
        "ck_workflow_outbox_state",
        "workflow_outbox",
        "state IN ('PENDING', 'PROCESSING', 'PUBLISHED', 'DEAD')",
    )
