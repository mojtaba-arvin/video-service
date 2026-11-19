"""Add durable leased dispatch references for immutable workflow plans.

Revision ID: 0009_transactional_outbox
Revises: 0008_attempt_leases
"""
from alembic import op
import sqlalchemy as sa


revision = "0009_transactional_outbox"
down_revision = "0008_attempt_leases"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workflow_outbox",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("plan_version", sa.String(length=64), nullable=False),
        sa.Column("plan_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_by", sa.String(length=255), nullable=True),
        sa.Column("lock_token_hash", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=128), nullable=True),
        sa.CheckConstraint(
            "state IN ('PENDING', 'PROCESSING', 'PUBLISHED', 'DEAD')",
            name="ck_workflow_outbox_state",
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name="ck_workflow_outbox_schema_version",
        ),
        sa.CheckConstraint(
            "(locked_by IS NULL AND lock_token_hash IS NULL AND "
            "lease_expires_at IS NULL) OR "
            "(locked_by IS NOT NULL AND lock_token_hash IS NOT NULL AND "
            "lease_expires_at IS NOT NULL)",
            name="ck_workflow_outbox_lease_complete",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["workflow_jobs.id"],
            name="fk_workflow_outbox_job", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["workflow_workflows.id"],
            name="fk_workflow_outbox_workflow", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_workflow_outbox_dedupe_key"),
    )
    op.create_index(
        "ix_workflow_outbox_ready",
        "workflow_outbox",
        ["state", "available_at", "lease_expires_at"],
    )
    op.create_index(
        "ix_workflow_outbox_job_created",
        "workflow_outbox",
        ["job_id", "created_at"],
    )


def downgrade():
    op.drop_index("ix_workflow_outbox_job_created", table_name="workflow_outbox")
    op.drop_index("ix_workflow_outbox_ready", table_name="workflow_outbox")
    op.drop_table("workflow_outbox")
