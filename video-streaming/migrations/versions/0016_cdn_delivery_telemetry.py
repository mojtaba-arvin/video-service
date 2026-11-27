"""Persist privacy-minimized CDN delivery telemetry with bounded retention.

Revision ID: 0016_cdn_delivery_telemetry
Revises: 0015_asset_provenance
"""
from alembic import op
import sqlalchemy as sa


revision = "0016_cdn_delivery_telemetry"
down_revision = "0015_asset_provenance"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cdn_delivery_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_event_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("stage_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
        sa.Column("artifact_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("asset_version", sa.String(length=128), nullable=True),
        sa.Column("delivery_config_id", sa.String(length=128), nullable=True),
        sa.Column("edge_region", sa.String(length=64), nullable=True),
        sa.Column("cache_status", sa.String(length=16), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("response_time_ms", sa.BigInteger(), nullable=True),
        sa.Column("bytes_sent", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("schema_version = 1", name="ck_cdn_delivery_schema_version"),
        sa.CheckConstraint(
            "cache_status IN ('HIT', 'MISS', 'BYPASS', 'REVALIDATED', 'STALE', 'UNKNOWN')",
            name="ck_cdn_delivery_cache_status",
        ),
        sa.CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name="ck_cdn_delivery_http_status",
        ),
        sa.CheckConstraint(
            "response_time_ms IS NULL OR response_time_ms BETWEEN 0 AND 86400000",
            name="ck_cdn_delivery_response_time",
        ),
        sa.CheckConstraint(
            "bytes_sent IS NULL OR bytes_sent >= 0",
            name="ck_cdn_delivery_bytes_sent",
        ),
        sa.CheckConstraint("expires_at > ingested_at", name="ck_cdn_delivery_expiry"),
        sa.ForeignKeyConstraint(["job_id"], ["workflow_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflow_workflows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["stage_id"], ["workflow_stages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["attempt_id"], ["workflow_attempts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["artifact_id"], ["workflow_artifacts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["session_id"], ["playback_sessions.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("source", "source_event_id", name="uq_cdn_delivery_source_event"),
    )
    op.create_index(
        "ix_cdn_delivery_job_source_time", "cdn_delivery_events",
        ["job_id", "source", "source_timestamp"],
    )
    op.create_index(
        "ix_cdn_delivery_stage_time", "cdn_delivery_events",
        ["stage_id", "source_timestamp"],
    )
    op.create_index(
        "ix_cdn_delivery_session_time", "cdn_delivery_events",
        ["session_id", "source_timestamp"],
    )
    op.create_index(
        "ix_cdn_delivery_expiry", "cdn_delivery_events", ["expires_at"],
    )


def downgrade():
    count = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM cdn_delivery_events")
    ).scalar_one()
    if count:
        raise RuntimeError(
            "Refusing to drop retained CDN delivery evidence; expire it first"
        )
    op.drop_index("ix_cdn_delivery_expiry", table_name="cdn_delivery_events")
    op.drop_index("ix_cdn_delivery_session_time", table_name="cdn_delivery_events")
    op.drop_index("ix_cdn_delivery_stage_time", table_name="cdn_delivery_events")
    op.drop_index("ix_cdn_delivery_job_source_time", table_name="cdn_delivery_events")
    op.drop_table("cdn_delivery_events")
