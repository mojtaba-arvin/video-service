"""Persist immutable execution-plan snapshots with workflow records.

Revision ID: 0005_execution_plan_snapshot
Revises: 0004_attempt_pricing_snapshot
"""
from alembic import op
import sqlalchemy as sa


revision = "0005_execution_plan_snapshot"
down_revision = "0004_attempt_pricing_snapshot"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workflow_workflows",
        sa.Column("workflow_version", sa.String(64), nullable=True),
    )
    op.add_column(
        "workflow_workflows",
        sa.Column("execution_plan_schema_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "workflow_workflows",
        sa.Column("execution_plan_fingerprint", sa.String(64), nullable=True),
    )
    op.add_column(
        "workflow_workflows",
        sa.Column("execution_plan_json", sa.JSON(none_as_null=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_workflow_execution_plan_snapshot_complete",
        "workflow_workflows",
        "(execution_plan_json IS NULL AND "
        "execution_plan_fingerprint IS NULL AND "
        "execution_plan_schema_version IS NULL AND workflow_version IS NULL) "
        "OR (execution_plan_json IS NOT NULL AND "
        "execution_plan_fingerprint IS NOT NULL AND "
        "execution_plan_schema_version IS NOT NULL AND workflow_version IS NOT NULL)",
    )


def downgrade():
    op.drop_constraint(
        "ck_workflow_execution_plan_snapshot_complete",
        "workflow_workflows",
        type_="check",
    )
    op.drop_column("workflow_workflows", "execution_plan_json")
    op.drop_column("workflow_workflows", "execution_plan_fingerprint")
    op.drop_column("workflow_workflows", "execution_plan_schema_version")
    op.drop_column("workflow_workflows", "workflow_version")
