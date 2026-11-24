"""Persist normalized dependencies between durable workflow stages.

Revision ID: 0010_workflow_stage_dependencies
Revises: 0009_transactional_outbox
"""
from alembic import op
import sqlalchemy as sa


revision = "0010_workflow_stage_dependencies"
down_revision = "0009_transactional_outbox"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        "uq_workflow_stage_id_workflow",
        "workflow_stages",
        ["id", "workflow_id"],
    )
    op.create_table(
        "workflow_stage_dependencies",
        sa.Column("stage_id", sa.String(length=36), nullable=False),
        sa.Column("depends_on_stage_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.CheckConstraint(
            "stage_id <> depends_on_stage_id",
            name="ck_workflow_stage_dependency_not_self",
        ),
        sa.ForeignKeyConstraint(
            ["stage_id", "workflow_id"],
            ["workflow_stages.id", "workflow_stages.workflow_id"],
            name="fk_workflow_stage_dependency_stage",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["depends_on_stage_id", "workflow_id"],
            ["workflow_stages.id", "workflow_stages.workflow_id"],
            name="fk_workflow_stage_dependency_parent",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("stage_id", "depends_on_stage_id"),
    )
    op.create_index(
        "ix_workflow_stage_dependencies_workflow",
        "workflow_stage_dependencies",
        ["workflow_id", "stage_id"],
    )


def downgrade():
    count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM workflow_stage_dependencies"
    )).scalar_one()
    if count:
        raise RuntimeError(
            "Refusing to drop workflow dependency edges while any exist"
        )
    op.drop_index(
        "ix_workflow_stage_dependencies_workflow",
        table_name="workflow_stage_dependencies",
    )
    op.drop_table("workflow_stage_dependencies")
    op.drop_constraint(
        "uq_workflow_stage_id_workflow",
        "workflow_stages",
        type_="unique",
    )
