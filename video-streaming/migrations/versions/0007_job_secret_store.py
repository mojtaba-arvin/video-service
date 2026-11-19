"""Persist encrypted Job-scoped secrets with owner-bound foreign keys.

Revision ID: 0007_job_secret_store
Revises: 0006_job_tenant_ownership
"""
from alembic import op
import sqlalchemy as sa


revision = "0007_job_secret_store"
down_revision = "0006_job_tenant_ownership"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        "uq_workflow_jobs_secret_owner",
        "workflow_jobs",
        ["id", "owner_tenant_id", "owner_issuer", "owner_subject"],
    )
    op.create_table(
        "workflow_job_secrets",
        sa.Column("secret_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("issuer", sa.String(length=512), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("purpose", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("algorithm", sa.String(length=32), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("wrapped_dek", sa.Text(), nullable=False),
        sa.Column("kms_key_id", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("secret_id"),
        sa.ForeignKeyConstraint(
            ["job_id", "tenant_id", "issuer", "subject"],
            [
                "workflow_jobs.id",
                "workflow_jobs.owner_tenant_id",
                "workflow_jobs.owner_issuer",
                "workflow_jobs.owner_subject",
            ],
            name="fk_workflow_job_secrets_owner",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "job_id", "purpose", "version",
            name="uq_workflow_job_secret_purpose_version",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_workflow_job_secret_version",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED', 'DELETED')",
            name="ck_workflow_job_secret_status",
        ),
    )
    op.create_index(
        "ix_workflow_job_secrets_scope",
        "workflow_job_secrets",
        ["job_id", "tenant_id", "purpose", "status"],
    )


def downgrade():
    op.drop_index(
        "ix_workflow_job_secrets_scope",
        table_name="workflow_job_secrets",
    )
    op.drop_table("workflow_job_secrets")
    op.drop_constraint(
        "uq_workflow_jobs_secret_owner",
        "workflow_jobs",
        type_="unique",
    )
