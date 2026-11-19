import mimetypes
import os
from functools import partial
from abc import ABC
from botocore import exceptions as botocore_exceptions
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.core.constants import CacheKeysTemplates
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from video_streaming.ffmpeg.utils import S3UploadCallback
from video_streaming.workflows.service import get_workflow_repository
from video_streaming.workflows.domain import AttemptLeaseLost
from video_streaming.workflows.lease import AttemptLeaseHeartbeat
from video_streaming.workflows.artifacts import attempt_scoped_object_key
from video_streaming.workflows.metrics import configured_pricing
from video_streaming.workflows.resource_meter import ProcessResourceMeter
from .base import BaseStreamingTask
from .mixins import UploadFileMixin


def _publish_leased_file(
        task,
        repository,
        file_path,
        file_size,
        s3_output_key,
        s3_output_bucket,
        request_id,
        output_id,
        processing_attempt_id,
        kwargs):
    """Publish one validated processing artifact under an immutable Attempt key."""
    publication_id, _, lease_token, _ = repository.claim_publication_attempt(
        job_id=request_id,
        stage_key=output_id,
        lease_ttl_seconds=settings.WORKFLOW_ATTEMPT_LEASE_TTL_SECONDS,
        lease_owner=("celery:{}".format(
            getattr(task.request, "hostname", None) or "worker"
        ))[:255],
        execution_backend="celery",
        execution_ref=str(task.request.id),
        processing_attempt_id=processing_attempt_id,
        pricing=configured_pricing(settings),
        package_output_key=s3_output_key,
        package_output_bucket=s3_output_bucket,
        package_output_kind="single_file",
    )
    heartbeat = AttemptLeaseHeartbeat(
        repository,
        request_id,
        output_id,
        publication_id,
        lease_token,
        lease_ttl_seconds=settings.WORKFLOW_ATTEMPT_LEASE_TTL_SECONDS,
    )
    object_key = attempt_scoped_object_key(s3_output_key, publication_id)
    metrics = {
        "input_bytes": None,
        "cpu_seconds": None,
        "peak_memory_bytes": None,
        "memory_duration_ms": None,
        "output_bytes": None,
        "artifact_count": 0,
    }

    heartbeat.assert_owned()
    if task.is_forced_to_stop(request_id) or task.is_output_forced_to_stop(
            request_id, output_id):
        repository.cancel_attempt(
            request_id,
            output_id,
            publication_id,
            reason="CANCELLED_BEFORE_FILE_PUBLICATION",
            metrics=metrics,
            pricing=configured_pricing(settings),
            lease_token=lease_token,
        )
        if task.is_forced_to_stop(request_id):
            raise task.raise_revoke(request_id)
        raise task.raise_revoke_output(request_id, output_id)

    repository.start_publishing(
        request_id, output_id, publication_id, lease_token=lease_token
    )
    cancelled = False
    resource_meter = ProcessResourceMeter().start()
    try:
        task.save_output_status(
            task.output_status.UPLOADING, output_id, request_id
        )
        progress = S3UploadCallback(
            task=task,
            task_id=str(task.request.id),
            output_id=output_id,
            request_id=request_id,
        ).file_progress

        def on_upload_progress(transferred):
            heartbeat.on_progress()
            progress(file_size, transferred)

        heartbeat.assert_owned()
        task.s3_service.upload_file_by_path(
            key=object_key,
            file_path=file_path,
            bucket_name=s3_output_bucket,
            callback=on_upload_progress,
        )
        heartbeat.assert_owned()

        details = task.s3_service.head(
            key=object_key, bucket_name=s3_output_bucket
        )
        heartbeat.assert_owned()
        if not details or int(details.get("ContentLength", -1)) != file_size:
            raise RuntimeError(
                "Uploaded file is not visible with the expected size"
            )

        first_byte = task.s3_service.read_range(
            key=object_key,
            bucket_name=s3_output_bucket,
            start=0,
            end=0,
        )
        heartbeat.assert_owned()
        if not first_byte:
            raise RuntimeError(
                "Uploaded file is not readable through storage"
            )

        metrics.update(resource_meter.stop())
        metrics["output_bytes"] = file_size
        metrics["artifact_count"] = 1
        if task.is_forced_to_stop(request_id) or task.is_output_forced_to_stop(
                request_id, output_id):
            repository.cancel_attempt(
                request_id,
                output_id,
                publication_id,
                reason="CANCELLED_BEFORE_FILE_ARTIFACT_COMMIT",
                metrics=metrics,
                pricing=configured_pricing(settings),
                lease_token=lease_token,
            )
            cancelled = True
        else:
            artifact_type = kwargs.get("artifact_type") or "media_artifact"
            media_type = kwargs.get("media_type") or (
                mimetypes.guess_type(file_path)[0]
                or "application/octet-stream"
            )
            heartbeat.assert_owned()
            repository.finish_stage(
                job_id=request_id,
                stage_key=output_id,
                attempt_id=publication_id,
                artifacts=[{
                    "artifact_type": artifact_type,
                    "logical_name": os.path.basename(s3_output_key),
                    "uri": "s3://{}/{}".format(s3_output_bucket, object_key),
                    "media_type": media_type,
                    "size_bytes": file_size,
                    "metadata": {
                        "readback_verified": True,
                        "attempt_scoped": True,
                        "processing_attempt_id": processing_attempt_id,
                    },
                }],
                metrics=metrics,
                pricing=configured_pricing(settings),
                lease_token=lease_token,
            )
    except AttemptLeaseLost:
        # A stale publisher cannot commit an Artifact pointer. Its object key
        # is unique to this Attempt and is therefore not authoritative.
        resource_meter.stop()
        raise
    except Exception as exc:
        metrics.update(resource_meter.stop())
        if task.is_forced_to_stop(request_id) or task.is_output_forced_to_stop(
                request_id, output_id):
            repository.cancel_attempt(
                request_id,
                output_id,
                publication_id,
                reason="CANCELLED_DURING_FILE_PUBLICATION",
                metrics=metrics,
                pricing=configured_pricing(settings),
                lease_token=lease_token,
            )
            cancelled = True
        else:
            retryable = (
                task.request.retries < settings.TASK_RETRY_BACKOFF_MAX
            )
            repository.fail_attempt(
                job_id=request_id,
                stage_key=output_id,
                attempt_id=publication_id,
                retryable=retryable,
                error_code=(
                    "FILE_PUBLICATION_RETRYABLE"
                    if retryable else "FILE_PUBLICATION_FAILED"
                ),
                error_message=str(exc),
                metrics=metrics,
                pricing=configured_pricing(settings),
                lease_token=lease_token,
            )
            if retryable:
                raise task.retry(
                    exc=exc,
                    max_retries=settings.TASK_RETRY_BACKOFF_MAX,
                )
            task.save_job_stop_reason(
                task.stop_reason.INTERNAL_ERROR, request_id
            )
            raise task.raise_ignore(
                message=task.error_messages.CAN_NOT_UPLOAD_FILE,
                request_kwargs=task.request.kwargs,
            )

    if cancelled:
        if task.is_forced_to_stop(request_id):
            raise task.raise_revoke(request_id)
        raise task.raise_revoke_output(request_id, output_id)

    try:
        os.remove(file_path)
    except OSError:
        # Publication is already durable; local scratch cleanup is best-effort.
        pass

    task.cache.set(
        CacheKeysTemplates.OUTPUT_SIZE.format(
            request_id=request_id, output_id=output_id
        ),
        file_size,
    )
    task.save_output_status(
        task.output_status.PROCESSING_FINISHED, output_id, request_id
    )
    task.save_output_status(
        task.output_status.UPLOADING_FINISHED, output_id, request_id
    )
    return {
        "file_size": file_size,
        "durable_stage_status": "FINISHED",
        "uri": "s3://{}/{}".format(s3_output_bucket, object_key),
        "publication_attempt_id": publication_id,
    }


