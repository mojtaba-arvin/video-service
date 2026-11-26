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
