"""Add tenant-owned creative projects and immutable project revisions.

Revision ID: 0013_creative_project_revisions
Revises: 0012_tenant_quota_audit
"""
from alembic import op
import sqlalchemy as sa


revision = "0013_creative_project_revisions"
down_revision = "0012_tenant_quota_audit"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workflow_creative_projects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_by_issuer", sa.String(length=512), nullable=False),
        sa.Column("created_by_subject", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "tenant_id", name="uq_workflow_creative_project_tenant"
        ),
    )
    op.create_index(
        "ix_workflow_creative_projects_tenant_created",
        "workflow_creative_projects", ["tenant_id", "created_at"],
    )
    op.create_table(
        "workflow_creative_project_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by_issuer", sa.String(length=512), nullable=False),
        sa.Column("created_by_subject", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "revision_number >= 1",
            name="ck_workflow_project_revision_positive",
        ),
        sa.CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_workflow_project_revision_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "tenant_id"],
            ["workflow_creative_projects.id", "workflow_creative_projects.tenant_id"],
            name="fk_workflow_project_revision_project_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "tenant_id", "revision_number",
            name="uq_workflow_project_revision_number",
        ),
        sa.UniqueConstraint(
            "id", "project_id", "tenant_id",
            name="uq_workflow_project_revision_identity",
        ),
    )
    op.create_index(
        "ix_workflow_project_revisions_project_created",
        "workflow_creative_project_revisions",
        ["project_id", "tenant_id", "created_at"],
    )
    op.execute("""
        CREATE FUNCTION workflow_reject_project_revision_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'creative project revisions are immutable';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_workflow_project_revision_immutable
        BEFORE UPDATE OR DELETE ON workflow_creative_project_revisions
        FOR EACH ROW EXECUTE FUNCTION workflow_reject_project_revision_mutation()
    """)


def downgrade():
    count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM workflow_creative_project_revisions"
    )).scalar_one()
    if count:
        raise RuntimeError(
            "Refusing to drop immutable creative project revision history"
        )
    project_count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM workflow_creative_projects"
    )).scalar_one()
    if project_count:
        raise RuntimeError(
            "Refusing to drop creative projects while project records exist"
        )
    op.execute(
        "DROP TRIGGER trg_workflow_project_revision_immutable "
        "ON workflow_creative_project_revisions"
    )
    op.execute("DROP FUNCTION workflow_reject_project_revision_mutation()")
    op.drop_index(
        "ix_workflow_project_revisions_project_created",
        table_name="workflow_creative_project_revisions",
    )
    op.drop_table("workflow_creative_project_revisions")
    op.drop_index(
        "ix_workflow_creative_projects_tenant_created",
        table_name="workflow_creative_projects",
    )
    op.drop_table("workflow_creative_projects")
