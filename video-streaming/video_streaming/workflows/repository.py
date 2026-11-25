"""SQLAlchemy-backed source of truth for jobs, workflows and artifacts."""

import hashlib
import hmac
import json
import math
import re
import secrets
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    JSON, BigInteger, CheckConstraint, Column, DateTime, Float, ForeignKey,
    ForeignKeyConstraint, Index, Integer, Numeric, String, Text,
    UniqueConstraint, create_engine, and_, or_,
    select, func,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker

from .execution_plan import (
    execution_plan_sha256,
    validate_execution_plan,
)
from .artifacts import attempt_scoped_object_key
from .domain import (
    AttemptLeaseLost,
    AttemptState,
    IdempotencyConflict,
    InvalidStateTransition,
    JobState,
    StageState,
    StageAlreadyRunning,
    WorkflowState,
    ALLOWED_STAGE_TRANSITIONS,
)


Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


def new_id():
    return str(uuid.uuid4())


class JobRecord(Base):
    __tablename__ = "workflow_jobs"
    __table_args__ = (
        CheckConstraint(
            "(owner_tenant_id IS NULL AND owner_issuer IS NULL AND "
            "owner_subject IS NULL) OR "
            "(owner_tenant_id IS NOT NULL AND owner_issuer IS NOT NULL AND "
            "owner_subject IS NOT NULL)",
            name="ck_workflow_job_owner_identity_complete",
        ),
        UniqueConstraint(
            "id", "owner_tenant_id", "owner_issuer", "owner_subject",
            name="uq_workflow_jobs_secret_owner",
        ),
        Index(
            "ix_workflow_jobs_owner_tenant_created",
            "owner_tenant_id",
            "created_at",
        ),
    )

    id = Column(String(36), primary_key=True)
    idempotency_key = Column(String(255), nullable=False, unique=True)
    request_hash = Column(String(64), nullable=False)
    reference_id = Column(String(255), nullable=False, default="")
    owner_tenant_id = Column(String(255), nullable=True)
    owner_issuer = Column(String(512), nullable=True)
    owner_subject = Column(String(255), nullable=True)
    state = Column(String(24), nullable=False, default=JobState.QUEUED.value)
    metadata_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    version = Column(Integer, nullable=False, default=1)


