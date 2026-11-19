from abc import ABC
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.core.constants import CacheKeysTemplates
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from video_streaming.workflows.service import get_workflow_repository
from video_streaming.workflows.domain import AttemptLeaseLost
from video_streaming.workflows.lease import AttemptLeaseHeartbeat
from video_streaming.workflows.artifacts import attempt_scoped_object_key
from video_streaming.workflows.metrics import configured_pricing
from .base import BaseStreamingTask
from .mixins import UploadDirectoryMixin


class UploadDirectoryTask(
        ChainCallbackMixin,
        UploadDirectoryMixin,
        BaseStreamingTask,
        ABC
        ):

    # rewrite BaseOutputMixin.save_failed
    def save_failed(self, request_id, output_id):
        super().save_failed(request_id, output_id)
        # stop reason will only be set if there is no reason before.
        # set common reason for the task after many retries or etc.
        self.save_job_stop_reason(
            self.stop_reason.FAILED_UPLOAD_DIRECTORY,
            request_id
        )


@celery_app.task(name="upload_directory",
                 base=UploadDirectoryTask,
                 **TASK_DECORATOR_KWARGS)
def upload_directory(self,
                     *args,
                     directory: str = None,
                     s3_output_key: str = None,
                     s3_output_bucket: str = settings.S3_DEFAULT_OUTPUT_BUCKET_NAME,
                     request_id: str = None,
                     output_id: str = None,
                     protocol: str = None,
                     **kwargs):
    """Publish a previously validated package and finish its durable stage."""

    self.check_upload_directory_requirements(
        request_id=request_id,
        output_id=output_id,
        directory=directory,
        s3_output_key=s3_output_key,
        s3_output_bucket=s3_output_bucket,
    )

    repository = get_workflow_repository()
    processing_attempt_id = kwargs.get("attempt_id")
    if not processing_attempt_id:
        raise RuntimeError("Package publication requires its processing attempt id")

    stage = repository.get_stage_snapshot(request_id, output_id)
    if stage is None:
        raise RuntimeError(
            "Durable workflow stage is missing for playlist output {}".format(
                output_id
            )
        )
    if stage["status"] == "FINISHED":
        attempt = stage["attempts"][-1] if stage.get("attempts") else {}
        directory_size = int(attempt.get("output_bytes") or 0)
        self.cache.set(
            CacheKeysTemplates.OUTPUT_SIZE.format(
                request_id=request_id, output_id=output_id
            ),
            directory_size,
        )
        self.save_output_status(
            self.output_status.PROCESSING_FINISHED, output_id, request_id
        )
        self.save_output_status(
            self.output_status.UPLOADING_FINISHED, output_id, request_id
        )
        return {
            "directory_size": directory_size,
            "durable_stage_status": "FINISHED",
            "replayed": True,
        }

    publication_id, _, lease_token, _ = repository.claim_publication_attempt(
        job_id=request_id,
        stage_key=output_id,
        lease_ttl_seconds=settings.WORKFLOW_ATTEMPT_LEASE_TTL_SECONDS,
        lease_owner=("celery:{}".format(
            getattr(self.request, "hostname", None) or "worker"
        ))[:255],
        execution_backend="celery",
        execution_ref=str(self.request.id),
        processing_attempt_id=processing_attempt_id,
        pricing=configured_pricing(settings),
    )
    heartbeat = AttemptLeaseHeartbeat(
        repository, request_id, output_id, publication_id, lease_token,
        lease_ttl_seconds=settings.WORKFLOW_ATTEMPT_LEASE_TTL_SECONDS,
    )
    attempt_output_key = attempt_scoped_object_key(
        s3_output_key, publication_id
    )

    if self.is_forced_to_stop(request_id) or self.is_output_forced_to_stop(
            request_id, output_id):
        repository.cancel_attempt(
            request_id, output_id, publication_id,
            reason="CANCELLED_BEFORE_PACKAGE_PUBLICATION",
            metrics={
                "input_bytes": None,
                "cpu_seconds": kwargs.get("cpu_seconds"),
                "peak_memory_bytes": kwargs.get("peak_memory_bytes"),
                "memory_duration_ms": kwargs.get("memory_duration_ms"),
            },
            pricing=configured_pricing(settings),
            lease_token=lease_token,
        )
        if self.is_forced_to_stop(request_id):
            raise self.raise_revoke(request_id)
        raise self.raise_revoke_output(request_id, output_id)

    repository.start_publishing(
        request_id, output_id, publication_id, lease_token=lease_token
    )
    # Processing usage belongs to the processing Attempt. Do not copy it into
    # publication metrics or aggregate the same CPU/memory spend twice.
    cpu_seconds = None
    peak_memory_bytes = None
    memory_duration_ms = None
    self.save_output_status(
        self.output_status.UPLOADING, output_id, request_id
    )

    try:
        heartbeat.assert_owned()
        publication = self.upload_directory(
            directory=directory,
            s3_output_key=attempt_output_key,
            s3_output_bucket=s3_output_bucket,
            output_id=output_id,
            request_id=request_id,
            protocol=protocol,
            expected_renditions=kwargs.get("expected_renditions"),
            on_progress=heartbeat.on_progress,
        )
        heartbeat.assert_owned()

        repository.finish_stage(
            job_id=request_id,
            stage_key=output_id,
            attempt_id=publication_id,
            artifacts=publication["artifacts"],
            metrics={
                "cpu_seconds": cpu_seconds,
                "peak_memory_bytes": (
                    int(peak_memory_bytes) if peak_memory_bytes else None
                ),
                "memory_duration_ms": memory_duration_ms,
                "input_bytes": int(kwargs.get("input_bytes") or 0),
                "output_bytes": int(publication["directory_size"]),
                "artifact_count": int(publication["files"]),
                "validation_errors": len(
                    publication["validation_report"].get("errors", [])
                ),
            },
            pricing=configured_pricing(settings),
            lease_token=lease_token,
        )
    except AttemptLeaseLost:
        # Attempt-scoped package keys isolate stale publishers. Only the
        # current lease holder may publish the Artifact pointer.
        raise
    except Exception as exc:
        if self.is_forced_to_stop(request_id) or self.is_output_forced_to_stop(
                request_id, output_id):
            repository.cancel_attempt(
                request_id, output_id, publication_id,
                reason="CANCELLED_DURING_PACKAGE_PUBLICATION",
                metrics={
                    "input_bytes": int(kwargs.get("input_bytes") or 0),
                    "cpu_seconds": cpu_seconds,
                    "peak_memory_bytes": (
                        int(peak_memory_bytes) if peak_memory_bytes else None
                    ),
                    "memory_duration_ms": memory_duration_ms,
                },
                pricing=configured_pricing(settings),
                lease_token=lease_token,
            )
            if self.is_forced_to_stop(request_id):
                raise self.raise_revoke(request_id)
            raise self.raise_revoke_output(request_id, output_id)

        retries_available = (
            self.request.retries < settings.TASK_RETRY_BACKOFF_MAX
        )
        repository.fail_attempt(
            job_id=request_id,
            stage_key=output_id,
            attempt_id=publication_id,
            retryable=retries_available,
            error_code=(
                "PACKAGE_PUBLICATION_RETRYABLE"
                if retries_available else "PACKAGE_PUBLICATION_FAILED"
            ),
            error_message=str(exc),
            metrics={
                "input_bytes": int(kwargs.get("input_bytes") or 0),
                "cpu_seconds": cpu_seconds,
                "peak_memory_bytes": (
                    int(peak_memory_bytes) if peak_memory_bytes else None
                ),
                "memory_duration_ms": memory_duration_ms,
            },
            pricing=configured_pricing(settings),
            lease_token=lease_token,
        )
        if retries_available:
            raise self.retry(
                exc=exc,
                max_retries=settings.TASK_RETRY_BACKOFF_MAX,
            )
        self.save_job_stop_reason(
            self.stop_reason.INTERNAL_ERROR, request_id
        )
        raise self.raise_ignore(
            message=self.error_messages.CAN_NOT_UPLOAD_FILE,
            request_kwargs=self.request.kwargs,
        )

    directory_size = int(publication["directory_size"])
    self.cache.set(
        CacheKeysTemplates.OUTPUT_SIZE.format(
            request_id=request_id, output_id=output_id
        ),
        directory_size,
    )
    self.save_output_status(
        self.output_status.PROCESSING_FINISHED, output_id, request_id
    )
    self.save_output_status(
        self.output_status.UPLOADING_FINISHED, output_id, request_id
    )

    return {
        "directory_size": directory_size,
        "durable_stage_status": "FINISHED",
        "manifest_key": publication["manifest_key"],
        "artifacts": publication["artifacts"],
        "publication_attempt_id": publication_id,
    }
