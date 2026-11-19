"""Append-only workflow transition and lifecycle events.

Revision ID: 0002_workflow_events
Revises: 0001_workflow_state
"""
from alembic import op
import sqlalchemy as sa


revision = "0002_workflow_events"
down_revision = "0001_workflow_state"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workflow_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "job_id", sa.String(36),
            sa.ForeignKey("workflow_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workflow_id", sa.String(36),
            sa.ForeignKey("workflow_workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "stage_id", sa.String(36),
            sa.ForeignKey("workflow_stages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "attempt_id", sa.String(36),
            sa.ForeignKey("workflow_attempts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("from_state", sa.String(24), nullable=True),
        sa.Column("to_state", sa.String(24), nullable=True),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("payload_json", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_workflow_events_job_time",
        "workflow_events",
        ["job_id", "occurred_at"],
    )
    op.create_index(
        "ix_workflow_events_stage_time",
        "workflow_events",
        ["stage_id", "occurred_at"],
    )


def downgrade():
    op.drop_index(
        "ix_workflow_events_stage_time",
        table_name="workflow_events",
    )
    op.drop_index(
        "ix_workflow_events_job_time",
        table_name="workflow_events",
    )
    op.drop_table("workflow_events")
