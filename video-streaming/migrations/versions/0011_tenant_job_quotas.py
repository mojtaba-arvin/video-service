"""Add provisioned per-tenant active Job admission policies.

Revision ID: 0011_tenant_job_quotas
Revises: 0010_workflow_stage_dependencies
"""
from alembic import op
import sqlalchemy as sa


revision = "0011_tenant_job_quotas"
down_revision = "0010_workflow_stage_dependencies"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workflow_tenant_job_quota_policies",
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("max_active_jobs", sa.Integer(), nullable=False),
        sa.Column(
            "policy_version", sa.Integer(), nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "max_active_jobs >= 0",
            name="ck_workflow_tenant_job_quota_nonnegative",
        ),
        sa.PrimaryKeyConstraint("tenant_id"),
    )


def downgrade():
    count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM workflow_tenant_job_quota_policies"
    )).scalar_one()
    if count:
        raise RuntimeError(
            "Refusing to drop tenant quota policies while any exist"
        )
    op.drop_table("workflow_tenant_job_quota_policies")
