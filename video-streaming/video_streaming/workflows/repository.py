"""SQLAlchemy-backed source of truth for jobs, workflows and artifacts."""

import hashlib
import hmac
import json
import math
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    JSON, BigInteger, CheckConstraint, Column, DateTime, Float, ForeignKey,
    ForeignKeyConstraint, Index, Integer, Numeric, String, Text,
    UniqueConstraint, create_engine,
    select, func,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker

from .execution_plan import (
    execution_plan_sha256,
    validate_execution_plan,
)
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
            secret_envelopes=None):
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
            if context.purpose != "webhook.callback":
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
                    replay_fingerprint = (
                        execution_plan_sha256(dict(plan, job_id=existing.id))
                        if plan is not None else None
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
                for index, spec in enumerate(stage_specs):
                    session.add(StageRecord(
                        id=new_id(),
                        workflow_id=workflow_id,
                        stage_key=spec["stage_key"],
                        operation=spec["operation"],
                        ordinal=spec.get("ordinal", index),
                        input_contract_json=spec.get("input", {}),
                        output_contract_json=spec.get("output", {}),
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
                replay_fingerprint = (
                    execution_plan_sha256(dict(plan, job_id=existing.id))
                    if plan is not None else None
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
        """Authorize the Job owning a playback session before accepting events."""
        from video_streaming.security import (
            AuthorizationError,
            authorize_tenant_resource,
        )

        if principal is None:
            raise AuthorizationError("Resource not found or access denied")
        with self._session() as session:
            playback = session.execute(
                select(PlaybackSessionRecord).where(
                    PlaybackSessionRecord.id == session_id
                )
            ).scalar_one_or_none()
            if playback is None:
                raise AuthorizationError("Resource not found or access denied")
            job = session.execute(
                select(JobRecord).where(
                    JobRecord.id == playback.job_id
                )
            ).scalar_one_or_none()
            if job is None:
                raise AuthorizationError("Resource not found or access denied")
            authorize_tenant_resource(
                principal,
                job.owner_tenant_id,
                required_scope,
            )
            if (
                    job.owner_issuer != principal.issuer
                    or job.owner_subject != principal.subject):
                raise AuthorizationError(
                    "Resource not found or access denied"
                )
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
            query = query.with_for_update()
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
        if (
                not isinstance(lease_token, str)
                or not hmac.compare_digest(
                    attempt.lease_token_hash,
                    hashlib.sha256(lease_token.encode("utf-8")).hexdigest(),
                )
                or attempt.state != AttemptState.RUNNING.value
                or attempt.lease_expires_at is None
                or attempt.lease_expires_at <= now):
            raise AttemptLeaseLost("Attempt lease is no longer valid")

    def _recover_expired_attempt_in_session(
            self, session, stage, attempt, now, pricing=None):
        if (
                attempt.state != AttemptState.RUNNING.value
                or attempt.lease_token_hash is None
                or attempt.lease_expires_at is None
                or attempt.lease_expires_at > now
                or stage.status not in (
                    StageState.PROCESSING.value,
                    StageState.VALIDATED.value,
                    StageState.PUBLISHING.value)):
            return False

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
        attempt.error_code = "WORKER_LEASE_EXPIRED"
        attempt.error_message = "Recovered expired worker lease"
        workflow = session.get(WorkflowRecord, stage.workflow_id)
        _record_event(
            session,
            workflow.job_id,
            workflow.id,
            "ATTEMPT_LEASE_EXPIRED",
            from_state=previous_status,
            to_state=StageState.RETRY_WAIT.value,
            stage_id=stage.id,
            attempt_id=attempt.id,
            payload={"reason": "WORKER_LEASE_EXPIRED"},
        )
        return True

    def claim_attempt(
            self,
            job_id,
            stage_key,
            lease_owner,
            lease_ttl_seconds=60,
            execution_backend="celery",
            execution_ref=None,
            pricing=None):
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
        now = utcnow()
        lease_token = secrets.token_urlsafe(32)
        lease_expires_at = now + timedelta(seconds=ttl)
        lease_token_hash = hashlib.sha256(
            lease_token.encode("utf-8")
        ).hexdigest()

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
                    .with_for_update()
                ).scalar_one_or_none()
                if running is None:
                    raise StageAlreadyRunning(
                        "Stage {} has an active state without a claimable attempt"
                        .format(stage_key)
                    )
                if not self._recover_expired_attempt_in_session(
                        session, stage, running, now, pricing):
                    raise StageAlreadyRunning(
                        "Stage {} already has a live attempt lease".format(stage_key)
                    )

            current = StageState(stage.status)
            if StageState.PROCESSING not in ALLOWED_STAGE_TRANSITIONS[current]:
                raise InvalidStateTransition(
                    "Cannot claim stage {} from {}".format(
                        stage_key, current.value
                    )
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
            job = session.get(JobRecord, job_id)
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

    def renew_attempt_lease(
            self,
            job_id,
            stage_key,
            attempt_id,
            lease_token,
            lease_ttl_seconds=60):
        """Extend a live lease; an expired or stale worker cannot resurrect it."""
        ttl = self._validated_lease_ttl(lease_ttl_seconds)
        now = utcnow()
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
            self._assert_attempt_lease(attempt, lease_token, now=now)
            attempt.heartbeat_at = now
            attempt.lease_expires_at = now + timedelta(seconds=ttl)
            return attempt.lease_expires_at

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
            lease_token=None):
        """Persist the structured report as a first-class artifact."""
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
                    return False
            previous_status = stage.status
            self._transition(stage, StageState.VALIDATED)
            stage.output_contract_json = {
                "validation": validation_report,
            }
            session.add(ArtifactRecord(
                id=new_id(),
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
            return True

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
        metrics = dict(metrics or {})
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
            if stages and all(
                    item.status == StageState.FINISHED.value
                    for item in stages):
                workflow.state = WorkflowState.FINISHED.value
                workflow.updated_at = finished_at
                job = session.get(JobRecord, job_id)
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
            lease_token=None):
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
            attempt.error_code = error_code
            attempt.error_message = error_message
            workflow = session.get(WorkflowRecord, stage.workflow_id)
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
            pricing=None):
        """Persist cancellation of a running attempt and its workflow stage."""
        with self._session.begin() as session:
            stage = self._find_stage(session, job_id, stage_key, lock=True)
            attempt = session.get(AttemptRecord, attempt_id)
            if attempt is None or attempt.stage_id != stage.id:
                raise ValueError("Attempt does not belong to workflow stage")
            if attempt.state != AttemptState.RUNNING.value:
                return False
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
            attempt.error_code = "CANCELLED"
            attempt.error_message = reason
            workflow = session.get(WorkflowRecord, stage.workflow_id)
            workflow.state = WorkflowState.CANCELLED.value
            workflow.updated_at = now
            job = session.get(JobRecord, job_id)
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
