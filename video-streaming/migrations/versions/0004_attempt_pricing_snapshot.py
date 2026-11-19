"""Persist immutable price-card snapshots and versions per attempt.

Revision ID: 0004_attempt_pricing_snapshot
Revises: 0003_playback_qoe
"""
from alembic import op
import sqlalchemy as sa


revision = "0004_attempt_pricing_snapshot"
down_revision = "0003_playback_qoe"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workflow_attempts",
        sa.Column("pricing_model_version", sa.String(64), nullable=True),
    )
    op.add_column(
        "workflow_attempts",
        sa.Column("pricing_snapshot_json", sa.JSON(), nullable=True),
    )


def downgrade():
    op.drop_column("workflow_attempts", "pricing_snapshot_json")
    op.drop_column("workflow_attempts", "pricing_model_version")
