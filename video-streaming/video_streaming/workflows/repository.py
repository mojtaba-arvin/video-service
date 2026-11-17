"""SQLAlchemy-backed source of truth for jobs, workflows and artifacts."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, BigInteger, Column, DateTime, Float, ForeignKey, Index, Integer,
    Numeric, String, Text, UniqueConstraint, create_engine, select, func,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from .domain import (
    AttemptState,
    IdempotencyConflict,
    InvalidStateTransition,
    JobState,
    StageState,
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


class WorkflowRepository:
    """Persistent repository; Celery IDs are optional execution metadata only."""

    def __init__(self, database_url, create_schema=False, engine=None):
        if engine is None and not database_url:
            raise ValueError("WORKFLOW_DATABASE_URL is required")
        if engine is None:
            options = {"pool_pre_ping": True, "future": True}
            connect_args = {}
            if database_url.startswith("sqlite:"):
                connect_args["check_same_thread"] = False
                options["connect_args"] = connect_args
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
            if stage.status == StageState.PROCESSING.value:
                running = session.execute(
                    select(AttemptRecord).where(
                        AttemptRecord.stage_id == stage.id,
                        AttemptRecord.state == AttemptState.RUNNING.value,
                    ).order_by(AttemptRecord.attempt_number.desc())
                ).scalar_one_or_none()
                if running is not None:
                    if execution_ref and not running.execution_ref:
                        running.execution_ref = execution_ref
                    return running.id, running.attempt_number

            current = StageState(stage.status)
            if StageState.PROCESSING not in ALLOWED_STAGE_TRANSITIONS[current]:
                raise InvalidStateTransition(
                    "Cannot start stage {} from {}".format(stage_key, current.value)
                )

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

    def start_publishing(self, job_id, stage_key, attempt_id):
        with self._session.begin() as session:
            stage = self._find_stage(session, job_id, stage_key, lock=True)
            attempt = session.get(AttemptRecord, attempt_id)
            if attempt is None or attempt.stage_id != stage.id:
                raise ValueError("Attempt does not belong to workflow stage")
            if attempt.state != AttemptState.RUNNING.value:
                raise InvalidStateTransition("Attempt is not running")
            self._transition(stage, StageState.PUBLISHING)

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
            self._transition(stage, StageState.FINISHED)
            finished_at = utcnow()
            attempt.state = AttemptState.SUCCEEDED.value
            attempt.finished_at = finished_at
            started = attempt.started_at
            attempt.duration_ms = max(
                0, int((finished_at - started).total_seconds() * 1000)
            )
            attempt.cpu_seconds = metrics.get("cpu_seconds")
            attempt.peak_memory_bytes = metrics.get("peak_memory_bytes")
            attempt.input_bytes = metrics.get("input_bytes")
            attempt.output_bytes = metrics.get("output_bytes")
            attempt.metrics_json = metrics
            attempt.artifact_count = len(artifacts)
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
            if stages and all(
                    item.status == StageState.FINISHED.value
                    for item in stages):
                workflow.state = WorkflowState.FINISHED.value
                workflow.updated_at = finished_at
                job = session.get(JobRecord, job_id)
                job.state = JobState.FINISHED.value
                job.updated_at = finished_at
                job.version += 1
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
            if stage.status in (
                    StageState.FINISHED.value,
                    StageState.FAILED.value,
                    StageState.CANCELLED.value):
                return False
            self._transition(stage, target)
            attempt.state = attempt_state.value
            attempt.finished_at = utcnow()
            attempt.duration_ms = max(
                0, int((attempt.finished_at - attempt.started_at).total_seconds() * 1000)
            )
            attempt.cpu_seconds = metrics.get("cpu_seconds")
            attempt.peak_memory_bytes = metrics.get("peak_memory_bytes")
            attempt.input_bytes = metrics.get("input_bytes")
            attempt.output_bytes = metrics.get("output_bytes")
            attempt.metrics_json = metrics
            attempt.error_code = error_code
            attempt.error_message = error_message
            if not retryable:
                workflow = session.get(WorkflowRecord, stage.workflow_id)
                workflow.state = WorkflowState.FAILED.value
                workflow.updated_at = utcnow()
                job = session.get(JobRecord, job_id)
                job.state = JobState.FAILED.value
                job.updated_at = utcnow()
                job.version += 1
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
                if stage.status != StageState.PROCESSING.value:
                    continue
                self._transition(stage, StageState.RETRY_WAIT)
                attempt.state = AttemptState.RETRYABLE.value
                attempt.finished_at = now
                attempt.error_code = "WORKER_LOST"
                attempt.error_message = "Recovered stale running attempt"
                recovered += 1
        return recovered

    def is_stage_finished(self, job_id, stage_key):
        with self._session() as session:
            stage = self._find_stage(session, job_id, stage_key)
            return stage.status == StageState.FINISHED.value

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
            snapshot = {
                "id": job.id,
                "idempotency_key": job.idempotency_key,
                "request_hash": job.request_hash,
                "reference_id": job.reference_id,
                "status": job.state,
                "workflow": {
                    "id": workflow.id,
                    "name": workflow.name,
                    "status": workflow.state,
                },
                "stages": [],
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
