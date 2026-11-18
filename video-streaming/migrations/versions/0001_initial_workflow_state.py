"""Initial durable workflow state schema.

Revision ID: 0001_workflow_state
Revises:
"""
from alembic import op
import sqlalchemy as sa


revision = "0001_workflow_state"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workflow_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("reference_id", sa.String(255), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_table(
        "workflow_workflows",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "job_id", sa.String(36),
            sa.ForeignKey("workflow_jobs.id", ondelete="CASCADE"),
            nullable=False, unique=True,
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("execution_backend", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "workflow_stages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workflow_id", sa.String(36),
            sa.ForeignKey("workflow_workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage_key", sa.String(128), nullable=False),
        sa.Column("operation", sa.String(128), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("input_contract_json", sa.JSON(), nullable=False),
        sa.Column("output_contract_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("workflow_id", "stage_key", name="uq_workflow_stage_key"),
    )
    op.create_index(
        "ix_workflow_stages_status_updated",
        "workflow_stages", ["status", "updated_at"],
    )
    op.create_table(
        "workflow_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "stage_id", sa.String(36),
            sa.ForeignKey("workflow_stages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("execution_backend", sa.String(64), nullable=True),
        sa.Column("execution_ref", sa.String(255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("cpu_seconds", sa.Float(), nullable=True),
        sa.Column("peak_memory_bytes", sa.BigInteger(), nullable=True),
        sa.Column("input_bytes", sa.BigInteger(), nullable=True),
        sa.Column("output_bytes", sa.BigInteger(), nullable=True),
        sa.Column("artifact_count", sa.Integer(), nullable=False),
        sa.Column("estimated_cost", sa.Numeric(18, 8), nullable=True),
        sa.Column("cost_currency", sa.String(8), nullable=True),
        sa.Column("pricing_model_id", sa.String(128), nullable=True),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.UniqueConstraint("stage_id", "attempt_number", name="uq_stage_attempt_number"),
    )
    op.create_index(
        "ix_workflow_attempts_state_started",
        "workflow_attempts", ["state", "started_at"],
    )
    op.create_table(
        "workflow_artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "job_id", sa.String(36),
            sa.ForeignKey("workflow_jobs.id", ondelete="CASCADE"),
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
        sa.Column("artifact_type", sa.String(64), nullable=False),
        sa.Column("logical_name", sa.String(255), nullable=False),
        sa.Column("uri", sa.Text(), nullable=True),
        sa.Column("media_type", sa.String(128), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("checksum_sha256", sa.String(64), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "stage_id", "attempt_id", "logical_name",
            name="uq_stage_attempt_artifact_name",
        ),
    )
    op.create_index(
        "ix_workflow_artifacts_job_type",
        "workflow_artifacts", ["job_id", "artifact_type"],
    )


def downgrade():
    op.drop_index("ix_workflow_artifacts_job_type", table_name="workflow_artifacts")
    op.drop_table("workflow_artifacts")
    op.drop_index("ix_workflow_attempts_state_started", table_name="workflow_attempts")
    op.drop_table("workflow_attempts")
    op.drop_index("ix_workflow_stages_status_updated", table_name="workflow_stages")
    op.drop_table("workflow_stages")
    op.drop_table("workflow_workflows")
    op.drop_table("workflow_jobs")