class TenantJobQuotaPolicyRecord(Base):
    """Provisioned per-tenant cap for active durable and legacy Jobs."""

    __tablename__ = "workflow_tenant_job_quota_policies"
    __table_args__ = (
        CheckConstraint(
            "max_active_jobs >= 0",
            name="ck_workflow_tenant_job_quota_nonnegative",
        ),
    )

    tenant_id = Column(String(255), primary_key=True)
    max_active_jobs = Column(Integer, nullable=False)
    policy_version = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class TenantJobQuotaAuditRecord(Base):
    """Append-only operator audit trail for tenant admission policy changes."""

    __tablename__ = "workflow_tenant_job_quota_audit"
    __table_args__ = (
        CheckConstraint(
            "max_active_jobs >= 0",
            name="ck_workflow_tenant_job_quota_audit_nonnegative",
        ),
        CheckConstraint(
            "policy_version >= 1",
            name="ck_workflow_tenant_job_quota_audit_version",
        ),
        Index(
            "ix_workflow_tenant_job_quota_audit_tenant_created",
            "tenant_id", "created_at",
        ),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(255), nullable=False)
    operator_issuer = Column(String(512), nullable=False)
    operator_subject = Column(String(255), nullable=False)
    reason = Column(String(1000), nullable=False)
    previous_max_active_jobs = Column(Integer, nullable=True)
    max_active_jobs = Column(Integer, nullable=False)
    policy_version = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class JobSecretRecord(Base):
    """Encrypted callback material bound to the exact durable Job owner."""

    __tablename__ = "workflow_job_secrets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["job_id", "tenant_id", "issuer", "subject"],
            [
                "workflow_jobs.id",
                "workflow_jobs.owner_tenant_id",
                "workflow_jobs.owner_issuer",
                "workflow_jobs.owner_subject",
            ],
            ondelete="CASCADE",
            name="fk_workflow_job_secrets_owner",
        ),
        UniqueConstraint(
            "job_id", "purpose", "version",
            name="uq_workflow_job_secret_purpose_version",
        ),
        CheckConstraint("version >= 1", name="ck_workflow_job_secret_version"),
        CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED', 'DELETED')",
            name="ck_workflow_job_secret_status",
        ),
        Index(
            "ix_workflow_job_secrets_scope",
            "job_id", "tenant_id", "purpose", "status",
        ),
    )

    secret_id = Column(String(64), primary_key=True)
    job_id = Column(String(36), nullable=False)
    tenant_id = Column(String(255), nullable=False)
    issuer = Column(String(512), nullable=False)
    subject = Column(String(255), nullable=False)
    purpose = Column(String(128), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    algorithm = Column(String(32), nullable=False)
    nonce = Column(String(64), nullable=False)
    ciphertext = Column(Text, nullable=False)
    wrapped_dek = Column(Text, nullable=False)
    kms_key_id = Column(String(512), nullable=False)
    status = Column(String(16), nullable=False, default="ACTIVE")
    created_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class WorkflowRecord(Base):
    __tablename__ = "workflow_workflows"
    __table_args__ = (
        CheckConstraint(
            "(execution_plan_json IS NULL AND "
            "execution_plan_fingerprint IS NULL AND "
            "execution_plan_schema_version IS NULL AND workflow_version IS NULL) "
            "OR (execution_plan_json IS NOT NULL AND "
            "execution_plan_fingerprint IS NOT NULL AND "
            "execution_plan_schema_version IS NOT NULL AND workflow_version IS NOT NULL)",
            name="ck_workflow_execution_plan_snapshot_complete",
        ),
    )

    id = Column(String(36), primary_key=True)
    job_id = Column(
        String(36), ForeignKey("workflow_jobs.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    name = Column(String(128), nullable=False)
    state = Column(String(24), nullable=False, default=WorkflowState.QUEUED.value)
    execution_backend = Column(String(64), nullable=True)
    workflow_version = Column(String(64), nullable=True)
    execution_plan_schema_version = Column(Integer, nullable=True)
    execution_plan_fingerprint = Column(String(64), nullable=True)
    execution_plan_json = Column(JSON(none_as_null=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class StageRecord(Base):
    __tablename__ = "workflow_stages"
    __table_args__ = (
        UniqueConstraint("workflow_id", "stage_key", name="uq_workflow_stage_key"),
        UniqueConstraint("id", "workflow_id", name="uq_workflow_stage_id_workflow"),
        Index("ix_workflow_stages_status_updated", "status", "updated_at"),
    )

    id = Column(String(36), primary_key=True)
    workflow_id = Column(
        String(36), ForeignKey("workflow_workflows.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage_key = Column(String(128), nullable=False)
    operation = Column(String(128), nullable=False)
    status = Column(String(24), nullable=False, default=StageState.PENDING.value)
    ordinal = Column(Integer, nullable=False, default=0)
    input_contract_json = Column(JSON, nullable=False, default=dict)
    output_contract_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    version = Column(Integer, nullable=False, default=1)


class StageDependencyRecord(Base):
    """Normalized, same-workflow dependency edge between durable stages."""

    __tablename__ = "workflow_stage_dependencies"
    __table_args__ = (
        CheckConstraint(
            "stage_id <> depends_on_stage_id",
            name="ck_workflow_stage_dependency_not_self",
        ),
        ForeignKeyConstraint(
            ["stage_id", "workflow_id"],
            ["workflow_stages.id", "workflow_stages.workflow_id"],
            ondelete="CASCADE",
            name="fk_workflow_stage_dependency_stage",
        ),
        ForeignKeyConstraint(
            ["depends_on_stage_id", "workflow_id"],
            ["workflow_stages.id", "workflow_stages.workflow_id"],
            ondelete="CASCADE",
            name="fk_workflow_stage_dependency_parent",
        ),
        Index(
            "ix_workflow_stage_dependencies_workflow",
            "workflow_id", "stage_id",
        ),
    )

    stage_id = Column(String(36), primary_key=True)
    depends_on_stage_id = Column(String(36), primary_key=True)
    workflow_id = Column(String(36), nullable=False)


class AttemptRecord(Base):
    __tablename__ = "workflow_attempts"
    __table_args__ = (
        UniqueConstraint("stage_id", "attempt_number", name="uq_stage_attempt_number"),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_token_hash IS NULL AND "
            "lease_expires_at IS NULL AND heartbeat_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_token_hash IS NOT NULL AND "
            "lease_expires_at IS NOT NULL AND heartbeat_at IS NOT NULL)",
            name="ck_workflow_attempt_lease_complete",
        ),
        Index("ix_workflow_attempts_state_started", "state", "started_at"),
        Index("ix_workflow_attempts_lease_expiry", "state", "lease_expires_at"),
    )

    id = Column(String(36), primary_key=True)
    stage_id = Column(
        String(36), ForeignKey("workflow_stages.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number = Column(Integer, nullable=False)
    state = Column(String(24), nullable=False, default=AttemptState.RUNNING.value)
    execution_backend = Column(String(64), nullable=True)
    execution_ref = Column(String(255), nullable=True)
    lease_owner = Column(String(255), nullable=True)
    lease_token_hash = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(BigInteger, nullable=True)
    cpu_seconds = Column(Float, nullable=True)
    peak_memory_bytes = Column(BigInteger, nullable=True)
    input_bytes = Column(BigInteger, nullable=True)
    output_bytes = Column(BigInteger, nullable=True)
    artifact_count = Column(Integer, nullable=False, default=0)
    estimated_cost = Column(Numeric(18, 8), nullable=True)
    cost_currency = Column(String(8), nullable=True)
    pricing_model_id = Column(String(128), nullable=True)
    pricing_model_version = Column(String(64), nullable=True)
    pricing_snapshot_json = Column(JSON, nullable=True)
    metrics_json = Column(JSON, nullable=False, default=dict)
    error_code = Column(String(128), nullable=True)
    error_message = Column(Text, nullable=True)


class ArtifactRecord(Base):
    __tablename__ = "workflow_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "stage_id", "attempt_id", "logical_name",
            name="uq_stage_attempt_artifact_name",
        ),
        Index("ix_workflow_artifacts_job_type", "job_id", "artifact_type"),
    )

    id = Column(String(36), primary_key=True)
    job_id = Column(
        String(36), ForeignKey("workflow_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage_id = Column(
        String(36), ForeignKey("workflow_stages.id", ondelete="SET NULL"),
        nullable=True,
    )
    attempt_id = Column(
        String(36), ForeignKey("workflow_attempts.id", ondelete="SET NULL"),
        nullable=True,
    )
    artifact_type = Column(String(64), nullable=False)
    logical_name = Column(String(255), nullable=False)
    uri = Column(Text, nullable=True)
    media_type = Column(String(128), nullable=True)
    size_bytes = Column(BigInteger, nullable=True)
    checksum_sha256 = Column(String(64), nullable=True)
    status = Column(String(24), nullable=False, default="AVAILABLE")
    metadata_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class WorkflowEventRecord(Base):
    __tablename__ = "workflow_events"
    __table_args__ = (
        Index("ix_workflow_events_job_time", "job_id", "occurred_at"),
        Index("ix_workflow_events_stage_time", "stage_id", "occurred_at"),
    )

    id = Column(String(36), primary_key=True)
    job_id = Column(
        String(36), ForeignKey("workflow_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_id = Column(
        String(36), ForeignKey("workflow_workflows.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage_id = Column(
        String(36), ForeignKey("workflow_stages.id", ondelete="SET NULL"),
        nullable=True,
    )
    attempt_id = Column(
        String(36), ForeignKey("workflow_attempts.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type = Column(String(80), nullable=False)
    from_state = Column(String(24), nullable=True)
    to_state = Column(String(24), nullable=True)
    occurred_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    payload_json = Column(JSON, nullable=False, default=dict)


class OutboxRecord(Base):
    """Durable, leased reference to an immutable workflow execution plan."""

    __tablename__ = "workflow_outbox"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_workflow_outbox_dedupe_key"),
        CheckConstraint(
            "state IN ('PENDING', 'PROCESSING', 'PUBLISHED', 'DEAD')",
            name="ck_workflow_outbox_state",
        ),
        CheckConstraint(
            "schema_version >= 1",
            name="ck_workflow_outbox_schema_version",
        ),
        CheckConstraint(
            "(state = 'PROCESSING' AND locked_by IS NOT NULL AND "
            "lock_token_hash IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(state <> 'PROCESSING' AND locked_by IS NULL AND "
            "lock_token_hash IS NULL AND lease_expires_at IS NULL)",
            name="ck_workflow_outbox_lease_complete",
        ),
        CheckConstraint("attempts >= 0", name="ck_workflow_outbox_attempts"),
        Index(
            "ix_workflow_outbox_ready",
            "state", "available_at", "lease_expires_at",
        ),
        Index("ix_workflow_outbox_job_created", "job_id", "created_at"),
    )

    id = Column(String(36), primary_key=True)
    job_id = Column(
        String(36), ForeignKey("workflow_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_id = Column(
        String(36), ForeignKey("workflow_workflows.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type = Column(String(80), nullable=False)
    schema_version = Column(Integer, nullable=False, default=1)
    plan_version = Column(String(64), nullable=False)
    plan_fingerprint = Column(String(64), nullable=False)
    dedupe_key = Column(String(255), nullable=False)
    payload_json = Column(JSON, nullable=False)
    state = Column(String(16), nullable=False, default="PENDING")
    attempts = Column(Integer, nullable=False, default=0)
    available_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    locked_by = Column(String(255), nullable=True)
    lock_token_hash = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    published_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(String(128), nullable=True)


class PlaybackSessionRecord(Base):
    __tablename__ = "playback_sessions"
    __table_args__ = (
        Index("ix_playback_sessions_stage_started", "stage_id", "started_at"),
        Index("ix_playback_sessions_job_started", "job_id", "started_at"),
    )

    id = Column(String(36), primary_key=True)
    job_id = Column(
        String(36), ForeignKey("workflow_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage_id = Column(
        String(36), ForeignKey("workflow_stages.id", ondelete="CASCADE"),
        nullable=False,
    )
    manifest_artifact_id = Column(
        String(36), ForeignKey("workflow_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    token_hash = Column(String(64), nullable=False)
    client_version = Column(String(64), nullable=False, default="")
    status = Column(String(16), nullable=False, default="ACTIVE")
    started_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_event_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    playback_started_at = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    startup_ms = Column(BigInteger, nullable=True)
    buffering_started_at = Column(DateTime(timezone=True), nullable=True)
    rebuffer_count = Column(Integer, nullable=False, default=0)
    total_buffering_ms = Column(BigInteger, nullable=False, default=0)
    decoder_error_count = Column(Integer, nullable=False, default=0)


class PlaybackEventRecord(Base):
    __tablename__ = "playback_events"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "event_id", name="uq_playback_session_event_id"
        ),
        Index("ix_playback_events_session_time", "session_id", "received_at"),
    )

    id = Column(String(36), primary_key=True)
    session_id = Column(
        String(36), ForeignKey("playback_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id = Column(String(128), nullable=False)
    event_type = Column(String(32), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    duration_ms = Column(BigInteger, nullable=True)
    startup_ms = Column(BigInteger, nullable=True)
    error_code = Column(String(128), nullable=True)
    metadata_json = Column(JSON, nullable=False, default=dict)


def _record_event(
        session,
        job_id,
        workflow_id,
        event_type,
        from_state=None,
        to_state=None,
        stage_id=None,
        attempt_id=None,
        payload=None):
    """Append a small immutable event inside the caller's state transaction."""
    session.add(WorkflowEventRecord(
        id=new_id(),
        job_id=job_id,
        workflow_id=workflow_id,
        stage_id=stage_id,
        attempt_id=attempt_id,
        event_type=event_type,
        from_state=from_state,
        to_state=to_state,
        occurred_at=utcnow(),
        payload_json=payload or {},
    ))



def _durable_retry_delay(workflow, stage_key, attempt_number):
    """Return the persisted node policy delay for the next retry."""
    plan = workflow.execution_plan_json if workflow is not None else None
    if not isinstance(plan, dict):
        return 0.0
    node = next((
        item for item in plan.get("nodes", [])
        if item.get("stage_key") == stage_key
    ), None)
    policy = node.get("retry_policy", {}) if node else {}
    try:
        initial = float(policy["initial_delay_seconds"])
        maximum = float(policy["max_delay_seconds"])
        backoff = policy["backoff"]
    except (KeyError, TypeError, ValueError):
        return 0.0
    if backoff == "fixed":
        return min(initial, maximum)
    if backoff == "exponential":
        return min(maximum, initial * (2 ** max(0, attempt_number - 1)))
    return 0.0


def _durable_retry_not_before(workflow, stage_key, attempt):
    if (
            workflow is None
            or workflow.execution_backend != "durable_vod_v1"
            or attempt is None
            or attempt.finished_at is None):
        return None
    finished_at = attempt.finished_at
    if finished_at.tzinfo is None or finished_at.utcoffset() is None:
        finished_at = finished_at.replace(tzinfo=timezone.utc)
    delay = _durable_retry_delay(workflow, stage_key, attempt.attempt_number)
    return finished_at + timedelta(seconds=delay)


def _enqueue_durable_dispatch(
        session, workflow, job_id, attempt_id, stage_id, available_at,
        reason, event_type):
    """Persist a retry/continuation wake-up with the state transition."""
    if workflow.execution_backend != "durable_vod_v1":
        return None
    if not (
            workflow.workflow_version
            and workflow.execution_plan_fingerprint
            and workflow.execution_plan_schema_version):
        raise InvalidStateTransition(
            "Durable dispatch requires an immutable execution-plan identity"
        )
    outbox_id = new_id()
    session.add(OutboxRecord(
        id=outbox_id,
        job_id=job_id,
        workflow_id=workflow.id,
        event_type="WORKFLOW_PLAN_DISPATCH",
        schema_version=workflow.execution_plan_schema_version,
        plan_version=workflow.workflow_version,
        plan_fingerprint=workflow.execution_plan_fingerprint,
        dedupe_key="workflow.{}:{}:{}".format(
            reason, workflow.id, attempt_id
        ),
        payload_json={
            "schema_version": workflow.execution_plan_schema_version,
            "job_id": job_id,
            "workflow_version": workflow.workflow_version,
            "plan_fingerprint": workflow.execution_plan_fingerprint,
        },
        state="PENDING",
        attempts=0,
        available_at=available_at,
        created_at=utcnow(),
        updated_at=utcnow(),
    ))
    _record_event(
        session,
        job_id,
        workflow.id,
        event_type,
        stage_id=stage_id,
        attempt_id=attempt_id,
        payload={"outbox_id": outbox_id, "available_at": available_at.isoformat()},
    )
    return outbox_id


class WorkflowRepository:
    """Persistent repository; Celery IDs are optional execution metadata only."""

    def __init__(
            self,
            database_url,
            create_schema=False,
            engine=None,
            pool_size=5,
            max_overflow=5):
        if engine is None and not database_url:
            raise ValueError("WORKFLOW_DATABASE_URL is required")
        if engine is None:
            options = {
                "pool_pre_ping": True,
                "future": True,
                "pool_size": pool_size,
                "max_overflow": max_overflow,
            }
            if database_url.startswith("sqlite:"):
                options["connect_args"] = {"check_same_thread": False}
                options.pop("pool_size", None)
                options.pop("max_overflow", None)
            engine = create_engine(database_url, **options)
        self.engine = engine
        self._session = sessionmaker(
            bind=self.engine, expire_on_commit=False, future=True
        )
        if create_schema:
            Base.metadata.create_all(self.engine)

    def close(self):
        self.engine.dispose()

    def provision_tenant_job_quota_policy(
            self, tenant_id, max_active_jobs, operator_principal, reason):
        """Atomically change a tenant quota and append its operator audit event.

        Authorization to invoke this administrative repository operation must
        be enforced by the authenticated admin service boundary. The audit
        identity is always derived from a trusted RequestPrincipal.
        """
        from video_streaming.security import require_scope

        require_scope(operator_principal, "video.tenant.quota.admin")
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        tenant_id = tenant_id.strip()
        if len(tenant_id) > 255:
            raise ValueError("tenant_id must contain at most 255 characters")
        if (
                isinstance(max_active_jobs, bool)
                or not isinstance(max_active_jobs, int)
                or max_active_jobs < 0):
            raise ValueError("max_active_jobs must be a nonnegative integer")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("an operator reason is required")
        reason = reason.strip()
        if len(reason) > 1000:
            raise ValueError("reason must contain at most 1000 characters")

        with self._session.begin() as session:
            policy = session.execute(
                select(TenantJobQuotaPolicyRecord)
                .where(TenantJobQuotaPolicyRecord.tenant_id == tenant_id)
                .with_for_update()
            ).scalar_one_or_none()
            previous_limit = None if policy is None else policy.max_active_jobs
            next_version = 1 if policy is None else policy.policy_version + 1
            if policy is None:
                session.add(TenantJobQuotaPolicyRecord(
                    tenant_id=tenant_id,
                    max_active_jobs=max_active_jobs,
                    policy_version=next_version,
                    updated_at=utcnow(),
                ))
            else:
                policy.max_active_jobs = max_active_jobs
                policy.policy_version = next_version
                policy.updated_at = utcnow()
            session.add(TenantJobQuotaAuditRecord(
                id=new_id(),
                tenant_id=tenant_id,
                operator_issuer=operator_principal.issuer,
                operator_subject=operator_principal.subject,
                reason=reason,
                previous_max_active_jobs=previous_limit,
                max_active_jobs=max_active_jobs,
                policy_version=next_version,
                created_at=utcnow(),
            ))
        return {"tenant_id": tenant_id, "max_active_jobs": max_active_jobs,
                "policy_version": next_version}

    def list_tenant_job_quota_policies(self, operator_principal, limit=1000):
        """Return a bounded policy inventory to an explicitly scoped operator."""
        from video_streaming.security import require_scope
        require_scope(operator_principal, "video.tenant.quota.admin")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._session() as session:
            rows = session.execute(
                select(TenantJobQuotaPolicyRecord)
                .order_by(TenantJobQuotaPolicyRecord.tenant_id)
                .limit(limit)
            ).scalars().all()
            return [
                {
                    "tenant_id": row.tenant_id,
                    "max_active_jobs": row.max_active_jobs,
                    "policy_version": row.policy_version,
                    "updated_at": row.updated_at,
                }
                for row in rows
            ]

    def find_idempotent_job(
            self, idempotency_key, request_hash, owner_principal=None):
        """Return an existing accepted Job after enforcing idempotency ownership.

        CreateJob uses this read before generating a new random secret envelope,
        so a legitimate retry reuses the original Job and never rotates its
        encrypted callback secret or changes its plan fingerprint.
        """
        with self._session() as session:
            job = session.execute(
                select(JobRecord).where(
                    JobRecord.idempotency_key == idempotency_key
                )
            ).scalar_one_or_none()
            if job is None:
                return None
            if job.request_hash != request_hash:
                raise IdempotencyConflict(
                    "Idempotency key was already used with a different request"
                )
            if job.owner_tenant_id is not None and owner_principal is None:
                from video_streaming.security import AuthorizationError
                raise AuthorizationError("Resource not found or access denied")
            if owner_principal is not None and (
                    job.owner_tenant_id != owner_principal.tenant_id
                    or job.owner_issuer != owner_principal.issuer
                    or job.owner_subject != owner_principal.subject):
                from video_streaming.security import AuthorizationError
                raise AuthorizationError("Resource not found or access denied")
            return job.id

    @staticmethod
    def _rebind_webhook_secret_references(session, job_id, plan):
        """Rebind plan references to accepted Job secrets without rotating them."""
        if plan is None:
            return
        accepted_by_purpose = {}
        now = utcnow()
        for node in plan["nodes"]:
            parameters = node["parameters"]
            for parameter, purpose in (
                ("webhook_secret_ref", "webhook.callback"),
                ("webhook_signing_secret_ref", "webhook.signing"),
            ):
                if parameter not in parameters:
                    continue
                if purpose not in accepted_by_purpose:
                    accepted_by_purpose[purpose] = session.execute(
                        select(JobSecretRecord).where(
                            JobSecretRecord.job_id == job_id,
                            JobSecretRecord.purpose == purpose,
                            JobSecretRecord.status == "ACTIVE",
                            JobSecretRecord.deleted_at.is_(None),
                            (JobSecretRecord.expires_at.is_(None)
                             | (JobSecretRecord.expires_at > now)),
                        )
                    ).scalar_one_or_none()
                accepted_secret = accepted_by_purpose[purpose]
                if accepted_secret is None:
                    raise IdempotencyConflict(
                        "Idempotent plan references a missing active {} secret".format(
                            purpose
                        )
                    )
                parameters[parameter] = accepted_secret.secret_id

    def create_job(
            self,
            job_id,
            idempotency_key,
            request_hash,
            reference_id="",
            workflow_name="video.vod",
            stage_specs=None,
            metadata=None,
            execution_plan=None,
            owner_principal=None,
            secret_envelopes=None,
            enqueue_dispatch=False,
            outbox_dedupe_key=None,
            execution_backend=None,
            quota_enforcement_enabled=False):
        """Atomically create job, workflow, stages and optional owner/plan.

        owner_principal must be a trusted RequestPrincipal established by the
        authentication boundary. Secret envelopes require that owner and are
        inserted in this same transaction as the Job, workflow and stages.
        Omitting owner_principal preserves legacy-unowned compatibility.
        """
        if owner_principal is not None:
            from video_streaming.security import RequestPrincipal, AuthorizationError

            if not isinstance(owner_principal, RequestPrincipal):
                raise TypeError("owner_principal must be a RequestPrincipal")
            if owner_principal.is_expired():
                raise AuthorizationError("Resource not found or access denied")

        from video_streaming.security.secret_store import SecretEnvelope

        secret_envelopes = list(secret_envelopes or [])
        if secret_envelopes and owner_principal is None:
            raise ValueError("secret envelopes require an authenticated Job owner")
        seen_purposes = set()
        seen_secret_ids = set()
        for envelope in secret_envelopes:
            if not isinstance(envelope, SecretEnvelope):
                raise TypeError("secret_envelopes must contain SecretEnvelope values")
            context = envelope.context
            if (
                    context.tenant_id != owner_principal.tenant_id
                    or context.issuer != owner_principal.issuer
                    or context.subject != owner_principal.subject):
                raise ValueError("secret envelope scope must match the Job owner")
            if context.purpose not in ("webhook.callback", "webhook.signing"):
                raise ValueError("unsupported Job secret purpose")
            if context.purpose in seen_purposes:
                raise ValueError("only one initial secret per purpose is allowed")
            if envelope.secret_id in seen_secret_ids:
                raise ValueError("secret_id values must be unique within a Job")
            seen_secret_ids.add(envelope.secret_id)
            if envelope.expires_at is not None:
                expiry = datetime.fromisoformat(envelope.expires_at)
                if expiry.tzinfo is None or expiry.utcoffset() is None:
                    raise ValueError("secret expiry must be timezone-aware")
                if expiry <= utcnow():
                    raise ValueError("expired secret envelopes cannot be persisted")
            seen_purposes.add(context.purpose)

        plan = None
        plan_fingerprint = None
        if execution_plan is not None:
            plan = validate_execution_plan(execution_plan)
            if plan["workflow_name"] != workflow_name:
                raise ValueError("execution plan workflow_name must match workflow_name")
            plan_fingerprint = execution_plan_sha256(plan)
            plan_stage_keys = [node["stage_key"] for node in plan["nodes"]]
            if len(plan_stage_keys) != len(set(plan_stage_keys)):
                raise ValueError("execution plan stage_key values must be unique")
            plan_stage_specs = [{
                "stage_key": node["stage_key"],
                "operation": node["operation"],
                "ordinal": index,
                # The immutable plan is the only parameter source. Stage rows
                # are intentionally thin projections to avoid duplicating
                # secret references or request payloads into status snapshots.
                "input": {},
                "output": {},
            } for index, node in enumerate(plan["nodes"])]
            if stage_specs is None:
                stage_specs = plan_stage_specs
            else:
                supplied = list(stage_specs)
                expected = [
                    (item["stage_key"], item["operation"])
                    for item in plan_stage_specs
                ]
                actual = [
                    (item["stage_key"], item["operation"])
                    for item in supplied
                ]
                if actual != expected:
                    raise ValueError(
                        "stage_specs must match execution plan node order, keys and operations"
                    )
                # Discard caller-supplied projection payloads; persist only
                # the validated plan-derived stage identity and operation.
                stage_specs = plan_stage_specs
        stage_specs = list(stage_specs or [])
        if execution_backend is not None and (
                not isinstance(execution_backend, str)
                or not execution_backend.strip()
                or len(execution_backend) > 64):
            raise ValueError("execution_backend must contain 1 to 64 characters")
        if not isinstance(enqueue_dispatch, bool):
            raise TypeError("enqueue_dispatch must be a boolean")
        if not isinstance(quota_enforcement_enabled, bool):
            raise TypeError("quota_enforcement_enabled must be a boolean")
        if enqueue_dispatch and plan is None:
            raise ValueError(
                "dispatch outbox requires a persisted immutable execution plan"
            )
        if enqueue_dispatch:
            if outbox_dedupe_key is None:
                outbox_dedupe_key = "workflow.dispatch:{}".format(job_id)
            if (
                    not isinstance(outbox_dedupe_key, str)
                    or not outbox_dedupe_key.strip()
                    or len(outbox_dedupe_key) > 255):
                raise ValueError(
                    "outbox_dedupe_key must contain 1 to 255 characters"
                )
        try:
            with self._session.begin() as session:
                existing = session.execute(
                    select(JobRecord).where(
                        JobRecord.idempotency_key == idempotency_key
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise IdempotencyConflict(
                            "Idempotency key was already used with a different request"
                        )
                    if existing.owner_tenant_id is not None and owner_principal is None:
                        from video_streaming.security import AuthorizationError
                        raise AuthorizationError(
                            "Resource not found or access denied"
                        )
                    if owner_principal is not None and (
                            existing.owner_tenant_id != owner_principal.tenant_id
                            or existing.owner_issuer != owner_principal.issuer
                            or existing.owner_subject != owner_principal.subject):
                        from video_streaming.security import AuthorizationError
                        raise AuthorizationError(
                            "Resource not found or access denied"
                        )
                    existing_workflow = session.execute(
                        select(WorkflowRecord).where(
                            WorkflowRecord.job_id == existing.id
                        )
                    ).scalar_one()
                    replay_plan = None
                    if plan is not None:
                        replay_plan = deepcopy(plan)
                        replay_plan["job_id"] = existing.id
                        # Concurrent idempotent CreateJob requests may have
                        # generated different random envelope IDs before one
                        # transaction wins. Rebind the candidate plan to the
                        # already-accepted active envelope before comparing
                        # fingerprints; never rotate the persisted secret.
                        self._rebind_webhook_secret_references(
                            session, existing.id, replay_plan
                        )
                    replay_fingerprint = (
                        execution_plan_sha256(replay_plan)
                        if replay_plan is not None else None
                    )
                    if (
                            replay_fingerprint is not None
                            and existing_workflow.execution_plan_fingerprint
                            is not None
                            and existing_workflow.execution_plan_fingerprint
                            != replay_fingerprint):
                        raise IdempotencyConflict(
                            "Idempotency key resolves to a different execution plan"
                        )
                    # A replay must not rotate or replace accepted secrets.
                    # It may only refer back to an already-persisted purpose.
                    for envelope in secret_envelopes:
                        existing_secret = session.execute(
                            select(JobSecretRecord).where(
                                JobSecretRecord.job_id == existing.id,
                                JobSecretRecord.purpose == envelope.context.purpose,
                                JobSecretRecord.status == "ACTIVE",
                                JobSecretRecord.deleted_at.is_(None),
                                (JobSecretRecord.expires_at.is_(None)
                                 | (JobSecretRecord.expires_at > utcnow())),
                            )
                        ).scalar_one_or_none()
                        if existing_secret is None:
                            raise IdempotencyConflict(
                                "Idempotent replay cannot add or replace a Job secret"
                            )
                    # A legacy workflow without a plan remains legacy on replay;
                    # never mutate already-accepted work during a deployment.
                    return existing.id, False

                # A policy-row lock serializes admissions for this tenant.
                # Re-check idempotency after acquiring the lock: another
                # identical request may have committed while this transaction
                # waited, and a replay must not consume a second quota slot.
                if owner_principal is not None and quota_enforcement_enabled:
                    quota_policy = session.execute(
                        select(TenantJobQuotaPolicyRecord).where(
                            TenantJobQuotaPolicyRecord.tenant_id
                            == owner_principal.tenant_id
                        ).with_for_update()
                    ).scalar_one_or_none()
                    if quota_policy is None:
                        from video_streaming.workflows.domain import (
                            TenantQuotaPolicyMissing,
                        )
                        raise TenantQuotaPolicyMissing(
                            "Tenant quota policy is not provisioned"
                        )

                    concurrent_existing = session.execute(
                        select(JobRecord).where(
                            JobRecord.idempotency_key == idempotency_key
                        )
                    ).scalar_one_or_none()
                    if concurrent_existing is not None:
                        if concurrent_existing.request_hash != request_hash:
                            raise IdempotencyConflict(
                                "Idempotency key was already used with a different request"
                            )
                        if (
                                concurrent_existing.owner_tenant_id
                                != owner_principal.tenant_id
                                or concurrent_existing.owner_issuer
                                != owner_principal.issuer
                                or concurrent_existing.owner_subject
                                != owner_principal.subject):
                            from video_streaming.security import AuthorizationError
                            raise AuthorizationError(
                                "Resource not found or access denied"
                            )
                        concurrent_workflow = session.execute(
                            select(WorkflowRecord).where(
                                WorkflowRecord.job_id == concurrent_existing.id
                            )
                        ).scalar_one()
                        replay_plan = None
                        if plan is not None:
                            replay_plan = deepcopy(plan)
                            replay_plan["job_id"] = concurrent_existing.id
                            self._rebind_webhook_secret_references(
                                session, concurrent_existing.id, replay_plan
                            )
                        replay_fingerprint = (
                            execution_plan_sha256(replay_plan)
                            if replay_plan is not None else None
                        )
                        if (
                                replay_fingerprint is not None
                                and concurrent_workflow.execution_plan_fingerprint
                                is not None
                                and concurrent_workflow.execution_plan_fingerprint
                                != replay_fingerprint):
                            raise IdempotencyConflict(
                                "Idempotency key resolves to a different execution plan"
                            )
                        for envelope in secret_envelopes:
                            active_secret = session.execute(
                                select(JobSecretRecord).where(
                                    JobSecretRecord.job_id == concurrent_existing.id,
                                    JobSecretRecord.purpose == envelope.context.purpose,
                                    JobSecretRecord.status == "ACTIVE",
                                    JobSecretRecord.deleted_at.is_(None),
                                    (JobSecretRecord.expires_at.is_(None)
                                     | (JobSecretRecord.expires_at > utcnow())),
                                )
                            ).scalar_one_or_none()
                            if active_secret is None:
                                raise IdempotencyConflict(
                                    "Idempotent replay cannot add or replace a Job secret"
                                )
                        return concurrent_existing.id, False

                    active_jobs = session.execute(
                        select(func.count()).select_from(JobRecord).where(
                            JobRecord.owner_tenant_id == owner_principal.tenant_id,
                            JobRecord.state.in_([
                                JobState.QUEUED.value,
                                JobState.RUNNING.value,
                            ]),
                        )
                    ).scalar_one()
                    if active_jobs >= quota_policy.max_active_jobs:
                        from video_streaming.workflows.domain import (
                            TenantQuotaExceeded,
                        )
                        raise TenantQuotaExceeded(
                            "Tenant active Job quota has been reached"
                        )

                # Validate the candidate identity only after idempotency
                # lookup. Replays may carry a newly generated candidate Job ID;
                # the immutable plan is compared after rebinding it to the
                # already accepted Job ID above, while new Jobs must match.
                if plan is not None and plan["job_id"] != job_id:
                    raise ValueError("execution plan job_id must match job_id")
                if any(
                        envelope.context.job_id != job_id
                        for envelope in secret_envelopes):
                    raise ValueError("secret envelope job_id must match job_id")

                job = JobRecord(
                    id=job_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    reference_id=reference_id or "",
                    owner_tenant_id=(
                        owner_principal.tenant_id if owner_principal else None
                    ),
                    owner_issuer=(
                        owner_principal.issuer if owner_principal else None
                    ),
                    owner_subject=(
                        owner_principal.subject if owner_principal else None
                    ),
                    state=JobState.QUEUED.value,
                    metadata_json=metadata or {},
                )
                workflow_id = new_id()
                workflow = WorkflowRecord(
                    id=workflow_id,
                    job_id=job_id,
                    name=workflow_name,
                    state=WorkflowState.QUEUED.value,
                    execution_backend=execution_backend,
                    workflow_version=(plan["workflow_version"] if plan else None),
                    execution_plan_schema_version=(plan["schema_version"] if plan else None),
                    execution_plan_fingerprint=plan_fingerprint,
                    execution_plan_json=plan,
                )
                session.add_all([job, workflow])
                # Parent rows, immutable plan and all stage projections commit
                # together; no accepted plan can exist without its stages.
                session.flush()
                _record_event(
                    session,
                    job_id,
                    workflow_id,
                    "JOB_CREATED",
                    from_state=None,
                    to_state=JobState.QUEUED.value,
                    payload={
                        "workflow_name": workflow_name,
                        "execution_plan_schema_version": (
                            plan["schema_version"] if plan else None
                        ),
                        "execution_plan_fingerprint": plan_fingerprint,
                    },
                )
                stage_records = {}
                for index, spec in enumerate(stage_specs):
                    stage_record = StageRecord(
                        id=new_id(),
                        workflow_id=workflow_id,
                        stage_key=spec["stage_key"],
                        operation=spec["operation"],
                        ordinal=spec.get("ordinal", index),
                        input_contract_json=spec.get("input", {}),
                        output_contract_json=spec.get("output", {}),
                    )
                    stage_records[stage_record.stage_key] = stage_record
                    session.add(stage_record)
                session.flush()

                if plan is not None:
                    plan_nodes = {
                        node["node_id"]: node for node in plan["nodes"]
                    }
                    for node in plan["nodes"]:
                        stage_record = stage_records[node["stage_key"]]
                        for dependency_node_id in node["depends_on"]:
                            dependency_node = plan_nodes[dependency_node_id]
                            dependency_stage = stage_records[
                                dependency_node["stage_key"]
                            ]
                            session.add(StageDependencyRecord(
                                workflow_id=workflow_id,
                                stage_id=stage_record.id,
                                depends_on_stage_id=dependency_stage.id,
                            ))

                for envelope in secret_envelopes:
                    context = envelope.context
                    session.add(JobSecretRecord(
                        secret_id=envelope.secret_id,
                        job_id=context.job_id,
                        tenant_id=context.tenant_id,
                        issuer=context.issuer,
                        subject=context.subject,
                        purpose=context.purpose,
                        version=1,
                        algorithm=envelope.algorithm,
                        nonce=envelope.nonce,
                        ciphertext=envelope.ciphertext,
                        wrapped_dek=envelope.wrapped_dek,
                        kms_key_id=envelope.kms_key_id,
                        status="ACTIVE",
                        created_at=datetime.fromisoformat(envelope.created_at),
                        expires_at=(
                            datetime.fromisoformat(envelope.expires_at)
                            if envelope.expires_at else None
                        ),
                    ))
                    _record_event(
                        session,
                        job_id,
                        workflow_id,
                        "JOB_SECRET_CREATED",
                        payload={
                            "secret_id": envelope.secret_id,
                            "purpose": context.purpose,
                            "version": 1,
                            "algorithm": envelope.algorithm,
                            "kms_key_id": envelope.kms_key_id,
                        },
                    )
                if enqueue_dispatch:
                    session.add(OutboxRecord(
                        id=new_id(),
                        job_id=job_id,
                        workflow_id=workflow_id,
                        event_type="WORKFLOW_PLAN_DISPATCH",
                        schema_version=1,
                        plan_version=plan["workflow_version"],
                        plan_fingerprint=plan_fingerprint,
                        dedupe_key=outbox_dedupe_key,
                        payload_json={
                            "schema_version": 1,
                            "job_id": job_id,
                            "workflow_version": plan["workflow_version"],
                            "plan_fingerprint": plan_fingerprint,
                        },
                        state="PENDING",
                        attempts=0,
                        available_at=utcnow(),
                    ))
            return job_id, True
        except IntegrityError:
            # Concurrent duplicate submission: resolve through the unique
            # idempotency key, then compare the canonical request fingerprint.
            with self._session() as session:
                existing = session.execute(
                    select(JobRecord).where(
                        JobRecord.idempotency_key == idempotency_key
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise
                if existing.request_hash != request_hash:
                    raise IdempotencyConflict(
                        "Idempotency key was already used with a different request"
                    )
                if existing.owner_tenant_id is not None and owner_principal is None:
                    from video_streaming.security import AuthorizationError
                    raise AuthorizationError(
                        "Resource not found or access denied"
                    )
                if owner_principal is not None and (
                        existing.owner_tenant_id != owner_principal.tenant_id
                        or existing.owner_issuer != owner_principal.issuer
                        or existing.owner_subject != owner_principal.subject):
                    from video_streaming.security import AuthorizationError
                    raise AuthorizationError(
                        "Resource not found or access denied"
                    )
                existing_workflow = session.execute(
                    select(WorkflowRecord).where(
                        WorkflowRecord.job_id == existing.id
                    )
                ).scalar_one()
                replay_plan = None
                if plan is not None:
                    replay_plan = deepcopy(plan)
                    replay_plan["job_id"] = existing.id
                    self._rebind_webhook_secret_references(
                        session, existing.id, replay_plan
                    )
                replay_fingerprint = (
                    execution_plan_sha256(replay_plan)
                    if replay_plan is not None else None
                )
                if (
                        replay_fingerprint is not None
                        and existing_workflow.execution_plan_fingerprint is not None
                        and existing_workflow.execution_plan_fingerprint != replay_fingerprint):
                    raise IdempotencyConflict(
                        "Idempotency key resolves to a different execution plan"
                    )
                for envelope in secret_envelopes:
                    existing_secret = session.execute(
                        select(JobSecretRecord).where(
                            JobSecretRecord.job_id == existing.id,
                            JobSecretRecord.purpose == envelope.context.purpose,
                            JobSecretRecord.status == "ACTIVE",
                            JobSecretRecord.deleted_at.is_(None),
                            (JobSecretRecord.expires_at.is_(None)
                             | (JobSecretRecord.expires_at > utcnow())),
                        )
                    ).scalar_one_or_none()
                    if existing_secret is None:
                        raise IdempotencyConflict(
                            "Concurrent replay did not persist the expected Job secret"
                        )
                return existing.id, False

    def claim_outbox_batch(
            self, owner, lease_ttl_seconds=30, limit=25, now=None,
            max_attempts=20):
        """Claim due outbox rows with SKIP LOCKED and a one-time lease token.

        Expired claims consume the same bounded delivery budget as explicit
        failures. Once the budget is exhausted, the row is dead-lettered
        instead of being reclaimed forever after repeated relay crashes.
        """
        if not isinstance(owner, str) or not owner.strip() or len(owner) > 255:
            raise ValueError("owner must contain 1 to 255 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if (
                isinstance(max_attempts, bool)
                or not isinstance(max_attempts, int)
                or not 1 <= max_attempts <= 100):
            raise ValueError("max_attempts must be between 1 and 100")
        if (
                isinstance(lease_ttl_seconds, bool)
                or not isinstance(lease_ttl_seconds, (int, float))
                or not math.isfinite(float(lease_ttl_seconds))
                or not 1 <= lease_ttl_seconds <= 3600):
            raise ValueError("lease_ttl_seconds must be between 1 and 3600")
        now = now or utcnow()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        claimed = []
        with self._session.begin() as session:
            rows = session.execute(
                select(OutboxRecord).where(
                    OutboxRecord.available_at <= now,
                    or_(
                        OutboxRecord.state == "PENDING",
                        and_(
                            OutboxRecord.state == "PROCESSING",
                            OutboxRecord.lease_expires_at <= now,
                        ),
                    ),
                ).order_by(
                    OutboxRecord.available_at,
                    OutboxRecord.created_at,
                    OutboxRecord.id,
                ).limit(limit).with_for_update(skip_locked=True)
            ).scalars().all()
            for row in rows:
                # A relay can die after claiming but before it reports a
                # publish failure. Expiry must therefore consume the bounded
                # attempt budget too; otherwise crash loops never dead-letter.
                if (
                        row.state == "PROCESSING"
                        and row.attempts >= max_attempts):
                    row.state = "DEAD"
                    row.locked_by = None
                    row.lock_token_hash = None
                    row.lease_expires_at = None
                    row.last_error = "lease_expired_retry_limit"
                    row.updated_at = now
                    _record_event(
                        session,
                        row.job_id,
                        row.workflow_id,
                        "OUTBOX_DEAD_LETTERED",
                        payload={
                            "outbox_id": row.id,
                            "attempt": row.attempts,
                            "error_code": row.last_error,
                            "reason": "lease_expired",
                        },
                    )
                    continue
                token = secrets.token_urlsafe(32)
                row.state = "PROCESSING"
                row.locked_by = owner.strip()
                row.lock_token_hash = hashlib.sha256(
                    token.encode("utf-8")
                ).hexdigest()
                row.lease_expires_at = now + timedelta(
                    seconds=float(lease_ttl_seconds)
                )
                row.attempts += 1
                row.updated_at = now
                _record_event(
                    session,
                    row.job_id,
                    row.workflow_id,
                    "OUTBOX_CLAIMED",
                    payload={
                        "outbox_id": row.id,
                        "attempt": row.attempts,
                        "lease_expires_at": row.lease_expires_at.isoformat(),
                    },
                )
                claimed.append({
                    "id": row.id,
                    "job_id": row.job_id,
                    "workflow_id": row.workflow_id,
                    "event_type": row.event_type,
                    "schema_version": row.schema_version,
                    "plan_version": row.plan_version,
                    "plan_fingerprint": row.plan_fingerprint,
                    "payload": row.payload_json,
                    "attempts": row.attempts,
                    "lease_token": token,
                    "lease_expires_at": row.lease_expires_at.isoformat(),
                })
        return claimed

    def mark_outbox_published(self, outbox_id, lease_token, now=None):
        """Acknowledge only while the caller still owns an unexpired lease."""
        now = now or utcnow()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        digest = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
        with self._session.begin() as session:
            row = session.execute(
                select(OutboxRecord).where(
                    OutboxRecord.id == outbox_id
                ).with_for_update()
            ).scalar_one_or_none()
            lease_expires_at = (
                row.lease_expires_at if row is not None else None
            )
            if (
                    lease_expires_at is not None
                    and (
                        lease_expires_at.tzinfo is None
                        or lease_expires_at.utcoffset() is None
                    )):
                lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
            if (
                    row is None
                    or row.state != "PROCESSING"
                    or lease_expires_at is None
                    or lease_expires_at <= now
                    or not hmac.compare_digest(row.lock_token_hash or "", digest)):
                return False
            row.state = "PUBLISHED"
            row.published_at = now
            row.updated_at = now
            row.locked_by = None
            row.lock_token_hash = None
            row.lease_expires_at = None
            row.last_error = None
            _record_event(
                session,
                row.job_id,
                row.workflow_id,
                "OUTBOX_PUBLISHED",
                payload={"outbox_id": row.id, "attempt": row.attempts},
            )
            return True

    def retry_outbox(
            self, outbox_id, lease_token, error_code,
            delay_seconds, max_attempts=20, now=None):
        """Release a failed delivery for bounded exponential retry or dead-letter."""
        if (
                isinstance(delay_seconds, bool)
                or not isinstance(delay_seconds, (int, float))
                or not math.isfinite(float(delay_seconds))
                or delay_seconds < 0
                or delay_seconds > 86400):
            raise ValueError("delay_seconds must be between 0 and 86400")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        now = now or utcnow()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        digest = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
        with self._session.begin() as session:
            row = session.execute(
                select(OutboxRecord).where(
                    OutboxRecord.id == outbox_id
                ).with_for_update()
            ).scalar_one_or_none()
            lease_expires_at = (
                row.lease_expires_at if row is not None else None
            )
            if (
                    lease_expires_at is not None
                    and (
                        lease_expires_at.tzinfo is None
                        or lease_expires_at.utcoffset() is None
                    )):
                lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
            if (
                    row is None
                    or row.state != "PROCESSING"
                    or lease_expires_at is None
                    or lease_expires_at <= now
                    or not hmac.compare_digest(row.lock_token_hash or "", digest)):
                return False
            row.state = "DEAD" if row.attempts >= max_attempts else "PENDING"
            row.available_at = now + timedelta(seconds=float(delay_seconds))
            row.updated_at = now
            row.last_error = re.sub(
                r"[^A-Za-z0-9_.-]", "_", str(error_code or "dispatch_error")
            )[:128]
            row.locked_by = None
            row.lock_token_hash = None
            row.lease_expires_at = None
            _record_event(
                session,
                row.job_id,
                row.workflow_id,
                (
                    "OUTBOX_RETRY_SCHEDULED"
                    if row.state == "PENDING"
                    else "OUTBOX_DEAD_LETTERED"
                ),
                payload={
                    "outbox_id": row.id,
                    "attempt": row.attempts,
                    "error_code": row.last_error,
                    "available_at": row.available_at.isoformat(),
                },
            )
            return row.state == "PENDING"

    def list_outbox(self, state="DEAD", limit=100):
        """List bounded non-secret relay records for operator inspection."""
        if state not in ("PENDING", "PROCESSING", "PUBLISHED", "DEAD"):
            raise ValueError("unsupported outbox state")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._session() as session:
            rows = session.execute(
                select(OutboxRecord).where(
                    OutboxRecord.state == state
                ).order_by(
                    OutboxRecord.updated_at,
                    OutboxRecord.created_at,
                    OutboxRecord.id,
                ).limit(limit)
            ).scalars().all()
            return [{
                "id": row.id,
                "job_id": row.job_id,
                "workflow_id": row.workflow_id,
                "state": row.state,
                "attempts": row.attempts,
                "available_at": row.available_at.isoformat(),
                "last_error": row.last_error,
            } for row in rows]

    def get_outbox_metrics(self, now=None):
        """Return bounded-cardinality relay health counters and pending age."""
        now = now or utcnow()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        with self._session() as session:
            grouped = session.execute(
                select(OutboxRecord.state, func.count(OutboxRecord.id))
                .group_by(OutboxRecord.state)
            ).all()
            counts = {state: int(count) for state, count in grouped}
            oldest = session.execute(
                select(func.min(OutboxRecord.created_at)).where(
                    OutboxRecord.state == "PENDING"
                )
            ).scalar_one_or_none()
            if oldest is not None and (
                    oldest.tzinfo is None or oldest.utcoffset() is None):
                oldest = oldest.replace(tzinfo=timezone.utc)
            return {
                "pending": counts.get("PENDING", 0),
                "processing": counts.get("PROCESSING", 0),
                "published": counts.get("PUBLISHED", 0),
                "dead": counts.get("DEAD", 0),
                "oldest_pending_age_seconds": (
                    max(0, int((now - oldest).total_seconds()))
                    if oldest is not None else None
                ),
            }

    def replay_dead_outbox(self, outbox_id, operator_id, reason, now=None):
        """Audited manual replay of one dead row; authorization is caller-owned.

        This low-level method must only be exposed through an authenticated,
        permission-checked operator action. The operator identity and reason
        are persisted as a durable event.
        """
        if not isinstance(operator_id, str) or not operator_id.strip() or len(operator_id) > 255:
            raise ValueError("operator_id must contain 1 to 255 characters")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1024:
            raise ValueError("reason must contain 1 to 1024 characters")
        now = now or utcnow()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        with self._session.begin() as session:
            row = session.execute(
                select(OutboxRecord).where(
                    OutboxRecord.id == outbox_id
                ).with_for_update()
            ).scalar_one_or_none()
            if row is None or row.state != "DEAD":
                return False
            row.state = "PENDING"
            row.attempts = 0
            row.available_at = now
            row.updated_at = now
            row.last_error = None
            row.locked_by = None
            row.lock_token_hash = None
            row.lease_expires_at = None
            _record_event(
                session,
                row.job_id,
                row.workflow_id,
                "OUTBOX_MANUAL_REPLAY_REQUESTED",
                payload={
                    "outbox_id": row.id,
                    "operator_id": operator_id.strip(),
                    "reason": reason.strip(),
                },
            )
            return True

    def get_outbox_snapshot(self, outbox_id):
        """Return non-secret operational metadata for one outbox delivery."""
        with self._session() as session:
            row = session.get(OutboxRecord, outbox_id)
            if row is None:
                return None
            return {
                "id": row.id,
                "job_id": row.job_id,
                "workflow_id": row.workflow_id,
                "event_type": row.event_type,
                "schema_version": row.schema_version,
                "plan_version": row.plan_version,
                "plan_fingerprint": row.plan_fingerprint,
                "dedupe_key": row.dedupe_key,
                "state": row.state,
                "attempts": row.attempts,
                "available_at": row.available_at.isoformat(),
                "published_at": (
                    row.published_at.isoformat() if row.published_at else None
                ),
                "last_error": row.last_error,
            }

    def get_job_secret_envelope(
            self, job_id, principal, purpose, now=None):
        """Return encrypted material only after exact Job-owner authorization.

        Plaintext decryption remains the responsibility of the internal
        delivery adapter. This method never returns or logs secret contents.
        """
        from video_streaming.security import AuthorizationError
        from video_streaming.security.secret_store import (
            SecretContext,
            SecretEnvelope,
        )

        if purpose != "webhook.callback":
            raise ValueError("unsupported Job secret purpose")
        self.authorize_job_access(
            job_id, principal, "video.jobs:callback:resolve"
        )
        current = now or utcnow()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")

        with self._session() as session:
            record = session.execute(
                select(JobSecretRecord).where(
                    JobSecretRecord.job_id == job_id,
                    JobSecretRecord.tenant_id == principal.tenant_id,
                    JobSecretRecord.issuer == principal.issuer,
                    JobSecretRecord.subject == principal.subject,
                    JobSecretRecord.purpose == purpose,
                    JobSecretRecord.status == "ACTIVE",
                    JobSecretRecord.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
            if record is None:
                raise AuthorizationError("Resource not found or access denied")
            expires_at = record.expires_at
            # SQLite drops timezone metadata for DateTime(timezone=True);
            # PostgreSQL preserves it. Stored values are normalized to UTC.
            if expires_at is not None and (
                    expires_at.tzinfo is None or expires_at.utcoffset() is None):
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at is not None and current >= expires_at:
                raise AuthorizationError("Resource not found or access denied")

            context = SecretContext(
                job_id=record.job_id,
                tenant_id=record.tenant_id,
                issuer=record.issuer,
                subject=record.subject,
                purpose=record.purpose,
            )
            return SecretEnvelope(
                secret_id=record.secret_id,
                context=context,
                nonce=record.nonce,
                ciphertext=record.ciphertext,
                wrapped_dek=record.wrapped_dek,
                kms_key_id=record.kms_key_id,
                created_at=record.created_at.isoformat(),
                expires_at=(
                    expires_at.isoformat()
                    if expires_at is not None else None
                ),
                algorithm=record.algorithm,
            )

    def get_job_secret_envelope_for_worker(
            self, job_id, worker_identity, purpose, secret_id, now=None):
        """Fetch encrypted callback material for an STS-verified worker only.

        This internal API is deliberately separate from the owner-facing
        get_job_secret_envelope method. The worker identity must have been
        produced by the STS verifier; it is never accepted from task arguments.
        KMS Decrypt permissions must independently be restricted to this
        workload role and the configured encryption context.
        """
        from video_streaming.workflows.worker_identity import (
            WorkflowWorkerIdentity,
        )
        from video_streaming.security import AuthorizationError
        from video_streaming.security.secret_store import (
            SecretContext,
            SecretEnvelope,
        )

        if not isinstance(worker_identity, WorkflowWorkerIdentity):
            raise AuthorizationError("Workflow worker identity is required")
        if purpose not in ("webhook.callback", "webhook.signing"):
            raise ValueError("unsupported Job secret purpose")
        if (
                not isinstance(secret_id, str)
                or not secret_id
                or len(secret_id) > 64):
            raise ValueError("an exact opaque secret_id is required")
        current = now or utcnow()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")

        with self._session.begin() as session:
            record = session.execute(
                select(JobSecretRecord)
                .join(JobRecord, JobRecord.id == JobSecretRecord.job_id)
                .where(
                    JobSecretRecord.job_id == job_id,
                    JobSecretRecord.secret_id == secret_id,
                    JobSecretRecord.purpose == purpose,
                    JobSecretRecord.status == "ACTIVE",
                    JobSecretRecord.deleted_at.is_(None),
                    JobRecord.owner_tenant_id == JobSecretRecord.tenant_id,
                    JobRecord.owner_issuer == JobSecretRecord.issuer,
                    JobRecord.owner_subject == JobSecretRecord.subject,
                )
            ).scalar_one_or_none()
            if record is None:
                raise AuthorizationError("Resource not found or access denied")
            expires_at = record.expires_at
            if expires_at is not None and (
                    expires_at.tzinfo is None or expires_at.utcoffset() is None):
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at is not None and current >= expires_at:
                raise AuthorizationError("Resource not found or access denied")
            context = SecretContext(
                job_id=record.job_id,
                tenant_id=record.tenant_id,
                issuer=record.issuer,
                subject=record.subject,
                purpose=record.purpose,
            )
            workflow = session.execute(
                select(WorkflowRecord).where(
                    WorkflowRecord.job_id == job_id
                )
            ).scalar_one()
            _record_event(
                session,
                job_id,
                workflow.id,
                "JOB_SECRET_RESOLVE_AUTHORIZED",
                payload={
                    "secret_id": record.secret_id,
                    "purpose": purpose,
                    "worker_arn": worker_identity.arn,
                },
            )
            return SecretEnvelope(
                secret_id=record.secret_id,
                context=context,
                nonce=record.nonce,
                ciphertext=record.ciphertext,
                wrapped_dek=record.wrapped_dek,
                kms_key_id=record.kms_key_id,
                created_at=record.created_at.isoformat(),
                expires_at=expires_at.isoformat() if expires_at else None,
                algorithm=record.algorithm,
            )

    def authorize_job_access(self, job_id, principal, required_scope):
        """Authorize one durable Job without exposing cross-tenant existence."""
        from video_streaming.security import authorize_tenant_resource

        if principal is None:
            from video_streaming.security import AuthorizationError
            raise AuthorizationError("Resource not found or access denied")

        with self._session() as session:
            job = session.execute(
                select(JobRecord).where(JobRecord.id == job_id)
            ).scalar_one_or_none()
            if job is None:
                from video_streaming.security import AuthorizationError
                raise AuthorizationError("Resource not found or access denied")
            authorize_tenant_resource(
                principal,
                job.owner_tenant_id,
                required_scope,
            )
            if (
                    job.owner_issuer != principal.issuer
                    or job.owner_subject != principal.subject):
                from video_streaming.security import AuthorizationError
                raise AuthorizationError("Resource not found or access denied")
        return True

    def authorize_playback_session_access(
            self, session_id, principal, required_scope):
        """Authorize a playback session through its owning durable Job.

        The session token remains a separate capability checked when events
        are recorded; this guard additionally requires the authenticated Job
        owner and the requested tenant scope. Unknown and foreign sessions
        deliberately share the same denial response.
        """
        from video_streaming.security import (
            AuthorizationError,
            authorize_tenant_resource,
        )

        if principal is None:
            raise AuthorizationError("Resource not found or access denied")

        with self._session() as session:
            job = session.execute(
                select(JobRecord)
                .join(
                    PlaybackSessionRecord,
                    PlaybackSessionRecord.job_id == JobRecord.id,
                )
                .where(PlaybackSessionRecord.id == session_id)
            ).scalar_one_or_none()
            if job is None:
                raise AuthorizationError("Resource not found or access denied")
            authorize_tenant_resource(
                principal, job.owner_tenant_id, required_scope
            )
            if (
                    job.owner_issuer != principal.issuer
                    or job.owner_subject != principal.subject):
                raise AuthorizationError("Resource not found or access denied")
        return True

    @staticmethod
    def _verify_execution_plan_snapshot(workflow, job_id):
        """Verify plan bytes and their denormalized version/identity columns."""
        plan = workflow.execution_plan_json
        if plan is None:
            return
        actual_fingerprint = execution_plan_sha256(plan)
        if not hmac.compare_digest(
                actual_fingerprint,
                workflow.execution_plan_fingerprint or ""):
            raise RuntimeError("Persisted execution plan fingerprint mismatch")
        if (
                plan.get("job_id") != job_id
                or plan.get("workflow_name") != workflow.name
                or plan.get("schema_version")
                != workflow.execution_plan_schema_version
                or plan.get("workflow_version") != workflow.workflow_version):
            raise RuntimeError("Persisted execution plan metadata mismatch")

    def get_execution_plan(self, job_id):
        """Return a detached immutable plan snapshot and its verification metadata."""
        with self._session() as session:
            workflow = session.execute(
                select(WorkflowRecord).where(WorkflowRecord.job_id == job_id)
            ).scalar_one_or_none()
            if workflow is None:
                return None
            if workflow.execution_plan_json is None:
                return None
            self._verify_execution_plan_snapshot(workflow, job_id)
            return {
                "workflow_id": workflow.id,
                "workflow_version": workflow.workflow_version,
                "schema_version": workflow.execution_plan_schema_version,
                "fingerprint": workflow.execution_plan_fingerprint,
                "plan": workflow.execution_plan_json,
            }

    def _find_stage(self, session, job_id, stage_key, lock=False):
        query = (
            select(StageRecord)
            .join(WorkflowRecord, StageRecord.workflow_id == WorkflowRecord.id)
            .where(
                WorkflowRecord.job_id == job_id,
                StageRecord.stage_key == stage_key,
            )
        )
        if lock:
            # The joined Workflow row participates in Job/Workflow updates
            # with a different lock order. Lock only the Stage row here;
            # callers that mutate Job state must then lock Job before Workflow.
            query = query.with_for_update(of=StageRecord)
        stage = session.execute(query).scalar_one_or_none()
        if stage is None:
            raise KeyError("Unknown workflow stage: {} / {}".format(
                job_id, stage_key
            ))
        return stage

    @staticmethod
    def _validated_lease_ttl(lease_ttl_seconds):
        if isinstance(lease_ttl_seconds, bool):
            raise ValueError("lease_ttl_seconds must be a positive finite number")
        try:
            ttl = float(lease_ttl_seconds)
        except (TypeError, ValueError):
            raise ValueError("lease_ttl_seconds must be a positive finite number")
        if not math.isfinite(ttl) or ttl <= 0 or ttl > 86400:
            raise ValueError("lease_ttl_seconds must be between 0 and 86400")
        return ttl

    @staticmethod
    def _assert_attempt_lease(attempt, lease_token, now=None):
        """Fence worker writes by the current, unexpired attempt capability."""
        if attempt.lease_token_hash is None:
            if lease_token is not None:
                raise AttemptLeaseLost("Attempt does not have a worker lease")
            return
        now = now or utcnow()
        lease_expires_at = attempt.lease_expires_at
        if lease_expires_at is not None and (
                lease_expires_at.tzinfo is None
                or lease_expires_at.utcoffset() is None):
            lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
        if (
                not isinstance(lease_token, str)
                or not hmac.compare_digest(
                    attempt.lease_token_hash,
                    hashlib.sha256(lease_token.encode("utf-8")).hexdigest(),
                )
                or attempt.state != AttemptState.RUNNING.value
                or lease_expires_at is None
                or lease_expires_at <= now):
            raise AttemptLeaseLost("Attempt lease is no longer valid")

    def _recover_expired_attempt_in_session(
            self, session, stage, attempt, now, pricing=None):
        lease_expires_at = attempt.lease_expires_at
        if lease_expires_at is not None and (
                lease_expires_at.tzinfo is None
                or lease_expires_at.utcoffset() is None):
            lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
        if (
                attempt.state != AttemptState.RUNNING.value
                or attempt.lease_token_hash is None
                or lease_expires_at is None
                or lease_expires_at > now
                or stage.status not in (
                    StageState.PROCESSING.value,
                    StageState.VALIDATED.value,
                    StageState.PUBLISHING.value)):
            return False

        workflow = session.get(WorkflowRecord, stage.workflow_id)
        job = session.execute(
            select(JobRecord).where(
                JobRecord.id == workflow.job_id
            ).with_for_update()
        ).scalar_one_or_none()
        if stage.status in (
                StageState.PROCESSING.value,
                StageState.VALIDATED.value):
            # A validation report from an unfinished processing Attempt is
            # not a valid hand-off. Publication Attempt recovery is different:
            # it must retain the report from the already-succeeded processor.
            reports = session.execute(
                select(ArtifactRecord).where(
                    ArtifactRecord.stage_id == stage.id,
                    ArtifactRecord.attempt_id == attempt.id,
                    ArtifactRecord.artifact_type == "validation_report",
                    ArtifactRecord.logical_name == "validation-report",
                    ArtifactRecord.status == "AVAILABLE",
                )
            ).scalars().all()
            for report in reports:
                report.status = "INVALIDATED"

        if job is None or job.state in (
                JobState.CANCELLED.value,
                JobState.FAILED.value,
                JobState.FINISHED.value):
            previous_status = stage.status
            self._transition(stage, StageState.CANCELLED)
            attempt.state = AttemptState.CANCELLED.value
            attempt.finished_at = now
            started = attempt.started_at
            if started.tzinfo is None or started.utcoffset() is None:
                started = started.replace(tzinfo=timezone.utc)
            attempt.duration_ms = max(
                0, int((now - started).total_seconds() * 1000)
            )
            self._apply_attempt_accounting(
                attempt,
                {
                    "duration_ms": attempt.duration_ms,
                    "usage_capture": "incomplete_worker_lost",
                },
                pricing,
            )
            self._clear_attempt_lease(attempt)
            attempt.error_code = "JOB_TERMINAL"
            attempt.error_message = (
                "Expired worker lease recovered after terminal Job state"
            )
            if job is not None:
                _record_event(
                    session,
                    workflow.job_id,
                    workflow.id,
                    "ATTEMPT_LEASE_EXPIRED_TERMINAL_JOB",
                    from_state=previous_status,
                    to_state=StageState.CANCELLED.value,
                    stage_id=stage.id,
                    attempt_id=attempt.id,
                    payload={"job_state": job.state},
                )
            return True

        retry_limit = None
        if (
                workflow is not None
                and workflow.execution_backend == "durable_vod_v1"
                and isinstance(workflow.execution_plan_json, dict)):
            node = next((
                item for item in workflow.execution_plan_json.get("nodes", [])
                if item.get("stage_key") == stage.stage_key
            ), None)
            retry_policy = node.get("retry_policy", {}) if node else {}
            retry_limit = retry_policy.get("max_attempts")
        exhausted = (
            isinstance(retry_limit, int)
            and not isinstance(retry_limit, bool)
            and attempt.attempt_number >= retry_limit
        )
        target_stage = (
            StageState.FAILED if exhausted else StageState.RETRY_WAIT
        )
        previous_status = stage.status
        self._transition(stage, target_stage)
        attempt.state = (
            AttemptState.FAILED.value
            if exhausted else AttemptState.RETRYABLE.value
        )
        attempt.finished_at = now
        started = attempt.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        attempt.duration_ms = max(
            0, int((now - started).total_seconds() * 1000)
        )
        metrics = {
            "duration_ms": attempt.duration_ms,
            "usage_capture": "incomplete_worker_lost",
        }
        self._apply_attempt_accounting(attempt, metrics, pricing)
        self._clear_attempt_lease(attempt)
        attempt.error_code = "WORKER_LEASE_EXPIRED"
        attempt.error_message = "Recovered expired worker lease"
        if not exhausted:
            retry_not_before = _durable_retry_not_before(
                workflow, stage.stage_key, attempt
            )
            _enqueue_durable_dispatch(
                session,
                workflow,
                workflow.job_id,
                attempt.id,
                stage.id,
                retry_not_before or now,
                reason="retry",
                event_type="WORKFLOW_RETRY_ENQUEUED",
            )
        if exhausted:
            workflow.state = WorkflowState.FAILED.value
            workflow.updated_at = now
            job = session.get(JobRecord, workflow.job_id)
            if job is not None:
                job.state = JobState.FAILED.value
                job.updated_at = now
                job.version += 1
                _record_event(
                    session,
                    workflow.job_id,
                    workflow.id,
                    "JOB_FAILED",
                    from_state=JobState.RUNNING.value,
                    to_state=JobState.FAILED.value,
                    stage_id=stage.id,
                    attempt_id=attempt.id,
                    payload={
                        "error_code": "WORKER_LEASE_RETRY_LIMIT_EXHAUSTED",
                    },
                )
        _record_event(
            session,
            workflow.job_id,
            workflow.id,
            (
                "ATTEMPT_LEASE_RETRY_LIMIT_EXHAUSTED"
                if exhausted else "ATTEMPT_LEASE_EXPIRED"
            ),
            from_state=previous_status,
            to_state=target_stage.value,
            stage_id=stage.id,
            attempt_id=attempt.id,
            payload={
                "reason": (
                    "WORKER_LEASE_RETRY_LIMIT_EXHAUSTED"
                    if exhausted else "WORKER_LEASE_EXPIRED"
                ),
                "attempt_number": attempt.attempt_number,
                "max_attempts": retry_limit,
            },
        )
        return True

    def _recover_expired_attempt_for_stage(
            self, job_id, stage_key, pricing=None):
        """Commit recovery independently of a later claim's eligibility checks.

        A claim can legitimately be rejected after recovery (for example while
        a retry delay or dependency is pending). Keeping recovery in that claim
        transaction would roll it back with the rejection.
        """
        with self._session.begin() as session:
            stage = self._find_stage(
                session, job_id, stage_key, lock=True
            )
            attempt = session.execute(
                select(AttemptRecord).where(
                    AttemptRecord.stage_id == stage.id,
                    AttemptRecord.state == AttemptState.RUNNING.value,
                ).order_by(AttemptRecord.attempt_number.desc())
                .limit(1).with_for_update()
            ).scalar_one_or_none()
            if attempt is None:
                return False
            return self._recover_expired_attempt_in_session(
                session, stage, attempt, utcnow(), pricing
            )

    def claim_attempt(
            self,
            job_id,
            stage_key,
            lease_owner,
            lease_ttl_seconds=60,
            execution_backend="celery",
            execution_ref=None,
            pricing=None,
            require_dependencies=False):
        """Atomically claim a stage and return a one-time worker lease token.

        The raw token is returned only to the claiming process. Only its SHA-256
        digest is persisted; it must never be copied into Redis or broker args.
        """
        if (
                not isinstance(lease_owner, str)
                or not lease_owner.strip()
                or len(lease_owner) > 255):
            raise ValueError("lease_owner must contain 1 to 255 characters")
        ttl = self._validated_lease_ttl(lease_ttl_seconds)
        self._recover_expired_attempt_for_stage(
            job_id, stage_key, pricing=pricing
        )
        lease_token = secrets.token_urlsafe(32)
        lease_token_hash = hashlib.sha256(
            lease_token.encode("utf-8")
        ).hexdigest()

        with self._session.begin() as session:
            stage = self._find_stage(session, job_id, stage_key, lock=True)
            workflow = session.get(WorkflowRecord, stage.workflow_id)
            if (
                    workflow.execution_backend == "durable_vod_v1"
                    and execution_backend != "workflow_node"):
                raise InvalidStateTransition(
                    "Legacy task cannot claim an Attempt owned by the durable workflow executor"
                )
            if require_dependencies:
                job = session.get(JobRecord, job_id)
                if job is None or job.state in (
                        JobState.CANCELLED.value,
                        JobState.FAILED.value,
                        JobState.FINISHED.value):
                    raise InvalidStateTransition(
                        "A terminal or missing Job cannot claim a workflow node"
                    )
                dependency_rows = session.execute(
                    select(StageRecord)
                    .join(
                        StageDependencyRecord,
                        StageDependencyRecord.depends_on_stage_id == StageRecord.id,
                    )
                    .where(
                        StageDependencyRecord.stage_id == stage.id,
                        StageDependencyRecord.workflow_id == stage.workflow_id,
                    )
                    .with_for_update()
                ).scalars().all()
                if any(
                        dependency.status != StageState.FINISHED.value
                        for dependency in dependency_rows):
                    raise InvalidStateTransition(
                        "Workflow node dependencies are not all finished"
                    )
            if stage.status in (
                    StageState.PROCESSING.value,
                    StageState.VALIDATED.value,
                    StageState.PUBLISHING.value):
                running = session.execute(
                    select(AttemptRecord).where(
                        AttemptRecord.stage_id == stage.id,
                        AttemptRecord.state == AttemptState.RUNNING.value,
                    ).order_by(AttemptRecord.attempt_number.desc())
                    .with_for_update()
                ).scalar_one_or_none()
                if running is None:
                    raise StageAlreadyRunning(
                        "Stage {} has an active state without a claimable attempt"
                        .format(stage_key)
                    )
                # Expired leases were recovered and committed in a
                # separate transaction before this claim. If a lease expires
                # while this transaction waits, reject now; the next delivery
                # will recover it without rolling back unrelated state.
                raise StageAlreadyRunning(
                    "Stage {} already has an active attempt lease".format(stage_key)
                )

            if (
                    stage.status == StageState.RETRY_WAIT.value
                    and require_dependencies
                    and workflow.execution_backend == "durable_vod_v1"):
                latest_attempt = session.execute(
                    select(AttemptRecord).where(
                        AttemptRecord.stage_id == stage.id,
                    ).order_by(AttemptRecord.attempt_number.desc())
                    .limit(1)
                ).scalar_one_or_none()
                retry_not_before = _durable_retry_not_before(
                    workflow, stage.stage_key, latest_attempt
                )
                if retry_not_before is not None and utcnow() < retry_not_before:
                    raise InvalidStateTransition(
                        "Workflow node retry delay has not elapsed"
                    )

            if stage.status == StageState.RETRY_WAIT.value:
                validated_artifact = session.execute(
                    select(ArtifactRecord).where(
                        ArtifactRecord.stage_id == stage.id,
                        ArtifactRecord.artifact_type == "validation_report",
                        ArtifactRecord.logical_name == "validation-report",
                        ArtifactRecord.status == "AVAILABLE",
                    ).order_by(ArtifactRecord.created_at.desc())
                ).scalars().first()
                validated_report = (
                    validated_artifact.metadata_json
                    if validated_artifact is not None else None
                )
                if (
                        isinstance(validated_report, dict)
                        and validated_report.get("valid") is True
                        and validated_report.get("errors") == []):
                    raise InvalidStateTransition(
                        "A validated output must be retried through "
                        "claim_publication_attempt"
                    )

            if stage.status == StageState.RETRY_WAIT.value:
                validated_artifact = session.execute(
                    select(ArtifactRecord).where(
                        ArtifactRecord.stage_id == stage.id,
                        ArtifactRecord.artifact_type == "validation_report",
                        ArtifactRecord.logical_name == "validation-report",
                        ArtifactRecord.status == "AVAILABLE",
                    ).order_by(ArtifactRecord.created_at.desc())
                ).scalars().first()
                validated_report = (
                    validated_artifact.metadata_json
                    if validated_artifact is not None else None
                )
                if (
                        isinstance(validated_report, dict)
                        and validated_report.get("valid") is True
                        and validated_report.get("errors") == []):
                    raise InvalidStateTransition(
                        "A validated output must be retried through "
                        "claim_publication_attempt"
                    )

            current = StageState(stage.status)
            if StageState.PROCESSING not in ALLOWED_STAGE_TRANSITIONS[current]:
                raise InvalidStateTransition(
                    "Cannot claim stage {} from {}".format(
                        stage_key, current.value
                    )
                )

            job = session.execute(
                select(JobRecord).where(
                    JobRecord.id == job_id
                ).with_for_update().execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if job is None or job.state in (
                    JobState.CANCELLED.value,
                    JobState.FAILED.value,
                    JobState.FINISHED.value):
                raise InvalidStateTransition(
                    "A terminal or missing Job cannot claim an Attempt"
                )

            # The Job row is the final blocking lock in this claim path.
            # Start the lease only after it is acquired and eligibility passes.
            now = utcnow()
            lease_expires_at = now + timedelta(seconds=ttl)
            previous_status = stage.status
            next_number = session.execute(
                select(func.coalesce(func.max(AttemptRecord.attempt_number), 0))
                .where(AttemptRecord.stage_id == stage.id)
            ).scalar_one() + 1
            attempt_id = new_id()
            session.add(AttemptRecord(
                id=attempt_id,
                stage_id=stage.id,
                attempt_number=next_number,
                state=AttemptState.RUNNING.value,
                execution_backend=execution_backend,
                execution_ref=execution_ref,
                lease_owner=lease_owner.strip(),
                lease_token_hash=lease_token_hash,
                lease_expires_at=lease_expires_at,
                heartbeat_at=now,
                started_at=now,
            ))
            stage.status = StageState.PROCESSING.value
            stage.updated_at = now
            stage.version += 1
            workflow = session.get(WorkflowRecord, stage.workflow_id)
            workflow.state = WorkflowState.RUNNING.value
            workflow.updated_at = now
            job.state = JobState.RUNNING.value
            job.updated_at = now
            job.version += 1
            _record_event(
                session,
                job_id,
                workflow.id,
                "STAGE_ATTEMPT_CLAIMED",
                from_state=previous_status,
                to_state=StageState.PROCESSING.value,
                stage_id=stage.id,
                attempt_id=attempt_id,
                payload={
                    "stage_key": stage_key,
                    "attempt_number": next_number,
                    "execution_backend": execution_backend,
                    "execution_ref": execution_ref,
                    "lease_owner": lease_owner.strip(),
                    "lease_expires_at": lease_expires_at.isoformat(),
                },
            )
            return attempt_id, next_number, lease_token, lease_expires_at

    def claim_node_attempt(
            self, job_id, stage_key, lease_owner, lease_ttl_seconds=60,
            execution_ref=None):
        """Claim a DAG node only after every durable dependency is FINISHED."""
        return self.claim_attempt(
            job_id,
            stage_key,
            lease_owner=lease_owner,
            lease_ttl_seconds=lease_ttl_seconds,
            execution_backend="workflow_node",
            execution_ref=execution_ref,
            require_dependencies=True,
        )

    def complete_node_attempt(
            self, job_id, stage_key, attempt_id, validation_report,
            lease_token, metrics=None, pricing=None, artifacts=None):
        """Finish an internal DAG node under its current fenced lease."""
        if not isinstance(validation_report, dict):
            raise ValueError("validation_report must be a dictionary")
        if (
                validation_report.get("valid") is not True
                or not isinstance(validation_report.get("errors"), list)
                or validation_report.get("errors")):
            raise ValueError("A workflow node requires a successful validation report")
        artifacts = list(artifacts or [])
        if len(artifacts) > 1000:
            raise ValueError("a workflow node cannot publish more than 1000 artifacts")
        normalized_artifacts = []
        logical_names = set()
        from video_streaming.workflows.artifact_store import parse_artifact_uri
        for item in artifacts:
            if not isinstance(item, dict):
                raise ValueError("node artifacts must be dictionaries")
            logical_name = item.get("logical_name")
            artifact_type = item.get("artifact_type")
            uri = item.get("uri")
            media_type = item.get("media_type")
            size_bytes = item.get("size_bytes")
            checksum = item.get("checksum_sha256")
            if not isinstance(logical_name, str) or not logical_name.strip() or len(logical_name) > 255:
                raise ValueError("artifact logical_name must contain 1 to 255 characters")
            if not isinstance(artifact_type, str) or not artifact_type.strip() or len(artifact_type) > 64:
                raise ValueError("artifact_type must contain 1 to 64 characters")
            parse_artifact_uri(uri)
            if logical_name in logical_names:
                raise ValueError("artifact logical_name values must be unique per Attempt")
            logical_names.add(logical_name)
            if media_type is not None and (
                    not isinstance(media_type, str) or len(media_type) > 128):
                raise ValueError("artifact media_type is invalid")
            if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
                raise ValueError("artifact size_bytes must be a non-negative integer")
            if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
                raise ValueError("artifact checksum_sha256 must be a lowercase SHA-256 digest")
            normalized_artifacts.append({
                "logical_name": logical_name,
                "artifact_type": artifact_type,
                "uri": uri,
                "media_type": media_type,
                "size_bytes": size_bytes,
                "checksum_sha256": checksum,
            })
        with self._session.begin() as session:
            stage = self._find_stage(session, job_id, stage_key, lock=True)
            attempt = session.get(AttemptRecord, attempt_id)
            if attempt is None or attempt.stage_id != stage.id:
                raise ValueError("Attempt does not belong to workflow stage")
            self._assert_attempt_lease(attempt, lease_token)
            if stage.status != StageState.PROCESSING.value:
                raise InvalidStateTransition(
                    "Only a processing workflow node can be completed"
                )
            if attempt.state != AttemptState.RUNNING.value:
                raise InvalidStateTransition("Workflow node Attempt is not running")
            job = session.execute(
                select(JobRecord).where(
                    JobRecord.id == job_id
                ).with_for_update()
            ).scalar_one_or_none()
            if job is None:
                raise InvalidStateTransition("Workflow Job no longer exists")
            if job.state in (
                    JobState.CANCELLED.value,
                    JobState.FAILED.value,
                    JobState.FINISHED.value):
                # Fence a live worker after any terminal Job transition.
                now = utcnow()
                terminal_reason = "JOB_{}_BEFORE_NODE_COMPLETION".format(
                    job.state
                )
                previous_status = stage.status
                self._transition(stage, StageState.CANCELLED)
                started = attempt.started_at
                if started.tzinfo is None or started.utcoffset() is None:
                    started = started.replace(tzinfo=timezone.utc)
                attempt.state = AttemptState.CANCELLED.value
                attempt.finished_at = now
                attempt.duration_ms = max(0, int((now - started).total_seconds() * 1000))
                usage = dict(metrics or {})
                usage["duration_ms"] = attempt.duration_ms
                attempt.cpu_seconds = usage.get("cpu_seconds")
                attempt.peak_memory_bytes = usage.get("peak_memory_bytes")
                attempt.input_bytes = usage.get("input_bytes")
                attempt.output_bytes = usage.get("output_bytes")
                self._apply_attempt_accounting(attempt, usage, pricing)
                self._clear_attempt_lease(attempt)
                attempt.error_code = "JOB_TERMINAL"
                attempt.error_message = terminal_reason
                _record_event(
                    session, job_id, stage.workflow_id, "STAGE_CANCELLED",
                    from_state=previous_status, to_state=StageState.CANCELLED.value,
                    stage_id=stage.id, attempt_id=attempt_id,
                    payload={"reason": terminal_reason},
                )
                return False
            now = utcnow()
            previous_status = stage.status
            self._complete_processing_attempt_in_session(
                attempt, metrics or {}, pricing
            )
            attempt.artifact_count = len(normalized_artifacts) + 1
            stage.status = StageState.FINISHED.value
            stage.updated_at = now
            stage.version += 1
            # Persist only bounded, non-sensitive validation metadata. Handler
            # payloads and arbitrary output values remain outside the control DB.
            artifact_id = new_id()
            safe_report = {
                "valid": True,
                "errors": [],
                "operation": stage.operation,
            }
            session.add(ArtifactRecord(
                id=artifact_id,
                job_id=job_id,
                stage_id=stage.id,
                attempt_id=attempt_id,
                artifact_type="node_result",
                logical_name="node-result",
                uri="workflow://jobs/{}/stages/{}/attempts/{}/result".format(
                    job_id, stage_key, attempt_id
                ),
                media_type="application/json",
                status="AVAILABLE",
                metadata_json=safe_report,
            ))
            for published in normalized_artifacts:
                session.add(ArtifactRecord(
                    id=new_id(),
                    job_id=job_id,
                    stage_id=stage.id,
                    attempt_id=attempt_id,
                    artifact_type=published["artifact_type"],
                    logical_name=published["logical_name"],
                    uri=published["uri"],
                    media_type=published["media_type"],
                    size_bytes=published["size_bytes"],
                    checksum_sha256=published["checksum_sha256"],
                    status="AVAILABLE",
                    metadata_json={},
                ))
            _record_event(
                session,
                job_id,
                stage.workflow_id,
                "WORKFLOW_NODE_COMPLETED",
                from_state=previous_status,
                to_state=StageState.FINISHED.value,
                stage_id=stage.id,
                attempt_id=attempt_id,
                payload={"operation": stage.operation, "artifact_id": artifact_id},
            )
            unfinished = session.execute(
                select(func.count(StageRecord.id)).where(
                    StageRecord.workflow_id == stage.workflow_id,
                    StageRecord.status != StageState.FINISHED.value,
                )
            ).scalar_one()
            if unfinished == 0:
                workflow = session.get(WorkflowRecord, stage.workflow_id)
                job = session.get(JobRecord, job_id)
                workflow.state = WorkflowState.FINISHED.value
                workflow.updated_at = now
                job.state = JobState.FINISHED.value
                job.updated_at = now
                job.version += 1
                _record_event(
                    session,
                    job_id,
                    workflow.id,
                    "JOB_FINISHED",
                    from_state=JobState.RUNNING.value,
                    to_state=JobState.FINISHED.value,
                )
            else:
                workflow = session.get(WorkflowRecord, stage.workflow_id)
                if workflow.execution_backend == "durable_vod_v1":
                    if not (
                            workflow.workflow_version
                            and workflow.execution_plan_fingerprint
                            and workflow.execution_plan_schema_version):
                        raise InvalidStateTransition(
                            "Durable workflow continuation requires an immutable plan identity"
                        )
                    # Completion and continuation intent commit together.
                    _enqueue_durable_dispatch(
                        session,
                        workflow,
                        job_id,
                        attempt_id,
                        stage.id,
                        now,
                        reason="continue",
                        event_type="WORKFLOW_CONTINUATION_ENQUEUED",
                    )
            return True

    def list_jobs_with_expired_attempts(self, limit=100, now=None):
        """List bounded durable Jobs whose worker lease has expired.

        This is a recovery scan, not a claim. The subsequent node claim remains
        the concurrency fence; duplicate scheduler deliveries are harmless.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        now = now or utcnow()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        statement = (
            select(JobRecord.id)
            .join(WorkflowRecord, WorkflowRecord.job_id == JobRecord.id)
            .join(StageRecord, StageRecord.workflow_id == WorkflowRecord.id)
            .join(AttemptRecord, AttemptRecord.stage_id == StageRecord.id)
            .where(
                WorkflowRecord.execution_backend == "durable_vod_v1",
                WorkflowRecord.execution_plan_json.is_not(None),
                JobRecord.state.in_(
                    (JobState.QUEUED.value, JobState.RUNNING.value)
                ),
                AttemptRecord.state == AttemptState.RUNNING.value,
                AttemptRecord.lease_token_hash.is_not(None),
                AttemptRecord.lease_expires_at.is_not(None),
                AttemptRecord.lease_expires_at <= now,
            )
            .distinct()
            .order_by(JobRecord.id)
            .limit(limit)
        )
        with self._session() as session:
            return list(session.execute(statement).scalars().all())

    def get_ready_node_ids(self, job_id, now=None):
        """Return only nodes whose dependencies are FINISHED and stage is claimable.

        Expired PROCESSING leases are included so a replay can fence and recover
        a worker that died after broker delivery. Live attempts are never
        fanned out repeatedly.
        """
        now = now or utcnow()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        snapshot = self.get_execution_plan(job_id)
        if snapshot is None:
            return []
        nodes = snapshot["plan"]["nodes"]
        node_by_id = {node["node_id"]: node for node in nodes}
        with self._session() as session:
            workflow = session.execute(
                select(WorkflowRecord).where(
                    WorkflowRecord.job_id == job_id
                )
            ).scalar_one_or_none()
            job = session.get(JobRecord, job_id)
            if workflow is None or job is None or job.state in (
                    JobState.CANCELLED.value, JobState.FAILED.value,
                    JobState.FINISHED.value):
                return []
            stages = session.execute(
                select(StageRecord).where(
                    StageRecord.workflow_id == workflow.id
                )
            ).scalars().all()
            by_key = {stage.stage_key: stage for stage in stages}
            claimable = set()
            for stage in stages:
                if stage.status == StageState.PENDING.value:
                    claimable.add(stage.stage_key)
                elif stage.status == StageState.RETRY_WAIT.value:
                    if workflow.execution_backend != "durable_vod_v1":
                        claimable.add(stage.stage_key)
                    else:
                        latest_attempt = session.execute(
                            select(AttemptRecord).where(
                                AttemptRecord.stage_id == stage.id,
                            ).order_by(AttemptRecord.attempt_number.desc())
                            .limit(1)
                        ).scalar_one_or_none()
                        retry_not_before = _durable_retry_not_before(
                            workflow, stage.stage_key, latest_attempt
                        )
                        if retry_not_before is None or retry_not_before <= now:
                            claimable.add(stage.stage_key)
                elif stage.status == StageState.PROCESSING.value:
                    running = session.execute(
                        select(AttemptRecord).where(
                            AttemptRecord.stage_id == stage.id,
                            AttemptRecord.state == AttemptState.RUNNING.value,
                        ).order_by(AttemptRecord.attempt_number.desc())
                        .limit(1)
                    ).scalar_one_or_none()
                    expiry = running.lease_expires_at if running else None
                    if expiry is not None and (
                            expiry.tzinfo is None or expiry.utcoffset() is None):
                        expiry = expiry.replace(tzinfo=timezone.utc)
                    # Claim logic can recover only a concrete RUNNING
                    # Attempt with a hashed token and an expired lease. A
                    # missing Attempt or lease-free RUNNING row is corrupt/
                    # operator-owned state, not scheduler-ready work.
                    if (
                            running is not None
                            and running.lease_token_hash is not None
                            and expiry is not None
                            and expiry <= now):
                        claimable.add(stage.stage_key)

            ready = []
            for node in nodes:
                stage_key = node["stage_key"]
                if stage_key not in claimable:
                    continue
                dependency_keys = [
                    node_by_id[dependency]["stage_key"]
                    for dependency in node["depends_on"]
                ]
                if all(
                    dependency_key in by_key
                    and by_key[dependency_key].status == StageState.FINISHED.value
                    for dependency_key in dependency_keys
                ):
                    ready.append(node["node_id"])
            return ready

    def get_dependency_artifacts(self, job_id, node_id):
        """Return durable Artifact references from a node's completed parents."""
        snapshot = self.get_execution_plan(job_id)
        if snapshot is None:
            raise KeyError("workflow execution plan not found")
        nodes = {item["node_id"]: item for item in snapshot["plan"]["nodes"]}
        node = nodes.get(node_id)
        if node is None:
            raise KeyError("workflow node not found")
        dependency_keys = [
            nodes[parent]["stage_key"] for parent in node["depends_on"]
        ]
        if not dependency_keys:
            return {}
        with self._session() as session:
            rows = session.execute(
                select(ArtifactRecord, StageRecord.stage_key)
                .join(StageRecord, StageRecord.id == ArtifactRecord.stage_id)
                .where(
                    ArtifactRecord.job_id == job_id,
                    StageRecord.stage_key.in_(dependency_keys),
                    ArtifactRecord.status == "AVAILABLE",
                    ArtifactRecord.uri.like("s3://%"),
                ).order_by(ArtifactRecord.created_at, ArtifactRecord.id)
            ).all()
            result = {key: [] for key in dependency_keys}
            for artifact, stage_key in rows:
                result[stage_key].append({
                    "artifact_id": artifact.id,
                    "artifact_type": artifact.artifact_type,
                    "logical_name": artifact.logical_name,
                    "uri": artifact.uri,
                    "media_type": artifact.media_type,
                    "size_bytes": artifact.size_bytes,
                    "checksum_sha256": artifact.checksum_sha256,
                })
            return result

    def renew_attempt_lease(
            self,
            job_id,
            stage_key,
            attempt_id,
            lease_token,
            lease_ttl_seconds=60):
        """Extend a live lease; an expired or stale worker cannot resurrect it."""
        ttl = self._validated_lease_ttl(lease_ttl_seconds)
        with self._session.begin() as session:
            stage = self._find_stage(session, job_id, stage_key, lock=True)
            attempt = session.execute(
                select(AttemptRecord).where(
                    AttemptRecord.id == attempt_id,
                    AttemptRecord.stage_id == stage.id,
                ).with_for_update()
            ).scalar_one_or_none()
            if attempt is None:
                raise AttemptLeaseLost("Attempt lease is no longer valid")
            # Capture time only after both row locks are held. A transaction
            # may have waited past expiry while acquiring either lock.
            now = utcnow()
            self._assert_attempt_lease(attempt, lease_token, now=now)
            attempt.heartbeat_at = now
            attempt.lease_expires_at = now + timedelta(seconds=ttl)
            return attempt.lease_expires_at

    def claim_publication_attempt(
            self,
            job_id,
            stage_key,
            lease_owner,
            lease_ttl_seconds=60,
            execution_backend="celery",
            execution_ref=None,
            processing_attempt_id=None,
            pricing=None,
            package_output_key=None,
            package_output_bucket=None,
            package_output_kind="package"):
        """Claim a fresh publication attempt for an already validated artifact.

        Processing and publication are separate attempts. The validation report
        is the durable hand-off; no processing lease token is reused or sent
        through Celery.
        """
        if (
                not isinstance(lease_owner, str)
                or not lease_owner.strip()
                or len(lease_owner) > 255):
            raise ValueError("lease_owner must contain 1 to 255 characters")
        if (package_output_key is None) != (package_output_bucket is None):
            raise ValueError(
                "package_output_key and package_output_bucket must be supplied together"
            )
        if package_output_key is not None:
            if not isinstance(package_output_bucket, str) or not package_output_bucket:
                raise ValueError("package_output_bucket is required")
            if package_output_kind not in ("package", "single_file"):
                raise ValueError("unsupported package_output_kind")
            # Validate the logical key before creating a durable Attempt.
            attempt_scoped_object_key(package_output_key, "validation-id")

        ttl = self._validated_lease_ttl(lease_ttl_seconds)
        self._recover_expired_attempt_for_stage(
            job_id, stage_key, pricing=pricing
        )
        lease_token = secrets.token_urlsafe(32)
        lease_token_hash = hashlib.sha256(
            lease_token.encode("utf-8")
        ).hexdigest()

        with self._session.begin() as session:
            stage = self._find_stage(session, job_id, stage_key, lock=True)
            workflow = session.get(WorkflowRecord, stage.workflow_id)
            if workflow.execution_backend == "durable_vod_v1":
                raise InvalidStateTransition(
                    "Legacy publication task cannot claim an Attempt owned by the durable workflow executor"
                )
            if stage.status in (
                    StageState.VALIDATED.value,
                    StageState.PUBLISHING.value):
                running = session.execute(
                    select(AttemptRecord).where(
                        AttemptRecord.stage_id == stage.id,
                        AttemptRecord.state == AttemptState.RUNNING.value,
                    ).order_by(AttemptRecord.attempt_number.desc())
                    .with_for_update()
                ).scalar_one_or_none()
                if running is not None:
                    raise StageAlreadyRunning(
                        "Stage {} already has an active publication attempt"
                        .format(stage_key)
                    )
                elif stage.status == StageState.PUBLISHING.value:
                    raise StageAlreadyRunning(
                        "Stage {} is publishing without an active Attempt".format(
                            stage_key
                        )
                    )

            if stage.status not in (
                    StageState.VALIDATED.value,
                    StageState.RETRY_WAIT.value):
                raise InvalidStateTransition(
                    "Cannot claim publication for stage {} from {}".format(
                        stage_key, stage.status
                    )
                )

            validation_artifact = session.execute(
                select(ArtifactRecord).where(
                    ArtifactRecord.stage_id == stage.id,
                    ArtifactRecord.artifact_type == "validation_report",
                    ArtifactRecord.logical_name == "validation-report",
                    ArtifactRecord.status == "AVAILABLE",
                ).order_by(ArtifactRecord.created_at.desc())
            ).scalars().first()
            report = (
                validation_artifact.metadata_json
                if validation_artifact is not None else None
            )
            if (
                    not isinstance(report, dict)
                    or report.get("valid") is not True
                    or not isinstance(report.get("errors"), list)
                    or report.get("errors")):
                raise InvalidStateTransition(
                    "Publication requires a persisted successful validation report"
                )
            processing_attempt = (
                session.get(AttemptRecord, validation_artifact.attempt_id)
                if validation_artifact is not None else None
            )
            if (
                    processing_attempt is None
                    or processing_attempt.state != AttemptState.SUCCEEDED.value
                    or (
                        processing_attempt_id is not None
                        and validation_artifact.attempt_id != processing_attempt_id
                    )):
                raise InvalidStateTransition(
                    "Publication requires the requested terminal successful "
                    "processing Attempt"
                )

            active = session.execute(
                select(AttemptRecord).where(
                    AttemptRecord.stage_id == stage.id,
                    AttemptRecord.state == AttemptState.RUNNING.value,
                ).with_for_update()
            ).scalars().first()
            if active is not None:
                raise StageAlreadyRunning(
                    "Stage {} already has an active attempt".format(stage_key)
                )

            job = session.execute(
                select(JobRecord).where(
                    JobRecord.id == job_id
                ).with_for_update()
            ).scalar_one_or_none()
            if job is None or job.state in (
                    JobState.CANCELLED.value,
                    JobState.FAILED.value,
                    JobState.FINISHED.value):
                raise InvalidStateTransition(
                    "A terminal or missing Job cannot claim publication"
                )

            # Publication lease timing starts after all row locks and
            # eligibility checks, not before a potentially blocking Job lock.
            now = utcnow()
            lease_expires_at = now + timedelta(seconds=ttl)
            previous_status = stage.status
            if stage.status == StageState.RETRY_WAIT.value:
                self._transition(stage, StageState.VALIDATED)
            else:
                stage.updated_at = now
                stage.version += 1

            next_number = session.execute(
                select(func.coalesce(func.max(AttemptRecord.attempt_number), 0))
                .where(AttemptRecord.stage_id == stage.id)
            ).scalar_one() + 1
            attempt_id = new_id()
            attempt_metrics = {}
            if package_output_key is not None:
                attempt_manifest_key = attempt_scoped_object_key(
                    package_output_key, attempt_id
                )
                attempt_prefix = attempt_manifest_key.rsplit("/", 1)[0]
                namespace = {
                    "kind": package_output_kind,
                    "bucket": package_output_bucket,
                    "logical_key": package_output_key,
                    "prefix": attempt_prefix,
                }
                if package_output_kind == "package":
                    namespace["inventory_key"] = (
                        "{}/.{}.artifacts.json".format(
                            attempt_prefix,
                            package_output_key.rsplit("/", 1)[-1],
                        )
                    )
                else:
                    namespace["object_key"] = attempt_manifest_key
                attempt_metrics["package_publication"] = namespace
            session.add(AttemptRecord(
                id=attempt_id,
                stage_id=stage.id,
                attempt_number=next_number,
                state=AttemptState.RUNNING.value,
                execution_backend=execution_backend,
                execution_ref=execution_ref,
                lease_owner=lease_owner.strip(),
                lease_token_hash=lease_token_hash,
                lease_expires_at=lease_expires_at,
                heartbeat_at=now,
                started_at=now,
                metrics_json=attempt_metrics,
            ))
            workflow = session.get(WorkflowRecord, stage.workflow_id)
            workflow.state = WorkflowState.RUNNING.value
            workflow.updated_at = now
            job.state = JobState.RUNNING.value
            job.updated_at = now
            job.version += 1
            _record_event(
                session,
                job_id,
                workflow.id,
                "STAGE_PUBLICATION_ATTEMPT_CLAIMED",
                from_state=previous_status,
                to_state=stage.status,
                stage_id=stage.id,
                attempt_id=attempt_id,
                payload={
                    "stage_key": stage_key,
                    "attempt_number": next_number,
                    "execution_backend": execution_backend,
                    "execution_ref": execution_ref,
                    "lease_owner": lease_owner.strip(),
                    "lease_expires_at": lease_expires_at.isoformat(),
                    "validation_artifact_id": validation_artifact.id,
                },
            )
            return attempt_id, next_number, lease_token, lease_expires_at

    def start_attempt(
            self,
            job_id,
            stage_key,
            execution_backend="celery",
            execution_ref=None):
        """Start or idempotently recover the currently-running stage attempt."""
        with self._session.begin() as session:
            stage = self._find_stage(session, job_id, stage_key, lock=True)
            if stage.status in (
                    StageState.PROCESSING.value,
                    StageState.VALIDATED.value,
                    StageState.PUBLISHING.value):
                running = session.execute(
                    select(AttemptRecord).where(
                        AttemptRecord.stage_id == stage.id,
                        AttemptRecord.state == AttemptState.RUNNING.value,
                    ).order_by(AttemptRecord.attempt_number.desc())
                ).scalar_one_or_none()
                if (
                        stage.status == StageState.PROCESSING.value
                        and running is not None
                        and running.execution_ref == execution_ref):
                    return running.id, running.attempt_number
                raise StageAlreadyRunning(
                    "Stage {} already has an active attempt or publication"
                    .format(stage_key)
                )

            current = StageState(stage.status)
            if StageState.PROCESSING not in ALLOWED_STAGE_TRANSITIONS[current]:
                raise InvalidStateTransition(
                    "Cannot start stage {} from {}".format(stage_key, current.value)
                )

            previous_status = stage.status
            next_number = session.execute(
                select(func.coalesce(func.max(AttemptRecord.attempt_number), 0))
                .where(AttemptRecord.stage_id == stage.id)
            ).scalar_one() + 1
            attempt_id = new_id()
            session.add(AttemptRecord(
                id=attempt_id,
                stage_id=stage.id,
                attempt_number=next_number,
                state=AttemptState.RUNNING.value,
                execution_backend=execution_backend,
                execution_ref=execution_ref,
                started_at=utcnow(),
            ))
            stage.status = StageState.PROCESSING.value
            stage.updated_at = utcnow()
            stage.version += 1
            workflow = session.get(WorkflowRecord, stage.workflow_id)
            workflow.state = WorkflowState.RUNNING.value
            workflow.updated_at = utcnow()
            job = session.get(JobRecord, job_id)
            job.state = JobState.RUNNING.value
            job.updated_at = utcnow()
            job.version += 1
            _record_event(
                session,
                job_id,
                workflow.id,
                "STAGE_ATTEMPT_STARTED",
                from_state=previous_status,
                to_state=StageState.PROCESSING.value,
                stage_id=stage.id,
                attempt_id=attempt_id,
                payload={
                    "stage_key": stage_key,
                    "attempt_number": next_number,
                    "execution_backend": execution_backend,
                    "execution_ref": execution_ref,
                },
            )
            return attempt_id, next_number

    def _transition(self, stage, target):
        current = StageState(stage.status)
        if target not in ALLOWED_STAGE_TRANSITIONS[current]:
            raise InvalidStateTransition(
                "Illegal workflow stage transition: {} -> {}".format(
                    current.value, target.value
                )
            )
        stage.status = target.value
        stage.updated_at = utcnow()
        stage.version += 1

    def mark_validated(
            self,
            job_id,
            stage_key,
            attempt_id,
            validation_report,
            lease_token=None,
            complete_attempt=False,
            metrics=None,
            pricing=None):
        """Persist a validation report; optionally close the processing attempt."""
        if not isinstance(validation_report, dict):
            raise ValueError("validation_report must be a dictionary")
        if validation_report.get("valid") is not True:
            raise ValueError("Only a valid report can validate a workflow stage")
        errors = validation_report.get("errors")
        if not isinstance(errors, list) or errors:
            raise ValueError("A valid report must contain an empty errors list")
        with self._session.begin() as session:
            stage = self._find_stage(session, job_id, stage_key, lock=True)
            attempt = session.get(AttemptRecord, attempt_id)
            if attempt is None or attempt.stage_id != stage.id:
                raise ValueError("Attempt does not belong to workflow stage")
            self._assert_attempt_lease(attempt, lease_token)
            if stage.status == StageState.VALIDATED.value:
                existing = session.execute(
                    select(ArtifactRecord).where(
                        ArtifactRecord.stage_id == stage.id,
                        ArtifactRecord.attempt_id == attempt_id,
                        ArtifactRecord.logical_name == "validation-report",
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if complete_attempt and attempt.state == AttemptState.RUNNING.value:
                        self._complete_processing_attempt_in_session(
                            attempt, metrics, pricing
                        )
                        _record_event(
                            session,
                            job_id,
                            stage.workflow_id,
                            "STAGE_PROCESSING_ATTEMPT_COMPLETED",
                            from_state=AttemptState.RUNNING.value,
                            to_state=AttemptState.SUCCEEDED.value,
                            stage_id=stage.id,
                            attempt_id=attempt_id,
                            payload={
                                "validation_artifact_id": existing.id,
                                "idempotent_validation_replay": True,
                            },
                        )
                    return False
            if attempt.state != AttemptState.RUNNING.value:
                raise InvalidStateTransition(
                    "Only a running attempt can validate a workflow stage"
                )
            previous_status = stage.status
            self._transition(stage, StageState.VALIDATED)
            stage.output_contract_json = {
                "validation": validation_report,
            }
            validation_artifact_id = new_id()
            session.add(ArtifactRecord(
                id=validation_artifact_id,
                job_id=job_id,
                stage_id=stage.id,
                attempt_id=attempt_id,
                artifact_type="validation_report",
                logical_name="validation-report",
                uri="workflow://jobs/{}/stages/{}/attempts/{}/validation-report".format(
                    job_id, stage_key, attempt_id
                ),
                media_type="application/json",
                status="AVAILABLE",
                metadata_json=validation_report,
            ))
            _record_event(
                session,
                job_id,
                stage.workflow_id,
                "STAGE_VALIDATED",
                from_state=previous_status,
                to_state=StageState.VALIDATED.value,
                stage_id=stage.id,
                attempt_id=attempt_id,
                payload={
                    "report_valid": validation_report.get("valid"),
                    "error_count": len(validation_report.get("errors", [])),
                },
            )
            if complete_attempt:
                self._complete_processing_attempt_in_session(
                    attempt, metrics, pricing
                )
                _record_event(
                    session,
                    job_id,
                    stage.workflow_id,
                    "STAGE_PROCESSING_ATTEMPT_COMPLETED",
                    from_state=AttemptState.RUNNING.value,
                    to_state=AttemptState.SUCCEEDED.value,
                    stage_id=stage.id,
                    attempt_id=attempt_id,
                    payload={"validation_artifact_id": validation_artifact_id},
                )
            return True

    @staticmethod
    def _complete_processing_attempt_in_session(attempt, metrics, pricing):
        """Close the processing attempt while leaving the Stage VALIDATED."""
        if attempt.state != AttemptState.RUNNING.value:
            raise InvalidStateTransition("Processing attempt is not running")
        now = utcnow()
        attempt.state = AttemptState.SUCCEEDED.value
        attempt.finished_at = now
        started = attempt.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        attempt.duration_ms = max(
            0, int((now - started).total_seconds() * 1000)
        )
        usage = dict(metrics or {})
        usage["duration_ms"] = attempt.duration_ms
        attempt.cpu_seconds = usage.get("cpu_seconds")
        attempt.peak_memory_bytes = usage.get("peak_memory_bytes")
        attempt.input_bytes = usage.get("input_bytes")
        attempt.output_bytes = usage.get("output_bytes")
        # The validation report is the processing Attempt's durable artifact.
        # Publication artifacts are counted later on the separate Attempt.
        attempt.artifact_count = max(1, int(attempt.artifact_count or 0))
        WorkflowRepository._apply_attempt_accounting(attempt, usage, pricing)
        # The process-local token is no longer useful after the processing
        # attempt is terminal. Clear the complete lease tuple atomically.
        WorkflowRepository._clear_attempt_lease(attempt)

    @staticmethod
    def _clear_attempt_lease(attempt):
        attempt.lease_owner = None
        attempt.lease_token_hash = None
        attempt.lease_expires_at = None
        attempt.heartbeat_at = None

    def complete_processing_attempt(
            self, job_id, stage_key, attempt_id, validation_report,
            lease_token, metrics=None, pricing=None):
        """Validate output and close its processing attempt in one transaction."""
        return self.mark_validated(
            job_id,
            stage_key,
            attempt_id,
            validation_report,
            lease_token=lease_token,
            complete_attempt=True,
            metrics=metrics,
            pricing=pricing,
        )

    def start_publishing(
            self, job_id, stage_key, attempt_id, lease_token=None):
        with self._session.begin() as session:
            stage = self._find_stage(session, job_id, stage_key, lock=True)
            attempt = session.get(AttemptRecord, attempt_id)
            if attempt is None or attempt.stage_id != stage.id:
                raise ValueError("Attempt does not belong to workflow stage")
            self._assert_attempt_lease(attempt, lease_token)
            if stage.status == StageState.PUBLISHING.value:
                if attempt.state == AttemptState.RUNNING.value:
                    return False
                raise StageAlreadyRunning(
                    "Stage {} is already being published by another attempt"
                    .format(stage_key)
                )
            if attempt.state != AttemptState.RUNNING.value:
                raise InvalidStateTransition("Attempt is not running")
            previous_status = stage.status
            self._transition(stage, StageState.PUBLISHING)
            _record_event(
                session,
                job_id,
                stage.workflow_id,
                "STAGE_PUBLISHING",
                from_state=previous_status,
                to_state=StageState.PUBLISHING.value,
                stage_id=stage.id,
                attempt_id=attempt_id,
            )

    @staticmethod
    def _build_cost_accounting(metrics, pricing):
        """Build an immutable price-card snapshot and coverage-aware estimate."""
        if not pricing:
            return None

        model_id = str(pricing.get("model_id") or "").strip()
        if not model_id:
            raise ValueError("A priced attempt requires a non-empty model_id")

        model_version = str(
            pricing.get("model_version") or pricing.get("version") or ""
        ).strip()
        if not model_version:
            match = re.search(r"-v([0-9]+)$", model_id)
            if match:
                model_version = match.group(1)
            else:
                raise ValueError(
                    "A price card requires model_version; use immutable versions"
                )

        currency = str(pricing.get("currency") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}|RUNIT", currency):
            raise ValueError("Pricing currency must be a three-letter code or RUNIT")

        rates = {}
        for key in ("cpu_per_second", "memory_gb_second", "storage_gb"):
            try:
                value = float(pricing.get(key, 0.0))
            except (TypeError, ValueError):
                raise ValueError("Invalid price-card rate: {}".format(key))
            if not math.isfinite(value) or value < 0:
                raise ValueError(
                    "Price-card rates must be finite and non-negative: {}".format(key)
                )
            rates[key] = value
        if not any(value > 0 for value in rates.values()):
            raise ValueError("At least one price-card rate must be greater than zero")

        snapshot = {
            "schema_version": 1,
            "model_id": model_id,
            "model_version": model_version,
            "currency": currency,
            "rates": rates,
            "rate_basis": {
                "cpu_per_second": "CPU-second",
                "memory_gb_second": (
                    "GiB-second estimated as measured peak RSS multiplied by "
                    "encoder processing duration; full attempt duration is a "
                    "conservative fallback when encoder duration is unavailable"
                ),
                "storage_gb": "GiB of published output bytes",
            },
            "excluded_dimensions": [
                "gpu_compute",
                "network_ingress",
                "network_egress",
                "provider_invoice_reconciliation",
                "worker_idle_and_control_plane",
                "power_and_cooling",
            ],
        }

        components = {}
        missing_dimensions = []
        required_dimensions = []
        measured_dimensions = []

        cpu_rate = rates["cpu_per_second"]
        if cpu_rate > 0:
            required_dimensions.append("cpu_compute")
            cpu_seconds = metrics.get("cpu_seconds")
            if cpu_seconds is None:
                missing_dimensions.append("cpu_compute")
                quantity = None
            else:
                quantity = float(cpu_seconds)
                if not math.isfinite(quantity) or quantity < 0:
                    raise ValueError("cpu_seconds must be finite and non-negative")
                measured_dimensions.append("cpu_compute")
            components["cpu_compute"] = {
                "quantity": quantity,
                "unit": "CPU-second",
                "rate": cpu_rate,
                "amount": (
                    round(quantity * cpu_rate, 8)
                    if quantity is not None else None
                ),
            }

        memory_rate = rates["memory_gb_second"]
        if memory_rate > 0:
            required_dimensions.append("memory")
            peak_memory = metrics.get("peak_memory_bytes")
            # Prefer measured encoder wall time; fall back to full attempt
            # duration as a conservative upper-bound approximation.
            duration_ms = metrics.get(
                "memory_duration_ms", metrics.get("duration_ms")
            )
            if peak_memory is None or duration_ms is None:
                missing_dimensions.append("memory")
                quantity = None
            else:
                peak_memory = float(peak_memory)
                duration_ms = float(duration_ms)
                if (
                    not math.isfinite(peak_memory)
                    or peak_memory < 0
                    or not math.isfinite(duration_ms)
                    or duration_ms < 0
                ):
                    raise ValueError(
                        "Memory and duration metrics must be finite and non-negative"
                    )
                quantity = (peak_memory / (1024.0 ** 3)) * (duration_ms / 1000.0)
                measured_dimensions.append("memory")
            components["memory"] = {
                "quantity": quantity,
                "unit": "GiB-second",
                "rate": memory_rate,
                "amount": (
                    round(quantity * memory_rate, 8)
                    if quantity is not None else None
                ),
            }

        storage_rate = rates["storage_gb"]
        if storage_rate > 0:
            required_dimensions.append("storage")
            output_bytes = metrics.get("output_bytes")
            if output_bytes is None:
                missing_dimensions.append("storage")
                quantity = None
            else:
                output_bytes = float(output_bytes)
                if not math.isfinite(output_bytes) or output_bytes < 0:
                    raise ValueError("output_bytes must be finite and non-negative")
                quantity = output_bytes / (1024.0 ** 3)
                measured_dimensions.append("storage")
            components["storage"] = {
                "quantity": quantity,
                "unit": "GiB",
                "rate": storage_rate,
                "amount": (
                    round(quantity * storage_rate, 8)
                    if quantity is not None else None
                ),
            }

        known_amounts = [
            item["amount"] for item in components.values()
            if item["amount"] is not None
        ]
        estimated_cost = (
            round(sum(known_amounts), 8) if known_amounts else None
        )
        coverage = {
            "complete": not missing_dimensions,
            "required_dimensions": required_dimensions,
            "measured_dimensions": measured_dimensions,
            "missing_dimensions": missing_dimensions,
            "excluded_dimensions": list(snapshot["excluded_dimensions"]),
            "not_priced_dimensions": [
                key for key, value in rates.items() if value == 0
            ],
        }
        return {
            "snapshot": snapshot,
            "breakdown": {
                "schema_version": 1,
                "components": components,
                "coverage": coverage,
                "partial_estimated_cost": estimated_cost,
            },
            "estimated_cost": estimated_cost,
            "complete": coverage["complete"],
        }

    @staticmethod
    def _apply_attempt_accounting(attempt, metrics, pricing):
        """Persist usage, a frozen price-card snapshot and explicit coverage."""
        # Preserve non-sensitive lifecycle metadata (for example the package
        # inventory namespace) while replacing the measured usage projection.
        merged_metrics = dict(attempt.metrics_json or {})
        merged_metrics.update(dict(metrics or {}))
        metrics = merged_metrics
        accounting = WorkflowRepository._build_cost_accounting(
            metrics, pricing
        )
        if accounting is None:
            attempt.metrics_json = metrics
            return metrics

        attempt.estimated_cost = accounting["estimated_cost"]
        attempt.cost_currency = accounting["snapshot"]["currency"]
        attempt.pricing_model_id = accounting["snapshot"]["model_id"]
        attempt.pricing_model_version = accounting["snapshot"]["model_version"]
        attempt.pricing_snapshot_json = accounting["snapshot"]
        metrics["cost_accounting"] = accounting["breakdown"]
        attempt.metrics_json = metrics
        return metrics

    def finish_stage(
            self,
            job_id,
            stage_key,
            attempt_id,
            artifacts=None,
            metrics=None,
            pricing=None,
            lease_token=None):
        """Complete the durable stage after publication has been verified."""
        artifacts = list(artifacts or [])
        metrics = dict(metrics or {})
        with self._session.begin() as session:
            stage = self._find_stage(session, job_id, stage_key, lock=True)
            attempt = session.get(AttemptRecord, attempt_id)
            if attempt is None or attempt.stage_id != stage.id:
                raise ValueError("Attempt does not belong to workflow stage")
            job = session.execute(
                select(JobRecord).where(
                    JobRecord.id == job_id
                ).with_for_update()
            ).scalar_one_or_none()
            if job is None:
                raise InvalidStateTransition("Workflow Job no longer exists")
            if attempt.state != AttemptState.RUNNING.value:
                if attempt.state == AttemptState.SUCCEEDED.value and \
                        stage.status == StageState.FINISHED.value:
                    return False
                self._assert_attempt_lease(attempt, lease_token)
                raise InvalidStateTransition("Attempt is not running")
            self._assert_attempt_lease(attempt, lease_token)
            previous_status = stage.status
            self._transition(stage, StageState.FINISHED)
            finished_at = utcnow()
            attempt.state = AttemptState.SUCCEEDED.value
            attempt.finished_at = finished_at
            started = attempt.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            attempt.duration_ms = max(
                0, int((finished_at - started).total_seconds() * 1000)
            )
            metrics["duration_ms"] = attempt.duration_ms
            attempt.cpu_seconds = metrics.get("cpu_seconds")
            attempt.peak_memory_bytes = metrics.get("peak_memory_bytes")
            attempt.input_bytes = metrics.get("input_bytes")
            attempt.output_bytes = metrics.get("output_bytes")
            self._apply_attempt_accounting(attempt, metrics, pricing)
            self._clear_attempt_lease(attempt)
            prior_artifact_count = session.execute(
                select(func.count()).select_from(ArtifactRecord).where(
                    ArtifactRecord.stage_id == stage.id,
                    ArtifactRecord.attempt_id == attempt_id,
                )
            ).scalar_one()
            attempt.artifact_count = prior_artifact_count + len(artifacts)
            for item in artifacts:
                session.add(ArtifactRecord(
                    id=item.get("id") or new_id(),
                    job_id=job_id,
                    stage_id=stage.id,
                    attempt_id=attempt_id,
                    artifact_type=item["artifact_type"],
                    logical_name=item["logical_name"],
                    uri=item.get("uri"),
                    media_type=item.get("media_type"),
                    size_bytes=item.get("size_bytes"),
                    checksum_sha256=item.get("checksum_sha256"),
                    status=item.get("status", "AVAILABLE"),
                    metadata_json=item.get("metadata", {}),
                ))

            workflow = session.get(WorkflowRecord, stage.workflow_id)
            stages = session.execute(
                select(StageRecord).where(
                    StageRecord.workflow_id == workflow.id
                )
            ).scalars().all()
            _record_event(
                session,
                job_id,
                workflow.id,
                "STAGE_FINISHED",
                from_state=previous_status,
                to_state=StageState.FINISHED.value,
                stage_id=stage.id,
                attempt_id=attempt_id,
                payload={
                    "artifact_count": len(artifacts),
                    "output_bytes": metrics.get("output_bytes"),
                },
            )
            completion_stages = stages
            if workflow.execution_backend == "legacy_celery":
                # The legacy Celery graph still executes output pipelines as
                # indivisible chains. Its full plan is durably recorded, but
                # internal plan nodes are not individually claimed yet.
                completion_stages = [
                    item for item in stages
                    if item.operation in (
                        "video.output.upload_directory",
                        "video.output.upload_file",
                    )
                ]
            if completion_stages and all(
                    item.status == StageState.FINISHED.value
                    for item in completion_stages):
                workflow.state = WorkflowState.FINISHED.value
                workflow.updated_at = finished_at
                job.state = JobState.FINISHED.value
                job.updated_at = finished_at
                job.version += 1
                _record_event(
                    session,
                    job_id,
                    workflow.id,
                    "JOB_FINISHED",
                    from_state=JobState.RUNNING.value,
                    to_state=JobState.FINISHED.value,
                )
        return True

    def fail_attempt(
            self,
            job_id,
            stage_key,
            attempt_id,
            retryable,
            error_code=None,
            error_message=None,
            metrics=None,
            pricing=None,
            lease_token=None,
            retry_after_seconds=None):
        if retry_after_seconds is not None and (
                isinstance(retry_after_seconds, bool)
                or not isinstance(retry_after_seconds, (int, float))
                or not math.isfinite(float(retry_after_seconds))
                or not 0 <= retry_after_seconds <= 86400):
            raise ValueError("retry_after_seconds must be between 0 and 86400")
        metrics = dict(metrics or {})
        target = StageState.RETRY_WAIT if retryable else StageState.FAILED
        attempt_state = (
            AttemptState.RETRYABLE if retryable else AttemptState.FAILED
        )
        with self._session.begin() as session:
            stage = self._find_stage(session, job_id, stage_key, lock=True)
            attempt = session.get(AttemptRecord, attempt_id)
            if attempt is None or attempt.stage_id != stage.id:
                raise ValueError("Attempt does not belong to workflow stage")
            self._assert_attempt_lease(attempt, lease_token)
            if attempt.state != AttemptState.RUNNING.value:
                return False
            if stage.status in (
                    StageState.FINISHED.value,
                    StageState.FAILED.value,
                    StageState.CANCELLED.value):
                return False
            job = session.execute(
                select(JobRecord).where(
                    JobRecord.id == job_id
                ).with_for_update()
            ).scalar_one_or_none()
            if job is None or job.state in (
                    JobState.CANCELLED.value,
                    JobState.FAILED.value,
                    JobState.FINISHED.value):
                # A sibling may have made the Job terminal while this worker
                # was still running. Fence it rather than scheduling a retry.
                now = utcnow()
                previous_status = stage.status
                self._transition(stage, StageState.CANCELLED)
                attempt.state = AttemptState.CANCELLED.value
                attempt.finished_at = now
                started = attempt.started_at
                if started.tzinfo is None or started.utcoffset() is None:
                    started = started.replace(tzinfo=timezone.utc)
                attempt.duration_ms = max(
                    0, int((now - started).total_seconds() * 1000)
                )
                metrics["duration_ms"] = attempt.duration_ms
                self._apply_attempt_accounting(attempt, metrics, pricing)
                self._clear_attempt_lease(attempt)
                attempt.error_code = "JOB_TERMINAL"
                attempt.error_message = "JOB_TERMINAL_BEFORE_ATTEMPT_FAILURE"
                _record_event(
                    session,
                    job_id,
                    stage.workflow_id,
                    "STAGE_CANCELLED",
                    from_state=previous_status,
                    to_state=StageState.CANCELLED.value,
                    stage_id=stage.id,
                    attempt_id=attempt_id,
                    payload={"reason": "JOB_TERMINAL_BEFORE_ATTEMPT_FAILURE"},
                )
                return False
            previous_status = stage.status
            self._transition(stage, target)
            attempt.state = attempt_state.value
            attempt.finished_at = utcnow()
            started = attempt.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            attempt.duration_ms = max(
                0, int((attempt.finished_at - started).total_seconds() * 1000)
            )
            metrics["duration_ms"] = attempt.duration_ms
            attempt.cpu_seconds = metrics.get("cpu_seconds")
            attempt.peak_memory_bytes = metrics.get("peak_memory_bytes")
            attempt.input_bytes = metrics.get("input_bytes")
            attempt.output_bytes = metrics.get("output_bytes")
            self._apply_attempt_accounting(attempt, metrics, pricing)
            self._clear_attempt_lease(attempt)
            attempt.error_code = error_code
            attempt.error_message = error_message
            workflow = session.get(WorkflowRecord, stage.workflow_id)
            if retryable and workflow.execution_backend == "durable_vod_v1":
                plan_delay = _durable_retry_delay(
                    workflow, stage.stage_key, attempt.attempt_number
                )
                if (
                        retry_after_seconds is not None
                        and not math.isclose(
                            float(retry_after_seconds),
                            plan_delay,
                            rel_tol=0.0,
                            abs_tol=1e-9,
                        )):
                    raise ValueError(
                        "retry_after_seconds must match the immutable plan policy"
                    )
                retry_not_before = _durable_retry_not_before(
                    workflow, stage.stage_key, attempt
                )
                if retry_not_before is None:
                    raise InvalidStateTransition(
                        "Durable retry requires a persisted attempt completion time"
                    )
                _enqueue_durable_dispatch(
                    session,
                    workflow,
                    job_id,
                    attempt_id,
                    stage.id,
                    retry_not_before,
                    reason="retry",
                    event_type="WORKFLOW_RETRY_ENQUEUED",
                )
            if not retryable:
                workflow.state = WorkflowState.FAILED.value
                workflow.updated_at = utcnow()
                job = session.get(JobRecord, job_id)
                job.state = JobState.FAILED.value
                job.updated_at = utcnow()
                job.version += 1
                _record_event(
                    session,
                    job_id,
                    workflow.id,
                    "JOB_FAILED",
                    from_state=JobState.RUNNING.value,
                    to_state=JobState.FAILED.value,
                    stage_id=stage.id,
                    attempt_id=attempt_id,
                    payload={"error_code": error_code},
                )
            _record_event(
                session,
                job_id,
                workflow.id,
                "ATTEMPT_RETRYABLE" if retryable else "STAGE_FAILED",
                from_state=previous_status,
                to_state=target.value,
                stage_id=stage.id,
                attempt_id=attempt_id,
                payload={"error_code": error_code},
            )
        return True

    def cancel_attempt(
            self,
            job_id,
            stage_key,
            attempt_id,
            reason="CANCELLED",
            metrics=None,
            pricing=None,
            lease_token=None):
        """Persist cancellation of a running attempt and its workflow stage."""
        with self._session.begin() as session:
            stage = self._find_stage(session, job_id, stage_key, lock=True)
            attempt = session.get(AttemptRecord, attempt_id)
            if attempt is None or attempt.stage_id != stage.id:
                raise ValueError("Attempt does not belong to workflow stage")
            self._assert_attempt_lease(attempt, lease_token)
            if attempt.state != AttemptState.RUNNING.value:
                return False
            job = session.execute(
                select(JobRecord).where(
                    JobRecord.id == job_id
                ).with_for_update()
            ).scalar_one_or_none()
            if job is None:
                raise InvalidStateTransition("Workflow Job no longer exists")
            if StageState.CANCELLED not in ALLOWED_STAGE_TRANSITIONS[
                    StageState(stage.status)]:
                raise InvalidStateTransition(
                    "Cannot cancel stage {} from {}".format(
                        stage_key, stage.status
                    )
                )
            now = utcnow()
            previous_status = stage.status
            self._transition(stage, StageState.CANCELLED)
            started = attempt.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            attempt.state = AttemptState.CANCELLED.value
            attempt.finished_at = now
            attempt.duration_ms = max(
                0, int((now - started).total_seconds() * 1000)
            )
            metrics = dict(metrics or {})
            metrics["duration_ms"] = attempt.duration_ms
            attempt.cpu_seconds = metrics.get("cpu_seconds")
            attempt.peak_memory_bytes = metrics.get("peak_memory_bytes")
            attempt.input_bytes = metrics.get("input_bytes")
            attempt.output_bytes = metrics.get("output_bytes")
            self._apply_attempt_accounting(attempt, metrics, pricing)
            self._clear_attempt_lease(attempt)
            attempt.error_code = "CANCELLED"
            attempt.error_message = reason
            workflow = session.get(WorkflowRecord, stage.workflow_id)
            # A sibling may already have failed the Job (or the workflow may
            # have reached another terminal state). Stop this Attempt without
            # rewriting that authoritative terminal outcome as CANCELLED.
            if job.state not in (
                    JobState.CANCELLED.value,
                    JobState.FAILED.value,
                    JobState.FINISHED.value):
                workflow.state = WorkflowState.CANCELLED.value
                workflow.updated_at = now
                job.state = JobState.CANCELLED.value
                job.updated_at = now
                job.version += 1
            _record_event(
                session,
                job_id,
                workflow.id,
                "STAGE_CANCELLED",
                from_state=previous_status,
                to_state=StageState.CANCELLED.value,
                stage_id=stage.id,
                attempt_id=attempt_id,
                payload={"reason": reason},
            )
        return True


    def recover_expired_attempts(self, now=None, pricing=None, limit=100):
        """Recover only running attempts whose durable worker lease has expired.

        Candidate discovery is unlocked. Mutations lock Stage then Attempt,
        matching claim/heartbeat/completion order and allowing safe pollers.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        now = now or utcnow()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        recovered = 0
        with self._session.begin() as session:
            candidates = session.execute(
                select(AttemptRecord.id, AttemptRecord.stage_id).where(
                    AttemptRecord.state == AttemptState.RUNNING.value,
                    AttemptRecord.lease_token_hash.is_not(None),
                    AttemptRecord.lease_expires_at <= now,
                ).order_by(
                    AttemptRecord.lease_expires_at,
                    AttemptRecord.id,
                ).limit(limit)
            ).all()

            for attempt_id, stage_id in candidates:
                stage = session.execute(
                    select(StageRecord).where(
                        StageRecord.id == stage_id
                    ).with_for_update(skip_locked=True)
                ).scalar_one_or_none()
                if stage is None:
                    continue
                attempt = session.execute(
                    select(AttemptRecord).where(
                        AttemptRecord.id == attempt_id
                    ).with_for_update().execution_options(
                        populate_existing=True
                    )
                ).scalar_one_or_none()
                if attempt is None:
                    continue
                if self._recover_expired_attempt_in_session(
                        session, stage, attempt, now, pricing):
                    recovered += 1
        return recovered

    def recover_stale_attempts(self, older_than, pricing=None, limit=100):
        """Recover legacy non-leased attempts by age without racing lease owners."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if older_than.tzinfo is None or older_than.utcoffset() is None:
            raise ValueError("older_than must be timezone-aware")
        recovered = 0
        now = utcnow()
        with self._session.begin() as session:
            candidates = session.execute(
                select(AttemptRecord.id, AttemptRecord.stage_id).where(
                    AttemptRecord.state == AttemptState.RUNNING.value,
                    AttemptRecord.started_at < older_than,
                    AttemptRecord.lease_token_hash.is_(None),
                ).order_by(
                    AttemptRecord.started_at,
                    AttemptRecord.id,
                ).limit(limit)
            ).all()

            for attempt_id, stage_id in candidates:
                stage = session.execute(
                    select(StageRecord).where(
                        StageRecord.id == stage_id
                    ).with_for_update(skip_locked=True)
                ).scalar_one_or_none()
                if stage is None:
                    continue
                attempt = session.execute(
                    select(AttemptRecord).where(
                        AttemptRecord.id == attempt_id
                    ).with_for_update().execution_options(
                        populate_existing=True
                    )
                ).scalar_one_or_none()
                if (
                        attempt is None
                        or attempt.state != AttemptState.RUNNING.value
                        or attempt.lease_token_hash is not None
                        or attempt.started_at >= older_than):
                    continue
                if stage.status not in (
                        StageState.PROCESSING.value,
                        StageState.VALIDATED.value,
                        StageState.PUBLISHING.value):
                    continue
                previous_status = stage.status
                self._transition(stage, StageState.RETRY_WAIT)
                attempt.state = AttemptState.RETRYABLE.value
                attempt.finished_at = now
                started = attempt.started_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                attempt.duration_ms = max(
                    0, int((now - started).total_seconds() * 1000)
                )
                metrics = {
                    "duration_ms": attempt.duration_ms,
                    "usage_capture": "incomplete_worker_lost",
                }
                self._apply_attempt_accounting(attempt, metrics, pricing)
                attempt.error_code = "WORKER_LOST"
                attempt.error_message = "Recovered stale running attempt"
                workflow = session.get(WorkflowRecord, stage.workflow_id)
                _record_event(
                    session,
                    workflow.job_id,
                    workflow.id,
                    "ATTEMPT_RECOVERED",
                    from_state=previous_status,
                    to_state=StageState.RETRY_WAIT.value,
                    stage_id=stage.id,
                    attempt_id=attempt.id,
                    payload={"reason": "WORKER_LOST"},
                )
                recovered += 1
        return recovered

    def is_stage_finished(self, job_id, stage_key):
        with self._session() as session:
            stage = self._find_stage(session, job_id, stage_key)
            return stage.status == StageState.FINISHED.value



    def start_playback_session(
            self,
            job_id,
            stage_key,
            client_version=""):
        """Create an opaque playback capability bound to a finished manifest."""
        session_id = new_id()
        session_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
        now = utcnow()
        with self._session.begin() as session:
            stage = self._find_stage(session, job_id, stage_key)
            if stage.status != StageState.FINISHED.value:
                raise ValueError("Playback can start only for a finished output")

            manifest = session.execute(
                select(ArtifactRecord).where(
                    ArtifactRecord.stage_id == stage.id,
                    ArtifactRecord.artifact_type == "streaming_manifest",
                    ArtifactRecord.status == "AVAILABLE",
                ).order_by(ArtifactRecord.created_at.desc())
            ).scalars().first()
            if manifest is None or not manifest.uri:
                raise ValueError(
                    "Finished output has no available streaming manifest artifact"
                )

            session.add(PlaybackSessionRecord(
                id=session_id,
                job_id=job_id,
                stage_id=stage.id,
                manifest_artifact_id=manifest.id,
                token_hash=token_hash,
                client_version=(client_version or "")[:64],
                status="ACTIVE",
                started_at=now,
                last_event_at=now,
            ))
        return {
            "session_id": session_id,
            "session_token": session_token,
            "manifest_artifact_id": manifest.id,
        }

    @staticmethod
    def _playback_session_summary(record):
        return {
            "session_id": record.id,
            "status": record.status,
            "startup_ms": record.startup_ms,
            "playback_started_at": (
                record.playback_started_at.isoformat()
                if record.playback_started_at else None
            ),
            "rebuffer_count": record.rebuffer_count,
            "total_buffering_ms": record.total_buffering_ms,
            "decoder_error_count": record.decoder_error_count,
            "started_at": record.started_at.isoformat(),
            "last_event_at": record.last_event_at.isoformat(),
            "ended_at": (
                record.ended_at.isoformat() if record.ended_at else None
            ),
        }

    def record_playback_event(
            self,
            session_id,
            session_token,
            event_id,
            event_type,
            startup_ms=None,
            error_code=None,
            decoder=None,
            client_version=None):
        """Append an idempotent event and update session aggregates atomically.

        Client clocks are deliberately not trusted: server receive time is the
        canonical clock for buffering intervals and ordering.
        """
        if not event_id or len(event_id) > 128:
            raise ValueError("event_id must contain 1 to 128 characters")
        if not session_token:
            raise PermissionError("A playback session token is required")
        event_type = str(event_type or "").upper()
        allowed = {
            "PLAYBACK_STARTED",
            "BUFFERING_STARTED",
            "BUFFERING_ENDED",
            "DECODER_ERROR",
            "PLAYBACK_ENDED",
        }
        if event_type not in allowed:
            raise ValueError("Unsupported playback event type")
        if startup_ms is not None and not 0 <= int(startup_ms) <= 86400000:
            raise ValueError("startup_ms must be between 0 and 86400000")
        if error_code and len(error_code) > 128:
            raise ValueError("error_code must be at most 128 characters")

        metadata = {}
        if decoder:
            metadata["decoder"] = str(decoder)[:128]
        if client_version:
            metadata["client_version"] = str(client_version)[:64]
        canonical_payload = {
            "event_type": event_type,
            "startup_ms": (
                int(startup_ms)
                if event_type == "PLAYBACK_STARTED" and startup_ms is not None
                else None
            ),
            "error_code": error_code or None,
            "metadata": metadata,
        }
        payload_hash = hashlib.sha256(
            json.dumps(
                canonical_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()

        now = utcnow()
        token_hash = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
        expired = False
        result = None
        with self._session.begin() as session:
            playback = session.execute(
                select(PlaybackSessionRecord).where(
                    PlaybackSessionRecord.id == session_id
                ).with_for_update()
            ).scalar_one_or_none()
            if playback is None:
                raise KeyError("Playback session was not found")
            if not hmac.compare_digest(playback.token_hash, token_hash):
                raise PermissionError("Playback session token is invalid")

            duplicate = session.execute(
                select(PlaybackEventRecord).where(
                    PlaybackEventRecord.session_id == session_id,
                    PlaybackEventRecord.event_id == event_id,
                )
            ).scalar_one_or_none()
            if duplicate is not None:
                if duplicate.event_type != event_type:
                    raise ValueError(
                        "event_id was already used for a different event type"
                    )
                if not hmac.compare_digest(
                        duplicate.payload_hash, payload_hash):
                    raise ValueError(
                        "event_id was already used with a different payload"
                    )
                return {
                    **self._playback_session_summary(playback),
                    "duplicate": True,
                }

            if playback.status != "ACTIVE":
                raise ValueError("Playback session is already ended")
            if now - playback.started_at > timedelta(hours=24):
                playback.status = "EXPIRED"
                playback.ended_at = now
                playback.last_event_at = now
                expired = True
            else:
                duration_ms = None
                if client_version and not playback.client_version:
                    playback.client_version = metadata["client_version"]

                if event_type == "PLAYBACK_STARTED":
                    if playback.startup_ms is not None:
                        raise ValueError("PLAYBACK_STARTED was already recorded")
                    if startup_ms is None:
                        raise ValueError("PLAYBACK_STARTED requires startup_ms")
                    playback.startup_ms = int(startup_ms)
                    playback.playback_started_at = now

                elif event_type == "BUFFERING_STARTED":
                    if playback.startup_ms is None:
                        raise ValueError("Playback must start before buffering")
                    if playback.buffering_started_at is not None:
                        raise ValueError("A buffering interval is already active")
                    playback.buffering_started_at = now

                elif event_type == "BUFFERING_ENDED":
                    if playback.buffering_started_at is None:
                        raise ValueError("BUFFERING_ENDED has no matching start")
                    duration_ms = max(
                        0,
                        int(
                            (now - playback.buffering_started_at)
                            .total_seconds() * 1000
                        ),
                    )
                    playback.rebuffer_count += 1
                    playback.total_buffering_ms += duration_ms
                    playback.buffering_started_at = None

                elif event_type == "DECODER_ERROR":
                    if not error_code:
                        raise ValueError("DECODER_ERROR requires error_code")
                    playback.decoder_error_count += 1

                elif event_type == "PLAYBACK_ENDED":
                    if playback.startup_ms is None:
                        raise ValueError("Playback must start before it can end")
                    if playback.buffering_started_at is not None:
                        duration_ms = max(
                            0,
                            int(
                                (now - playback.buffering_started_at)
                                .total_seconds() * 1000
                            ),
                        )
                        playback.rebuffer_count += 1
                        playback.total_buffering_ms += duration_ms
                        playback.buffering_started_at = None
                    playback.status = "ENDED"
                    playback.ended_at = now

                playback.last_event_at = now
                session.add(PlaybackEventRecord(
                    id=new_id(),
                    session_id=session_id,
                    event_id=event_id,
                    event_type=event_type,
                    payload_hash=payload_hash,
                    received_at=now,
                    duration_ms=duration_ms,
                    startup_ms=(
                        int(startup_ms)
                        if event_type == "PLAYBACK_STARTED" else None
                    ),
                    error_code=(error_code or None),
                    metadata_json=metadata,
                ))
                result = {
                    **self._playback_session_summary(playback),
                    "duplicate": False,
                }

        if expired:
            raise ValueError("Playback session has expired")
        return result

    def get_playback_qoe_report(self, job_id, stage_key):
        """Aggregate playback QoE beside the same stage's resource/cost data."""
        now = utcnow()
        with self._session() as session:
            stage = self._find_stage(session, job_id, stage_key)
            sessions = session.execute(
                select(PlaybackSessionRecord).where(
                    PlaybackSessionRecord.stage_id == stage.id
                ).order_by(PlaybackSessionRecord.started_at)
            ).scalars().all()
            attempts = session.execute(
                select(AttemptRecord).where(
                    AttemptRecord.stage_id == stage.id
                ).order_by(AttemptRecord.attempt_number)
            ).scalars().all()

            startup_values = [
                int(item.startup_ms)
                for item in sessions if item.startup_ms is not None
            ]
            rebuffer_count = sum(
                item.rebuffer_count
                + (1 if item.buffering_started_at is not None else 0)
                for item in sessions
            )
            buffering_ms = sum(
                item.total_buffering_ms
                + (
                    max(
                        0,
                        int((now - item.buffering_started_at)
                            .total_seconds() * 1000),
                    )
                    if item.buffering_started_at is not None else 0
                )
                for item in sessions
            )
            decoder_errors = sum(
                item.decoder_error_count for item in sessions
            )
            wall_time_ms = 0
            for item in sessions:
                if item.playback_started_at is None:
                    continue
                end_time = item.ended_at or now
                wall_time_ms += max(
                    0, int(
                        (end_time - item.playback_started_at)
                        .total_seconds() * 1000
                    )
                )
            buffering_ratio = (
                (buffering_ms / wall_time_ms * 100.0)
                if wall_time_ms > 0 else None
            )

            cpu_seconds = sum(
                float(item.cpu_seconds or 0.0) for item in attempts
            )
            priced_attempts = [
                item for item in attempts if item.estimated_cost is not None
            ]
            cost_currencies = {item.cost_currency for item in priced_attempts}
            pricing_models = {item.pricing_model_id for item in priced_attempts}
            pricing_versions = {
                item.pricing_model_version for item in priced_attempts
            }
            complete_attempts = [
                item for item in attempts
                if item.estimated_cost is not None
                and item.pricing_snapshot_json is not None
                and item.metrics_json.get("cost_accounting", {})
                    .get("coverage", {}).get("complete") is True
            ]
            missing_dimension_counts = {}
            for item in attempts:
                coverage = item.metrics_json.get(
                    "cost_accounting", {}
                ).get("coverage", {})
                for dimension in coverage.get("missing_dimensions", []):
                    missing_dimension_counts[dimension] = (
                        missing_dimension_counts.get(dimension, 0) + 1
                    )
            excluded_dimensions = sorted({
                dimension
                for item in attempts
                for dimension in item.metrics_json.get(
                    "cost_accounting", {}
                ).get("coverage", {}).get("excluded_dimensions", [])
            })
            cost_estimate_complete = (
                bool(attempts)
                and len(priced_attempts) == len(attempts)
                and len(complete_attempts) == len(attempts)
                and len(cost_currencies) == 1
                and None not in cost_currencies
                and len(pricing_models) == 1
                and None not in pricing_models
                and len(pricing_versions) == 1
                and None not in pricing_versions
            )
            partial_estimated_cost = (
                sum(float(item.estimated_cost) for item in priced_attempts)
                if priced_attempts else None
            )
            cost_coverage = {
                "complete": cost_estimate_complete,
                "priced_attempt_count": len(priced_attempts),
                "complete_attempt_count": len(complete_attempts),
                "total_attempt_count": len(attempts),
                "missing_dimensions_by_attempt": missing_dimension_counts,
                "excluded_dimensions": excluded_dimensions,
                "pricing_model_versions": sorted(
                    str(value) for value in pricing_versions if value is not None
                ),
            }
            report = {
                "job_id": job_id,
                "stage_key": stage_key,
                "stage_status": stage.status,
                "session_count": len(sessions),
                "playback_started_count": len(startup_values),
                "average_startup_ms": (
                    sum(startup_values) / len(startup_values)
                    if startup_values else None
                ),
                "rebuffer_count": rebuffer_count,
                "total_buffering_ms": buffering_ms,
                "decoder_error_count": decoder_errors,
                "buffering_ratio_percent": buffering_ratio,
                "total_cpu_seconds": cpu_seconds,
                "total_estimated_cost": (
                    partial_estimated_cost if cost_estimate_complete else None
                ),
                "partial_estimated_cost": partial_estimated_cost,
                "cost_estimate_complete": cost_estimate_complete,
                "cost_coverage": cost_coverage,
                "priced_attempt_count": len(priced_attempts),
                "total_attempt_count": len(attempts),
                "cost_currency": (
                    next(iter(cost_currencies))
                    if cost_estimate_complete else None
                ),
                "pricing_model_id": (
                    next(iter(pricing_models))
                    if cost_estimate_complete else None
                ),
                "sessions": [
                    self._playback_session_summary(item)
                    for item in sessions
                ],
            }
            return report


    def mark_package_attempt_cleaned(
            self, attempt_id, bucket, attempt_prefix, now=None):
        """Persist successful cleanup so the janitor does not rescan it."""
        now = now or utcnow()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        with self._session.begin() as session:
            attempt = session.get(
                AttemptRecord, attempt_id, with_for_update=True
            )
            if attempt is None:
                return False
            stored_metrics = dict(attempt.metrics_json or {})
            namespace = dict(
                stored_metrics.get("package_publication") or {}
            )
            if (
                    not namespace
                    or namespace.get("bucket") != bucket
                    or namespace.get("prefix") != attempt_prefix):
                raise InvalidStateTransition(
                    "Cleanup namespace does not match the recorded Attempt"
                )
            if attempt.state not in (
                    AttemptState.FAILED.value,
                    AttemptState.CANCELLED.value,
                    AttemptState.RETRYABLE.value):
                raise InvalidStateTransition(
                    "Only abandoned package Attempts can be marked cleaned"
                )
            if any((
                    attempt.lease_owner,
                    attempt.lease_token_hash,
                    attempt.lease_expires_at,
                    attempt.heartbeat_at)):
                raise AttemptLeaseLost(
                    "Cannot mark an Attempt cleaned while it has a lease"
                )
            namespace["cleanup_completed_at"] = now.isoformat()
            stored_metrics["package_publication"] = namespace
            attempt.metrics_json = stored_metrics
            return True


    def list_reclaimable_package_attempts(
            self, minimum_age_seconds=86400, limit=100, now=None,
            attempt_ids=None):
        """Return old package Attempts whose exact namespaces pass DB guards."""
        if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 1 <= limit <= 1000):
            raise ValueError("limit must be between 1 and 1000")
        if (
                isinstance(minimum_age_seconds, bool)
                or not isinstance(minimum_age_seconds, (int, float))
                or not math.isfinite(float(minimum_age_seconds))
                or minimum_age_seconds <= 0):
            raise ValueError("minimum_age_seconds must be finite and positive")
        now = now or utcnow()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        cutoff = now - timedelta(seconds=float(minimum_age_seconds))
        if attempt_ids is not None:
            attempt_ids = tuple(attempt_ids)
            if any(not isinstance(item, str) or not item for item in attempt_ids):
                raise ValueError("attempt_ids must contain non-empty strings")
            if not attempt_ids:
                return []

        with self._session() as session:
            query = select(AttemptRecord).where(
                    AttemptRecord.state.in_([
                        AttemptState.FAILED.value,
                        AttemptState.CANCELLED.value,
                        AttemptState.RETRYABLE.value,
                    ]),
                    AttemptRecord.finished_at <= cutoff,
                    AttemptRecord.metrics_json["package_publication"].is_not(None),
                    # Exclude completed cleanup before LIMIT. Filtering only
                    # after the SQL limit can permanently starve later rows.
                    AttemptRecord.metrics_json["package_publication"][
                        "cleanup_completed_at"
                    ].as_string().is_(None),
                )
            if attempt_ids is not None:
                query = query.where(AttemptRecord.id.in_(attempt_ids))
            candidates = session.execute(
                query.order_by(AttemptRecord.finished_at.asc()).limit(limit)
            ).scalars().all()
            metadata = []
            for attempt in candidates:
                namespace = (
                    (attempt.metrics_json or {}).get("package_publication")
                )
                if (
                        not isinstance(namespace, dict)
                        or namespace.get("cleanup_completed_at")):
                    continue
                required = ("bucket", "prefix", "logical_key", "kind")
                if not all(
                        isinstance(namespace.get(key), str)
                        and namespace.get(key)
                        for key in required):
                    continue
                if namespace["kind"] == "package":
                    if not namespace.get("inventory_key"):
                        continue
                elif namespace["kind"] == "single_file":
                    if not namespace.get("object_key"):
                        continue
                else:
                    continue
                metadata.append({
                    "attempt_id": attempt.id,
                    "bucket": namespace["bucket"],
                    "prefix": namespace["prefix"],
                    "kind": namespace["kind"],
                    "inventory_key": namespace.get("inventory_key"),
                    "object_key": namespace.get("object_key"),
                    "logical_key": namespace["logical_key"],
                })

        reclaimable = []
        for item in metadata:
            if self.is_attempt_reclaimable(
                    item["attempt_id"],
                    item["bucket"],
                    item["prefix"],
                    minimum_age_seconds=minimum_age_seconds,
                    now=now):
                reclaimable.append(item)
                if len(reclaimable) >= limit:
                    break
        return reclaimable


    def is_attempt_reclaimable(
            self, attempt_id, bucket, attempt_prefix,
            minimum_age_seconds=86400, now=None):
        """Return whether a failed package Attempt is safe for orphan cleanup.

        FAILED/CANCELLED attempts and superseded RETRYABLE attempts qualify
        only after the grace period. The exact bucket/prefix must match the
        namespace recorded on the Attempt, and any AVAILABLE Artifact under
        that prefix blocks deletion.
        """
        if (
                isinstance(minimum_age_seconds, bool)
                or not isinstance(minimum_age_seconds, (int, float))
                or not math.isfinite(float(minimum_age_seconds))
                or minimum_age_seconds <= 0):
            raise ValueError("minimum_age_seconds must be finite and positive")
        if not bucket or not isinstance(bucket, str):
            raise ValueError("bucket is required")
        if (
                not isinstance(attempt_prefix, str)
                or not attempt_prefix
                or attempt_prefix.startswith("/")
                or "\\" in attempt_prefix
                or any(part in ("", ".", "..")
                       for part in attempt_prefix.split("/"))):
            raise ValueError("attempt_prefix must be a safe relative key prefix")

        now = now or utcnow()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        prefix_parts = attempt_prefix.split("/")
        if (
                len(prefix_parts) < 2
                or prefix_parts[-2] != ".attempts"
                or prefix_parts[-1] != attempt_id):
            raise ValueError(
                "attempt_prefix must end with .attempts/<attempt_id>"
            )
        uri_prefix = "s3://{}/{}/".format(bucket, attempt_prefix.rstrip("/"))

        with self._session() as session:
            attempt = session.get(AttemptRecord, attempt_id)
            if attempt is None:
                return False
            stage = session.get(StageRecord, attempt.stage_id)
            if stage is None:
                return False
            namespace = (
                (attempt.metrics_json or {}).get("package_publication")
            )
            if (
                    not isinstance(namespace, dict)
                    or namespace.get("bucket") != bucket
                    or namespace.get("prefix") != attempt_prefix):
                return False
            if attempt.state not in (
                    AttemptState.FAILED.value,
                    AttemptState.CANCELLED.value,
                    AttemptState.RETRYABLE.value):
                return False
            if attempt.state == AttemptState.RETRYABLE.value:
                newer_attempt = session.execute(
                    select(AttemptRecord.id).where(
                        AttemptRecord.stage_id == attempt.stage_id,
                        AttemptRecord.attempt_number > attempt.attempt_number,
                    ).limit(1)
                ).scalar_one_or_none()
                if (
                        newer_attempt is None
                        and stage.status not in (
                            StageState.FINISHED.value,
                            StageState.FAILED.value,
                            StageState.CANCELLED.value)):
                    return False
            if any((
                    attempt.lease_owner,
                    attempt.lease_token_hash,
                    attempt.lease_expires_at,
                    attempt.heartbeat_at)):
                return False
            finished_at = attempt.finished_at
            if finished_at is None:
                return False
            if finished_at.tzinfo is None:
                finished_at = finished_at.replace(tzinfo=timezone.utc)
            if now - finished_at < timedelta(
                    seconds=float(minimum_age_seconds)):
                return False

            referenced = session.execute(
                select(ArtifactRecord.id).where(
                    ArtifactRecord.status == "AVAILABLE",
                    func.substr(
                        ArtifactRecord.uri, 1, len(uri_prefix)
                    ) == uri_prefix,
                ).limit(1)
            ).scalar_one_or_none()
            return referenced is None


    def get_attempt_publication_namespace(self, attempt_id):
        with self._session() as session:
            attempt = session.get(AttemptRecord, attempt_id)
            if attempt is None:
                return None
            namespace = (attempt.metrics_json or {}).get("package_publication")
            return dict(namespace) if isinstance(namespace, dict) else None


    def get_attempt_local_package(self, attempt_id):
        """Read internal shared-volume handoff metadata for a processing Attempt."""
        with self._session() as session:
            attempt = session.get(AttemptRecord, attempt_id)
            if attempt is None:
                return None
            package = (attempt.metrics_json or {}).get("local_package")
            return dict(package) if isinstance(package, dict) else None


    def requeue_processing_after_local_package_loss(
            self, job_id, stage_key, processing_attempt_id, reason):
        """Invalidate a local-only validation handoff and safely reprocess it."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason is required")
        now = utcnow()
        with self._session.begin() as session:
            stage = self._find_stage(session, job_id, stage_key, lock=True)
            if stage.status not in (
                    StageState.VALIDATED.value,
                    StageState.RETRY_WAIT.value):
                raise InvalidStateTransition(
                    "Local package loss can only requeue a validated stage"
                )
            processing_attempt = session.get(
                AttemptRecord, processing_attempt_id, with_for_update=True
            )
            if (
                    processing_attempt is None
                    or processing_attempt.stage_id != stage.id
                    or processing_attempt.state != AttemptState.SUCCEEDED.value):
                raise InvalidStateTransition(
                    "Requeue requires the successful processing Attempt"
                )
            active = session.execute(
                select(AttemptRecord.id).where(
                    AttemptRecord.stage_id == stage.id,
                    AttemptRecord.state == AttemptState.RUNNING.value,
                ).limit(1)
            ).scalar_one_or_none()
            if active is not None:
                raise StageAlreadyRunning(
                    "Cannot requeue while another Attempt is active"
                )

            previous_status = stage.status
            validation_artifact = session.execute(
                select(ArtifactRecord).where(
                    ArtifactRecord.stage_id == stage.id,
                    ArtifactRecord.attempt_id == processing_attempt_id,
                    ArtifactRecord.artifact_type == "validation_report",
                    ArtifactRecord.logical_name == "validation-report",
                    ArtifactRecord.status == "AVAILABLE",
                ).with_for_update()
            ).scalars().first()
            if validation_artifact is None:
                raise InvalidStateTransition(
                    "The successful processing Attempt has no available "
                    "validation proof to invalidate"
                )
            other_available = session.execute(
                select(ArtifactRecord.id).where(
                    ArtifactRecord.stage_id == stage.id,
                    ArtifactRecord.status == "AVAILABLE",
                    ArtifactRecord.id != validation_artifact.id,
                ).limit(1)
            ).scalar_one_or_none()
            if other_available is not None:
                raise InvalidStateTransition(
                    "Cannot reprocess after another Artifact was published"
                )

            metadata = dict(validation_artifact.metadata_json or {})
            metadata["invalidated_reason"] = reason.strip()[:500]
            metadata["invalidated_at"] = now.isoformat()
            validation_artifact.metadata_json = metadata
            validation_artifact.status = "UNAVAILABLE"
            if stage.status != StageState.RETRY_WAIT.value:
                self._transition(stage, StageState.RETRY_WAIT)
            else:
                stage.version += 1
                stage.updated_at = now

            job = session.execute(
                select(JobRecord).where(
                    JobRecord.id == job_id
                ).with_for_update()
            ).scalar_one_or_none()
            if job is None:
                raise InvalidStateTransition("Workflow Job no longer exists")
            if job.state in (
                    JobState.CANCELLED.value,
                    JobState.FAILED.value,
                    JobState.FINISHED.value):
                raise InvalidStateTransition(
                    "A terminal Job cannot recover a lost local package"
                )
            workflow = session.get(WorkflowRecord, stage.workflow_id)
            workflow.state = WorkflowState.RUNNING.value
            workflow.updated_at = now
            job.state = JobState.RUNNING.value
            job.updated_at = now
            job.version += 1
            _record_event(
                session,
                job_id,
                workflow.id,
                "PROCESSING_REQUEUED_AFTER_LOCAL_PACKAGE_LOSS",
                from_state=previous_status,
                to_state=StageState.RETRY_WAIT.value,
                stage_id=stage.id,
                attempt_id=processing_attempt_id,
                payload={"reason": reason.strip()[:500]},
            )
            return True


    def get_stage_snapshot(self, job_id, stage_key):
        """Return one durable stage, its attempts and artifacts."""
        snapshot = self.get_job_snapshot(job_id)
        if snapshot is None:
            return None
        for stage in snapshot["stages"]:
            if stage["key"] == stage_key:
                return stage
        return None


    def get_job_snapshot(self, job_id):
        with self._session() as session:
            job = session.get(JobRecord, job_id)
            if job is None:
                return None
            workflow = session.execute(
                select(WorkflowRecord).where(
                    WorkflowRecord.job_id == job_id
                )
            ).scalar_one()
            stages = session.execute(
                select(StageRecord).where(
                    StageRecord.workflow_id == workflow.id
                ).order_by(StageRecord.ordinal)
            ).scalars().all()
            events = session.execute(
                select(WorkflowEventRecord).where(
                    WorkflowEventRecord.job_id == job_id
                ).order_by(WorkflowEventRecord.occurred_at, WorkflowEventRecord.id)
            ).scalars().all()
            self._verify_execution_plan_snapshot(workflow, job_id)
            snapshot = {
                "id": job.id,
                "idempotency_key": job.idempotency_key,
                "request_hash": job.request_hash,
                "reference_id": job.reference_id,
                "metadata": job.metadata_json,
                "status": job.state,
                "workflow": {
                    "id": workflow.id,
                    "name": workflow.name,
                    "status": workflow.state,
                    "execution_backend": workflow.execution_backend,
                    "version": workflow.workflow_version,
                    "execution_plan_schema_version": workflow.execution_plan_schema_version,
                    "execution_plan_fingerprint": workflow.execution_plan_fingerprint,
                },
                "stages": [],
                "events": [{
                    "id": event.id,
                    "type": event.event_type,
                    "from_state": event.from_state,
                    "to_state": event.to_state,
                    "occurred_at": event.occurred_at.isoformat(),
                    "stage_id": event.stage_id,
                    "attempt_id": event.attempt_id,
                    "payload": event.payload_json,
                } for event in events],
            }
            for stage in stages:
                attempts = session.execute(
                    select(AttemptRecord).where(
                        AttemptRecord.stage_id == stage.id
                    ).order_by(AttemptRecord.attempt_number)
                ).scalars().all()
                artifacts = session.execute(
                    select(ArtifactRecord).where(
                        ArtifactRecord.stage_id == stage.id
                    ).order_by(ArtifactRecord.created_at)
                ).scalars().all()
                snapshot["stages"].append({
                    "id": stage.id,
                    "key": stage.stage_key,
                    "operation": stage.operation,
                    "status": stage.status,
                    "version": stage.version,
                    "input": stage.input_contract_json,
                    "output": stage.output_contract_json,
                    "attempts": [{
                        "id": item.id,
                        "number": item.attempt_number,
                        "status": item.state,
                        "backend": item.execution_backend,
                        "execution_ref": item.execution_ref,
                        "duration_ms": item.duration_ms,
                        "cpu_seconds": item.cpu_seconds,
                        "peak_memory_bytes": item.peak_memory_bytes,
                        "input_bytes": item.input_bytes,
                        "output_bytes": item.output_bytes,
                        "artifact_count": item.artifact_count,
                        "estimated_cost": (
                            float(item.estimated_cost)
                            if item.estimated_cost is not None else None
                        ),
                        "cost_currency": item.cost_currency,
                        "pricing_model_id": item.pricing_model_id,
                        "pricing_model_version": item.pricing_model_version,
                        "pricing_snapshot": item.pricing_snapshot_json,
                        "cost_accounting": item.metrics_json.get(
                            "cost_accounting"
                        ),
                        "metrics": item.metrics_json,
                        "error_code": item.error_code,
                    } for item in attempts],
                    "artifacts": [{
                        "id": item.id,
                        "type": item.artifact_type,
                        "logical_name": item.logical_name,
                        "uri": item.uri,
                        "media_type": item.media_type,
                        "size_bytes": item.size_bytes,
                        "checksum_sha256": item.checksum_sha256,
                        "status": item.status,
                        "metadata": item.metadata_json,
                    } for item in artifacts],
                })
            return snapshot
