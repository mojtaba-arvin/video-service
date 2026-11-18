import json
from abc import ABC
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.core.constants import CacheKeysTemplates
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from video_streaming.workflows.service import get_workflow_repository
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
    attempt_id = kwargs.get("attempt_id")
    if not attempt_id:
        raise RuntimeError(
            "Package publication requires the durable attempt_id from create_playlist"
        )

    if self.is_forced_to_stop(request_id) or self.is_output_forced_to_stop(
            request_id, output_id):
        repository.cancel_attempt(
            request_id, output_id, attempt_id,
            reason="CANCELLED_BEFORE_PACKAGE_PUBLICATION",
        )
        if self.is_forced_to_stop(request_id):
            raise self.raise_revoke(request_id)
        raise self.raise_revoke_output(request_id, output_id)

    stage = repository.get_stage_snapshot(request_id, output_id)
    if stage is None:
        raise RuntimeError(
            "Durable workflow stage is missing for playlist output {}".format(
                output_id
            )
        )

    # A redelivered task after the DB commit must only repair the Redis
    # projection. Do not upload the same completed stage again.
    if stage["status"] == "FINISHED":
        attempt = stage["attempts"][-1] if stage["attempts"] else {}
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

    if self.is_forced_to_stop(request_id) or self.is_output_forced_to_stop(
            request_id, output_id):
        repository.cancel_attempt(
            request_id, output_id, attempt_id,
            reason="CANCELLED_BEFORE_PACKAGE_PUBLICATION",
        )
        if self.is_forced_to_stop(request_id):
            raise self.raise_revoke(request_id)
        raise self.raise_revoke_output(request_id, output_id)

    repository.start_publishing(request_id, output_id, attempt_id)
    self.save_output_status(
        self.output_status.UPLOADING, output_id, request_id
    )

    try:
        publication = self.upload_directory(
            directory=directory,
            s3_output_key=s3_output_key,
            s3_output_bucket=s3_output_bucket,
            output_id=output_id,
            request_id=request_id,
            protocol=protocol,
            expected_renditions=kwargs.get("expected_renditions"),
        )

        start_cpu = self.cache.get(
            CacheKeysTemplates.OUTPUT_START_CPU_TIMES.format(
                request_id=request_id, output_id=output_id
            )
        ) or []
        end_cpu = self.cache.get(
            CacheKeysTemplates.OUTPUT_END_CPU_TIMES.format(
                request_id=request_id, output_id=output_id
            )
        ) or []
        cpu_seconds = 0.0
        if len(start_cpu) >= 2 and len(end_cpu) >= 2:
            cpu_seconds = max(
                0.0,
                sum(float(value) for value in end_cpu[:2])
                - sum(float(value) for value in start_cpu[:2]),
            )
        peak_memory_bytes = self.cache.get(
            CacheKeysTemplates.OUTPUT_END_MEMORY_RSS.format(
                request_id=request_id, output_id=output_id
            )
        )

        repository.finish_stage(
            job_id=request_id,
            stage_key=output_id,
            attempt_id=attempt_id,
            artifacts=publication["artifacts"],
            metrics={
                "cpu_seconds": cpu_seconds,
                "peak_memory_bytes": (
                    int(peak_memory_bytes) if peak_memory_bytes else None
                ),
                "input_bytes": int(kwargs.get("input_bytes") or 0),
                "output_bytes": int(publication["directory_size"]),
                "artifact_count": int(publication["files"]),
                "validation_errors": len(
                    publication["validation_report"].get("errors", [])
                ),
            },
        )
    except Exception as exc:
        if self.is_forced_to_stop(request_id) or self.is_output_forced_to_stop(
                request_id, output_id):
            repository.cancel_attempt(
                request_id, output_id, attempt_id,
                reason="CANCELLED_DURING_PACKAGE_PUBLICATION",
            )
            if self.is_forced_to_stop(request_id):
                raise self.raise_revoke(request_id)
            raise self.raise_revoke_output(request_id, output_id)

        # Keep the same durable attempt in PUBLISHING while Celery retries the
        # idempotent publisher. A worker-loss recovery can independently move
        # the attempt to RETRY_WAIT using database timestamps.
        retries_available = (
            self.request.retries < settings.TASK_RETRY_BACKOFF_MAX
        )
        if retries_available:
            raise self.retry(
                exc=exc,
                max_retries=settings.TASK_RETRY_BACKOFF_MAX,
            )

        repository.fail_attempt(
            job_id=request_id,
            stage_key=output_id,
            attempt_id=attempt_id,
            retryable=False,
            error_code="PACKAGE_PUBLICATION_FAILED",
            error_message=str(exc),
            metrics={
                "input_bytes": int(kwargs.get("input_bytes") or 0),
            },
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
    # BaseOutputMixin verifies the durable Stage is FINISHED before allowing
    # either legacy status projection to advance.
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
    }