class UploadFileTask(
        ChainCallbackMixin,
        UploadFileMixin,
        BaseStreamingTask,
        ABC
        ):

    # rewrite BaseOutputMixin.save_failed
    def save_failed(self, request_id, output_id):
        super().save_failed(request_id, output_id)
        # stop reason will only be set if there is no reason before.
        # set common reason for the task after many retries or etc.
        self.save_job_stop_reason(
            self.stop_reason.FAILED_UPLOAD_FILE,
            request_id
        )


@celery_app.task(name="upload_file",
                 base=UploadFileTask,
                 **TASK_DECORATOR_KWARGS)
def upload_file(self,
                *args,
                file_path: str = None,
                s3_output_key: str = None,
                s3_output_bucket: str = settings.S3_DEFAULT_OUTPUT_BUCKET_NAME,
                request_id: str = None,
                output_id: str = None,
                **kwargs):
    """Publish one file and finish its durable stage after storage read-back."""

    self.check_upload_file_requirements(
        request_id=request_id,
        output_id=output_id,
        file_path=file_path,
        s3_output_key=s3_output_key,
        s3_output_bucket=s3_output_bucket,
    )

    file_size = os.path.getsize(file_path)
    if file_size <= 0:
        raise self.raise_ignore(
            message=self.error_messages.CAN_NOT_UPLOAD_EMPTY_FILE,
            request_kwargs=self.request.kwargs,
        )

    repository = get_workflow_repository(required=False)
    durable_stage = (
        repository.get_stage_snapshot(request_id, output_id)
        if repository is not None else None
    )
    attempt_id = kwargs.get("attempt_id")

    if durable_stage is not None and durable_stage.get("status") in (
            "VALIDATED", "RETRY_WAIT", "PUBLISHING"):
        processing_attempt = next((
            item for item in durable_stage.get("attempts", [])
            if item.get("id") == attempt_id
        ), None)
        if (
                processing_attempt is not None
                and processing_attempt.get("status") == "SUCCEEDED"):
            return _publish_leased_file(
                self,
                repository,
                file_path,
                file_size,
                s3_output_key,
                s3_output_bucket,
                request_id,
                output_id,
                attempt_id,
                kwargs,
            )

    if self.is_forced_to_stop(request_id) or self.is_output_forced_to_stop(
            request_id, output_id):
        # This task does not own the processing Attempt's process-local token.
        # The processing worker observes the cancellation flag and closes its
        # own lease; the publisher must not attempt an unfenced cancellation.
        if self.is_forced_to_stop(request_id):
            raise self.raise_revoke(request_id)
        raise self.raise_revoke_output(request_id, output_id)

    if durable_stage is not None:
        # If recovery closed a lost publisher attempt, start a new durable
        # attempt and re-attach the already persisted validation report.
        if durable_stage["status"] == "FINISHED":
            previous_attempt = (
                durable_stage["attempts"][-1]
                if durable_stage.get("attempts") else {}
            )
            published_size = int(
                previous_attempt.get("output_bytes") or file_size
            )
            self.cache.set(
                CacheKeysTemplates.OUTPUT_SIZE.format(
                    request_id=request_id, output_id=output_id
                ),
                published_size,
            )
            self.save_output_status(
                self.output_status.PROCESSING_FINISHED, output_id, request_id
            )
            self.save_output_status(
                self.output_status.UPLOADING_FINISHED, output_id, request_id
            )
            return {
                "file_size": published_size,
                "durable_stage_status": "FINISHED",
                "replayed": True,
            }

        if durable_stage["status"] == "RETRY_WAIT":
            validation_report = kwargs.get("validation_report")
            if not validation_report:
                raise RuntimeError(
                    "A recovered file publication requires its validation report"
                )
            attempt_id, _ = repository.start_attempt(
                job_id=request_id,
                stage_key=output_id,
                execution_backend="celery",
                execution_ref=self.request.id.__str__(),
            )
            repository.mark_validated(
                request_id, output_id, attempt_id, validation_report
            )

        if not attempt_id:
            raise RuntimeError(
                "Durable file publication requires the attempt_id from its render stage"
            )
        repository.start_publishing(request_id, output_id, attempt_id)

    self.save_output_status(
        self.output_status.UPLOADING, output_id, request_id
    )

    file_progress_callback = S3UploadCallback(
        task=self,
        task_id=self.request.id.__str__(),
        output_id=output_id,
        request_id=request_id,
    ).file_progress

    try:
        self.s3_service.upload_file_by_path(
            key=s3_output_key,
            file_path=file_path,
            bucket_name=s3_output_bucket,
            callback=partial(file_progress_callback, file_size),
        )

        if durable_stage is not None:
            details = self.s3_service.head(
                key=s3_output_key, bucket_name=s3_output_bucket
            )
            if not details or int(details.get("ContentLength", -1)) != file_size:
                raise RuntimeError(
                    "Uploaded file is not visible with the expected size"
                )
            first_byte = self.s3_service.read_range(
                key=s3_output_key,
                bucket_name=s3_output_bucket,
                start=0,
                end=0,
            )
            if not first_byte:
                raise RuntimeError(
                    "Uploaded file is not readable through storage"
                )

            artifact_type = kwargs.get("artifact_type") or "media_artifact"
            media_type = kwargs.get("media_type") or (
                mimetypes.guess_type(file_path)[0]
                or "application/octet-stream"
            )
            repository.finish_stage(
                job_id=request_id,
                stage_key=output_id,
                attempt_id=attempt_id,
                artifacts=[{
                    "artifact_type": artifact_type,
                    "logical_name": os.path.basename(s3_output_key),
                    "uri": "s3://{}/{}".format(
                        s3_output_bucket, s3_output_key
                    ),
                    "media_type": media_type,
                    "size_bytes": file_size,
                    "metadata": {
                        "readback_verified": True,
                    },
                }],
                metrics={
                    "cpu_seconds": kwargs.get("cpu_seconds"),
                    "peak_memory_bytes": (
                        kwargs.get("peak_memory_bytes")
                        or self.cache.get(
                            CacheKeysTemplates.OUTPUT_END_MEMORY_RSS.format(
                                request_id=request_id,
                                output_id=output_id,
                            )
                        )
                    ),
                    "memory_duration_ms": kwargs.get("memory_duration_ms"),
                    "input_bytes": int(kwargs.get("input_bytes") or 0),
                    "output_bytes": file_size,
                    "artifact_count": 1,
                },
                pricing=configured_pricing(settings),
            )
    except Exception as exc:
        if durable_stage is not None:
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
                error_code="FILE_PUBLICATION_FAILED",
                error_message=str(exc),
                metrics={
                    "input_bytes": int(kwargs.get("input_bytes") or 0),
                    "output_bytes": file_size,
                    "cpu_seconds": kwargs.get("cpu_seconds"),
                    "peak_memory_bytes": kwargs.get("peak_memory_bytes"),
                    "memory_duration_ms": kwargs.get("memory_duration_ms"),
                },
                pricing=configured_pricing(settings),
            )
        self.save_job_stop_reason(
            self.stop_reason.INTERNAL_ERROR, request_id
        )
        raise self.raise_ignore(
            message=self.error_messages.CAN_NOT_UPLOAD_FILE,
            request_kwargs=self.request.kwargs,
        )

    self.cache.set(
        CacheKeysTemplates.OUTPUT_SIZE.format(
            request_id=request_id,
            output_id=output_id,
        ),
        file_size,
    )

    self.save_output_status(
        self.output_status.PROCESSING_FINISHED,
        output_id,
        request_id,
    )
    self.save_output_status(
        self.output_status.UPLOADING_FINISHED,
        output_id,
        request_id,
    )

    return {
        "file_size": file_size,
        "durable_stage_status": (
            "FINISHED" if durable_stage is not None else None
        ),
        "uri": "s3://{}/{}".format(s3_output_bucket, s3_output_key),
    }
