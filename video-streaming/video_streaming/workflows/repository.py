"""SQLAlchemy-backed source of truth for jobs, workflows and artifacts."""

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    JSON, BigInteger, Column, DateTime, Float, ForeignKey, Index, Integer,
    Numeric, String, Text, UniqueConstraint, create_engine, select, func,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker

from .domain import (
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

    id = Column(String(36), primary_key=True)
    idempotency_key = Column(String(255), nullable=False, unique=True)
    request_hash = Column(String(64), nullable=False)
    reference_id = Column(String(255), nullable=False, default="")
    state = Column(String(24), nullable=False, default=JobState.QUEUED.value)
    metadata_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    version = Column(Integer, nullable=False, default=1)


class WorkflowRecord(Base):
    __tablename__ = "workflow_workflows"

    id = Column(String(36), primary_key=True)
    job_id = Column(
        String(36), ForeignKey("workflow_jobs.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    name = Column(String(128), nullable=False)
    state = Column(String(24), nullable=False, default=WorkflowState.QUEUED.value)
    execution_backend = Column(String(64), nullable=True)
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
        Index("ix_workflow_attempts_state_started", "state", "started_at"),
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
            metadata=None):
        """Create the durable workflow once, or return the prior job on replay."""
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
                    return existing.id, False

                job = JobRecord(
                    id=job_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    reference_id=reference_id or "",
                    state=JobState.QUEUED.value,
                    metadata_json=metadata or {},
                )
                workflow_id = new_id()
                workflow = WorkflowRecord(
                    id=workflow_id,
                    job_id=job_id,
                    name=workflow_name,
                    state=WorkflowState.QUEUED.value,
                )
                session.add_all([job, workflow])
                # Insert parent rows before dependent events and stages. This
                # explicit flush avoids relying on unit-of-work table ordering
                # for unrelated ORM objects submitted in one batch.
                session.flush()
                _record_event(
                    session,
                    job_id,
                    workflow_id,
                    "JOB_CREATED",
                    from_state=None,
                    to_state=JobState.QUEUED.value,
                    payload={"workflow_name": workflow_name},
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
                return existing.id, False

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
            validation_report):
        """Persist the structured report as a first-class artifact."""
        with self._session.begin() as session:
            stage = self._find_stage(session, job_id, stage_key, lock=True)
            attempt = session.get(AttemptRecord, attempt_id)
            if attempt is None or attempt.stage_id != stage.id:
                raise ValueError("Attempt does not belong to workflow stage")
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

    def start_publishing(self, job_id, stage_key, attempt_id):
        with self._session.begin() as session:
            stage = self._find_stage(session, job_id, stage_key, lock=True)
            attempt = session.get(AttemptRecord, attempt_id)
            if attempt is None or attempt.stage_id != stage.id:
                raise ValueError("Attempt does not belong to workflow stage")
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
    def _estimate_cost(metrics, pricing):
        if not pricing:
            return None
        cpu_rate = float(pricing.get("cpu_per_second", 0))
        memory_rate = float(pricing.get("memory_gb_second", 0))
        storage_rate = float(pricing.get("storage_gb", 0))
        cpu_seconds = float(metrics.get("cpu_seconds") or 0)
        duration_seconds = float(metrics.get("duration_ms") or 0) / 1000.0
        memory_gb_seconds = (
            float(metrics.get("peak_memory_bytes") or 0)
            / (1024.0 ** 3) * duration_seconds
        )
        output_gb = float(metrics.get("output_bytes") or 0) / (1024.0 ** 3)
        return (
            cpu_seconds * cpu_rate
            + memory_gb_seconds * memory_rate
            + output_gb * storage_rate
        )

    def finish_stage(
            self,
            job_id,
            stage_key,
            attempt_id,
            artifacts=None,
            metrics=None,
            pricing=None):
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
                raise InvalidStateTransition("Attempt is not running")
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
            attempt.cpu_seconds = metrics.get("cpu_seconds")
            attempt.peak_memory_bytes = metrics.get("peak_memory_bytes")
            attempt.input_bytes = metrics.get("input_bytes")
            attempt.output_bytes = metrics.get("output_bytes")
            attempt.metrics_json = metrics
            prior_artifact_count = session.execute(
                select(func.count()).select_from(ArtifactRecord).where(
                    ArtifactRecord.stage_id == stage.id,
                    ArtifactRecord.attempt_id == attempt_id,
                )
            ).scalar_one()
            attempt.artifact_count = prior_artifact_count + len(artifacts)
            estimate = self._estimate_cost(metrics, pricing)
            if estimate is not None:
                attempt.estimated_cost = estimate
                attempt.cost_currency = pricing.get("currency", "USD")
                attempt.pricing_model_id = pricing.get("model_id")

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
            metrics=None):
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
            attempt.cpu_seconds = metrics.get("cpu_seconds")
            attempt.peak_memory_bytes = metrics.get("peak_memory_bytes")
            attempt.input_bytes = metrics.get("input_bytes")
            attempt.output_bytes = metrics.get("output_bytes")
            attempt.metrics_json = metrics
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
            reason="CANCELLED"):
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


    def recover_stale_attempts(self, older_than):
        """Move abandoned running attempts to RETRY_WAIT without Redis TTL."""
        recovered = 0
        now = utcnow()
        with self._session.begin() as session:
            attempts = session.execute(
                select(AttemptRecord).where(
                    AttemptRecord.state == AttemptState.RUNNING.value,
                    AttemptRecord.started_at < older_than,
                ).with_for_update()
            ).scalars().all()
            for attempt in attempts:
                stage = session.get(StageRecord, attempt.stage_id)
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
            "manifest_uri": manifest.uri,
        }

    @staticmethod
    def _playback_session_summary(record):
        return {
            "session_id": record.id,
            "status": record.status,
            "startup_ms": record.startup_ms,
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
                metadata = {}
                if decoder:
                    metadata["decoder"] = str(decoder)[:128]
                if client_version:
                    metadata["client_version"] = str(client_version)[:64]
                    if not playback.client_version:
                        playback.client_version = str(client_version)[:64]

                if event_type == "PLAYBACK_STARTED":
                    if playback.startup_ms is not None:
                        raise ValueError("PLAYBACK_STARTED was already recorded")
                    if startup_ms is None:
                        raise ValueError("PLAYBACK_STARTED requires startup_ms")
                    playback.startup_ms = int(startup_ms)

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
            rebuffer_count = sum(item.rebuffer_count for item in sessions)
            buffering_ms = sum(
                item.total_buffering_ms for item in sessions
            )
            decoder_errors = sum(
                item.decoder_error_count for item in sessions
            )
            wall_time_ms = 0
            for item in sessions:
                end_time = item.ended_at or now
                wall_time_ms += max(
                    0, int((end_time - item.started_at).total_seconds() * 1000)
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
                    sum(float(item.estimated_cost) for item in priced_attempts)
                    if priced_attempts and len(cost_currencies) == 1
                    and len(pricing_models) == 1 else None
                ),
                "cost_currency": (
                    next(iter(cost_currencies))
                    if len(cost_currencies) == 1 else None
                ),
                "pricing_model_id": (
                    next(iter(pricing_models))
                    if len(pricing_models) == 1 else None
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
