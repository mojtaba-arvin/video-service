"""Add append-only operator audit for tenant quota provisioning.

Revision ID: 0012_tenant_quota_audit
Revises: 0011_tenant_job_quotas
"""
from alembic import op
import sqlalchemy as sa


revision = "0012_tenant_quota_audit"
down_revision = "0011_tenant_job_quotas"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workflow_tenant_job_quota_audit",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("operator_issuer", sa.String(length=512), nullable=False),
        sa.Column("operator_subject", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column("previous_max_active_jobs", sa.Integer(), nullable=True),
        sa.Column("max_active_jobs", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "max_active_jobs >= 0",
            name="ck_workflow_tenant_job_quota_audit_nonnegative",
        ),
        sa.CheckConstraint(
            "policy_version >= 1",
            name="ck_workflow_tenant_job_quota_audit_version",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_tenant_job_quota_audit_tenant_created",
        "workflow_tenant_job_quota_audit",
        ["tenant_id", "created_at"],
    )


def downgrade():
    count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM workflow_tenant_job_quota_audit"
    )).scalar_one()
    if count:
        raise RuntimeError(
            "Refusing to drop tenant quota audit history while events exist"
        )
    op.drop_index(
        "ix_workflow_tenant_job_quota_audit_tenant_created",
        table_name="workflow_tenant_job_quota_audit",
    )
    op.drop_table("workflow_tenant_job_quota_audit")
