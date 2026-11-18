"""Durable playback QoE sessions and idempotent telemetry events.

Revision ID: 0003_playback_qoe
Revises: 0002_workflow_events
"""
from alembic import op
import sqlalchemy as sa


revision = "0003_playback_qoe"
down_revision = "0002_workflow_events"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "playback_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "job_id", sa.String(36),
            sa.ForeignKey("workflow_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "stage_id", sa.String(36),
            sa.ForeignKey("workflow_stages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "manifest_artifact_id", sa.String(36),
            sa.ForeignKey("workflow_artifacts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("client_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("playback_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("startup_ms", sa.BigInteger(), nullable=True),
        sa.Column(
            "buffering_started_at", sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("rebuffer_count", sa.Integer(), nullable=False),
        sa.Column("total_buffering_ms", sa.BigInteger(), nullable=False),
        sa.Column("decoder_error_count", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_playback_sessions_stage_started",
        "playback_sessions",
        ["stage_id", "started_at"],
    )
    op.create_index(
        "ix_playback_sessions_job_started",
        "playback_sessions",
        ["job_id", "started_at"],
    )
    op.create_table(
        "playback_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id", sa.String(36),
            sa.ForeignKey("playback_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("startup_ms", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "session_id", "event_id",
            name="uq_playback_session_event_id",
        ),
    )
    op.create_index(
        "ix_playback_events_session_time",
        "playback_events",
        ["session_id", "received_at"],
    )


def downgrade():
    op.drop_index(
        "ix_playback_events_session_time",
        table_name="playback_events",
    )
    op.drop_table("playback_events")
    op.drop_index(
        "ix_playback_sessions_job_started",
        table_name="playback_sessions",
    )
    op.drop_index(
        "ix_playback_sessions_stage_started",
        table_name="playback_sessions",
    )
    op.drop_table("playback_sessions")
