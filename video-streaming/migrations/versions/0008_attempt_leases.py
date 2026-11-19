"""Add expiring, fenced worker leases to durable workflow attempts.

Revision ID: 0008_attempt_leases
Revises: 0007_job_secret_store
"""
from alembic import op
import sqlalchemy as sa


revision = "0008_attempt_leases"
down_revision = "0007_job_secret_store"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workflow_attempts",
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "workflow_attempts",
        sa.Column("lease_token_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "workflow_attempts",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workflow_attempts",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_workflow_attempt_lease_complete",
        "workflow_attempts",
        "(lease_owner IS NULL AND lease_token_hash IS NULL AND "
        "lease_expires_at IS NULL AND heartbeat_at IS NULL) OR "
        "(lease_owner IS NOT NULL AND lease_token_hash IS NOT NULL AND "
        "lease_expires_at IS NOT NULL AND heartbeat_at IS NOT NULL)",
    )
    op.create_index(
        "ix_workflow_attempts_lease_expiry",
        "workflow_attempts",
        ["state", "lease_expires_at"],
    )


def downgrade():
    op.drop_index(
        "ix_workflow_attempts_lease_expiry",
        table_name="workflow_attempts",
    )
    op.drop_constraint(
        "ck_workflow_attempt_lease_complete",
        "workflow_attempts",
        type_="check",
    )
    op.drop_column("workflow_attempts", "heartbeat_at")
    op.drop_column("workflow_attempts", "lease_expires_at")
    op.drop_column("workflow_attempts", "lease_token_hash")
    op.drop_column("workflow_attempts", "lease_owner")
