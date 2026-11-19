"""Persist tenant ownership for durable jobs.

Revision ID: 0006_job_tenant_ownership
Revises: 0005_execution_plan_snapshot
"""
from alembic import op
import sqlalchemy as sa


revision = "0006_job_tenant_ownership"
down_revision = "0005_execution_plan_snapshot"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workflow_jobs",
        sa.Column("owner_tenant_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "workflow_jobs",
        sa.Column("owner_issuer", sa.String(512), nullable=True),
    )
    op.add_column(
        "workflow_jobs",
        sa.Column("owner_subject", sa.String(255), nullable=True),
    )
    op.create_check_constraint(
        "ck_workflow_job_owner_identity_complete",
        "workflow_jobs",
        "(owner_tenant_id IS NULL AND owner_issuer IS NULL AND "
        "owner_subject IS NULL) OR "
        "(owner_tenant_id IS NOT NULL AND owner_issuer IS NOT NULL AND "
        "owner_subject IS NOT NULL)",
    )
    op.create_index(
        "ix_workflow_jobs_owner_tenant_created",
        "workflow_jobs",
        ["owner_tenant_id", "created_at"],
    )


def downgrade():
    op.drop_index(
        "ix_workflow_jobs_owner_tenant_created",
        table_name="workflow_jobs",
    )
    op.drop_constraint(
        "ck_workflow_job_owner_identity_complete",
        "workflow_jobs",
        type_="check",
    )
    op.drop_column("workflow_jobs", "owner_subject")
    op.drop_column("workflow_jobs", "owner_issuer")
    op.drop_column("workflow_jobs", "owner_tenant_id")
