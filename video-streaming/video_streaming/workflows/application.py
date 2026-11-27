"""Application-level Job submission use case.

This module deliberately knows nothing about gRPC, protobuf, Redis or Celery.
Transport adapters translate requests into this command and retain ownership
of transport-specific response formatting and legacy dispatch.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from video_streaming.security import require_scope


@dataclass(frozen=True)
class SubmitJobCommand:
    job_id: str
    idempotency_key: str
    request_hash: str
    reference_id: str
    execution_plan: Dict[str, Any]
    execution_backend: str
    enqueue_dispatch: bool
    outbox_dedupe_key: Optional[str]
    principal: Any
    project_id: Optional[str]
    project_revision_id: Optional[str]
    secret_envelopes: Sequence[Any]
    quota_enforcement_enabled: bool
    metadata: Dict[str, Any]


@dataclass(frozen=True)
class SubmitJobResult:
    job_id: str
    created: bool


class SubmitJobService:
    """Authorize and durably register a Job through the workflow repository."""

    def __init__(self, repository):
        if repository is None:
            raise ValueError("workflow repository is required")
        self._repository = repository

    @staticmethod
    def authorize(principal, project_id=None, project_revision_id=None) -> None:
        """Fail before request expansion, encryption or other expensive work."""
        require_scope(principal, "video.jobs:create")
        if bool(project_id) != bool(project_revision_id):
            raise ValueError(
                "project_id and project_revision_id must be supplied together"
            )
        if project_revision_id:
            require_scope(principal, "video.projects:read")

    def execute(self, command: SubmitJobCommand) -> SubmitJobResult:
        if not command.job_id:
            raise ValueError("job_id is required")
        if not command.idempotency_key:
            raise ValueError("idempotency_key is required")
        if not command.request_hash:
            raise ValueError("request_hash is required")
        if not command.execution_plan:
            raise ValueError("execution_plan is required")

        self.authorize(
            command.principal,
            command.project_id,
            command.project_revision_id,
        )

        existing_job_id = self._repository.find_idempotent_job(
            command.idempotency_key,
            command.request_hash,
            owner_principal=command.principal,
        )
        if existing_job_id is not None:
            return SubmitJobResult(job_id=existing_job_id, created=False)

        job_id, created = self._repository.create_job(
            job_id=command.job_id,
            idempotency_key=command.idempotency_key,
            request_hash=command.request_hash,
            reference_id=command.reference_id,
            workflow_name="video.vod",
            execution_plan=command.execution_plan,
            execution_backend=command.execution_backend,
            enqueue_dispatch=command.enqueue_dispatch,
            outbox_dedupe_key=command.outbox_dedupe_key,
            owner_principal=command.principal,
            project_id=command.project_id,
            project_revision_id=command.project_revision_id,
            secret_envelopes=command.secret_envelopes,
            quota_enforcement_enabled=command.quota_enforcement_enabled,
            metadata=command.metadata,
        )
        return SubmitJobResult(job_id=job_id, created=created)


class GetJobStatusService:
    """Authorize a batch of Job reads before returning durable snapshots."""

    def __init__(self, repository):
        if repository is None:
            raise ValueError("workflow repository is required")
        self._repository = repository

    def execute(self, job_ids, principal):
        job_ids = tuple(job_ids)
        # Preserve the adapter's all-or-nothing authorization boundary: no
        # snapshot (or Redis projection) is read until every Job is authorized.
        for job_id in job_ids:
            self._repository.authorize_job_access(
                job_id, principal, "video.jobs:read"
            )
        return {
            job_id: self._repository.get_job_snapshot(job_id)
            for job_id in job_ids
        }


@dataclass(frozen=True)
class CancelJobResult:
    job_id: str
    status: str
    requested: bool


class CancelJobService:
    """Request durable Job cancellation through the shared workflow port."""

    def __init__(self, repository):
        if repository is None:
            raise ValueError("workflow repository is required")
        self._repository = repository

    def execute(self, job_id, principal, reason="CANCELLED"):
        if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 36:
            raise ValueError("job_id must contain 1 to 36 characters")
        if (
                not isinstance(reason, str)
                or not reason.strip()
                or len(reason) > 500):
            raise ValueError("reason must contain 1 to 500 characters")
        require_scope(principal, "video.jobs:cancel")
        result = self._repository.request_job_cancellation(
            job_id, principal, reason=reason.strip()
        )
        return CancelJobResult(
            job_id=result["job_id"],
            status=result["status"],
            requested=result["requested"],
        )
