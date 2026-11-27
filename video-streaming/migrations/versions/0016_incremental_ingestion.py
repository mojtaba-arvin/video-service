"""Persist resumable source ingestion and provisional chunk identity.

Revision ID: 0016_incremental_ingestion
Revises: 0015_asset_provenance
"""
from alembic import op
import sqlalchemy as sa


revision = "0016_incremental_ingestion"
down_revision = "0015_asset_provenance"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workflow_ingestions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("source_name", sa.String(length=255), nullable=False),
        sa.Column("chunk_size_bytes", sa.Integer(), nullable=False),
        sa.Column("max_pending_chunks", sa.Integer(), nullable=False),
        sa.Column("expected_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("expected_sha256", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("final_artifact_uri", sa.String(length=2048), nullable=True),
        sa.Column("final_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("final_sha256", sa.String(length=64), nullable=True),
        sa.Column("cleanup_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["job_id"], ["workflow_jobs.id"],
            name="fk_workflow_ingestion_job", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "job_id", name="uq_workflow_ingestion_job_identity"),
        sa.CheckConstraint("chunk_size_bytes > 0", name="ck_workflow_ingestion_chunk_size"),
        sa.CheckConstraint("max_pending_chunks > 0", name="ck_workflow_ingestion_pending_limit"),
        sa.CheckConstraint(
            "expected_size_bytes IS NULL OR expected_size_bytes > 0",
            name="ck_workflow_ingestion_expected_size",
        ),
        sa.CheckConstraint(
            "expected_sha256 IS NULL OR length(expected_sha256) = 64",
            name="ck_workflow_ingestion_expected_hash",
        ),
        sa.CheckConstraint(
            "state IN ('OPEN', 'FINALIZING', 'COMPLETE', 'CANCELLED', 'FAILED')",
            name="ck_workflow_ingestion_state",
        ),
    )
    op.create_index(
        "ix_workflow_ingestions_job_state", "workflow_ingestions",
        ["job_id", "state"],
    )
    op.create_table(
        "workflow_ingestion_chunks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("ingestion_id", sa.String(length=36), nullable=False),
        sa.Column("chunk_id", sa.String(length=36), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("byte_offset", sa.BigInteger(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("object_uri", sa.String(length=2048), nullable=False),
        sa.Column("start_seconds", sa.Integer(), nullable=True),
        sa.Column("end_seconds", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["ingestion_id"], ["workflow_ingestions.id"],
            name="fk_workflow_ingestion_chunk_session", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ingestion_id", "chunk_index",
            name="uq_workflow_ingestion_chunk_index",
        ),
        sa.UniqueConstraint(
            "ingestion_id", "chunk_id",
            name="uq_workflow_ingestion_chunk_identity",
        ),
        sa.CheckConstraint("chunk_index >= 0", name="ck_workflow_ingestion_chunk_index"),
        sa.CheckConstraint("byte_offset >= 0", name="ck_workflow_ingestion_chunk_offset"),
        sa.CheckConstraint("size_bytes > 0", name="ck_workflow_ingestion_chunk_size"),
        sa.CheckConstraint("length(checksum_sha256) = 64", name="ck_workflow_ingestion_chunk_hash"),
        sa.CheckConstraint(
            "(start_seconds IS NULL AND end_seconds IS NULL) OR "
            "(start_seconds IS NOT NULL AND end_seconds IS NOT NULL AND "
            "start_seconds >= 0 AND end_seconds > start_seconds)",
            name="ck_workflow_ingestion_chunk_time_range",
        ),
    )
    op.create_index(
        "ix_workflow_ingestion_chunks_order", "workflow_ingestion_chunks",
        ["ingestion_id", "chunk_index"],
    )


def downgrade():
    op.execute(
        "LOCK TABLE workflow_ingestions, workflow_ingestion_chunks "
        "IN SHARE ROW EXCLUSIVE MODE"
    )
    count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM workflow_ingestions"
    )).scalar_one()
    if count:
        raise RuntimeError(
            "Refusing to drop incremental ingestion while session records exist"
        )
    op.drop_index("ix_workflow_ingestion_chunks_order", table_name="workflow_ingestion_chunks")
    op.drop_table("workflow_ingestion_chunks")
    op.drop_index("ix_workflow_ingestions_job_state", table_name="workflow_ingestions")
    op.drop_table("workflow_ingestions")
