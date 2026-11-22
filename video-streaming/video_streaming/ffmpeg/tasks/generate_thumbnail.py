import ffmpeg
import os
from abc import ABC
from pathlib import Path
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from video_streaming.ffmpeg.thumbnail import (
    ThumbnailValidationError,
    render_thumbnail,
    validate_thumbnail_output,
)
from video_streaming.workflows.service import get_workflow_repository
from video_streaming.workflows.domain import AttemptLeaseLost
from video_streaming.workflows.lease import AttemptLeaseHeartbeat
from video_streaming.workflows.metrics import (
    capture_child_cpu_seconds,
    child_cpu_seconds_since,
    configured_pricing,
)
from .base import BaseStreamingTask
from .mixins import GenerateThumbnailMixin


class GenerateThumbnailTask(
        ChainCallbackMixin,
        GenerateThumbnailMixin,
        BaseStreamingTask,
        ABC
        ):

    # rewrite BaseOutputMixin.save_failed
    def save_failed(self, request_id, output_id):
        super().save_failed(request_id, output_id)
        # stop reason will only be set if there is no reason before.
        # set common reason for the task after many retries or etc.
        self.save_job_stop_reason(
            self.stop_reason.FAILED_GENERATE_THUMBNAIL,
            request_id
        )


@celery_app.task(name="generate_thumbnail",
                 base=GenerateThumbnailTask,
                 **TASK_DECORATOR_KWARGS)
def generate_thumbnail(
        self,
        *args,
        video_path: str = None,
        output_path: str = None,
        s3_output_key: str = None,
        request_id: str = None,
        output_id: str = None,
        thumbnail_time: str = "0",
        scale_width: int = -1,
        scale_height: int = -1,
        **kwargs,
        ) -> dict:
    """generate a thumbnails from the input video
    
    Args:
        self:
        *args:
        video_path:
        output_path:
        s3_output_key:
        request_id:
        output_id:
        thumbnail_time:
            Specific time of video as string.
            Defaults to is '0'.
            Some examples: '00:00:14.435','55','0.2','200ms','200000us','12:03:45' or '23.189'
            See http://ffmpeg.org/ffmpeg-utils.html#toc-Time-duration
        scale_width:
            Defaults to -1 ,The -1 will tell ffmpeg to
            automatically choose the correct height in relation to
            the provided width to preserve the aspect ratio
        scale_height:
            Defaults to -1
        **kwargs:
            some unused parameters from previous tasks that set by __call__
    Returns:

    """

    self.check_generate_thumbnail_requirements(
        request_id=request_id,
        output_id=output_id,
        video_path=video_path,
        output_path=output_path,
        s3_output_key=s3_output_key)

    if self.is_forced_to_stop(request_id):
        raise self.raise_revoke(request_id)
    if self.is_output_forced_to_stop(request_id, output_id):
        raise self.raise_revoke_output(request_id, output_id)

    input_bytes = os.path.getsize(video_path)
    cpu_started = capture_child_cpu_seconds()
    repository = get_workflow_repository()
    attempt_id, attempt_number, lease_token, _ = repository.claim_attempt(
        job_id=request_id,
        stage_key=output_id,
        lease_ttl_seconds=settings.WORKFLOW_ATTEMPT_LEASE_TTL_SECONDS,
        lease_owner=("celery:{}".format(
            getattr(self.request, "hostname", None) or "worker"
        ))[:255],
        execution_backend="celery",
        execution_ref=str(self.request.id),
    )
    lease_heartbeat = AttemptLeaseHeartbeat(
        repository,
        request_id,
        output_id,
        attempt_id,
        lease_token,
        lease_ttl_seconds=settings.WORKFLOW_ATTEMPT_LEASE_TTL_SECONDS,
    )
    # Keep the caller's explicit location for resolution. The actual file
    # written by this Attempt is tracked separately below so cleanup never
    # removes a caller-owned path from an earlier or concurrent execution.
    requested_output_path = output_path
    output_path = None
    usage_metrics = {}

    def capture_usage(metrics):
        process_id = str(metrics.get("process_id") or "legacy")
        previous = usage_metrics.get(process_id, {})
        current = dict(previous)
        current.update({
            key: value for key, value in metrics.items()
            if value is not None
        })
        # Samples are cumulative for one subprocess. Preserve the greatest
        # valid observation so a later partial/None sample cannot erase it.
        for key in ("cpu_seconds", "peak_memory_bytes", "memory_duration_ms"):
            value = metrics.get(key)
            if value is not None:
                prior = previous.get(key)
                current[key] = max(value, prior) if prior is not None else value
        usage_metrics[process_id] = current
        lease_heartbeat.on_usage(metrics)

    def aggregate_process_usage():
        samples = list(usage_metrics.values())
        return {
            "cpu_seconds": sum(
                float(item["cpu_seconds"])
                for item in samples if item.get("cpu_seconds") is not None
            ),
            "peak_memory_bytes": max(
                (int(item["peak_memory_bytes"]) for item in samples
                 if item.get("peak_memory_bytes") is not None),
                default=0,
            ) or None,
            "memory_duration_ms": sum(
                int(item["memory_duration_ms"])
                for item in samples
                if item.get("memory_duration_ms") is not None
            ) or None,
        }

    try:
        # Redis status is a projection; the PostgreSQL lease remains authoritative.
        lease_heartbeat.assert_owned()
        self.save_primary_status(
            self.primary_status.OUTPUTS_PROGRESSING,
            request_id)
        self.save_output_status(
            self.output_status.PREPARATION_PROCESSING,
            output_id,
            request_id)

        output_path, directory = self.ensure_set_output_location(
            request_id,
            output_id,
            output_path=requested_output_path,
            s3_output_key=s3_output_key,
        )
        directory = os.path.join(directory, ".attempts", attempt_id)
        output_path = os.path.join(directory, os.path.basename(output_path))
        Path(directory).mkdir(parents=True, exist_ok=True)

        lease_heartbeat.assert_owned()
        render_thumbnail(
            video_path=video_path,
            output_path=output_path,
            thumbnail_time=thumbnail_time,
            scale_width=scale_width,
            scale_height=scale_height,
            ffmpeg_bin_path=settings.FFMPEG_BIN_PATH,
            ffprobe_bin_path=settings.FFPROBE_BIN_PATH,
            timeout_seconds=settings.FFMPEG_THUMBNAIL_TIMEOUT_SECONDS,
            ffprobe_timeout_seconds=settings.FFPROBE_TIMEOUT_SECONDS,
            should_cancel=lambda: (
                self.is_forced_to_stop(request_id)
                or self.is_output_forced_to_stop(request_id, output_id)
            ),
            usage_callback=capture_usage,
        )
        lease_heartbeat.assert_owned()
        dimensions = validate_thumbnail_output(
            output_path=output_path,
            ffprobe_bin_path=settings.FFPROBE_BIN_PATH,
            expected_width=scale_width if scale_width > 0 else None,
            expected_height=scale_height if scale_height > 0 else None,
            timeout_seconds=settings.FFPROBE_TIMEOUT_SECONDS,
            should_cancel=lambda: (
                self.is_forced_to_stop(request_id)
                or self.is_output_forced_to_stop(request_id, output_id)
            ),
            on_usage=capture_usage,
        )
        lease_heartbeat.assert_owned()
        validation_report = {
            "valid": True,
            "artifact_type": "image_thumbnail",
            "width": dimensions["width"],
            "height": dimensions["height"],
            "size_bytes": os.path.getsize(output_path),
            "errors": [],
        }
        lease_heartbeat.assert_owned()
        repository.complete_processing_attempt(
            job_id=request_id,
            stage_key=output_id,
            attempt_id=attempt_id,
            validation_report=validation_report,
            lease_token=lease_token,
            metrics={
                "input_bytes": input_bytes,
                # The publisher owns the storage charge for the final object.
                "output_bytes": 0,
                "cpu_seconds": max(
                    child_cpu_seconds_since(cpu_started),
                    aggregate_process_usage()["cpu_seconds"],
                ),
                "peak_memory_bytes": aggregate_process_usage()[
                    "peak_memory_bytes"
                ],
                "memory_duration_ms": aggregate_process_usage()[
                    "memory_duration_ms"
                ],
            },
            pricing=configured_pricing(settings),
        )
    except Exception as exc:
        if output_path:
            try:
                Path(output_path).unlink()
            except OSError:
                pass
        if isinstance(exc, AttemptLeaseLost):
            raise
        if self.is_forced_to_stop(request_id) or self.is_output_forced_to_stop(
                request_id, output_id):
            repository.cancel_attempt(
                request_id,
                output_id,
                attempt_id,
                reason="CANCELLED_DURING_THUMBNAIL_GENERATION",
                metrics={
                    "input_bytes": input_bytes,
                    "cpu_seconds": max(
                        child_cpu_seconds_since(cpu_started),
                        aggregate_process_usage()["cpu_seconds"],
                    ),
                    "peak_memory_bytes": aggregate_process_usage()[
                        "peak_memory_bytes"
                    ],
                    "memory_duration_ms": aggregate_process_usage()[
                        "memory_duration_ms"
                    ],
                },
                pricing=configured_pricing(settings),
                lease_token=lease_token,
            )
            if self.is_forced_to_stop(request_id):
                raise self.raise_revoke(request_id)
            raise self.raise_revoke_output(request_id, output_id)

        retryable = (
            self.request.retries < settings.TASK_RETRY_FFMPEG_COMMAND_MAX
        )
        repository.fail_attempt(
            job_id=request_id,
            stage_key=output_id,
            attempt_id=attempt_id,
            retryable=retryable,
            error_code="THUMBNAIL_RENDER_OR_VALIDATION_FAILED",
            error_message=str(exc),
            metrics={
                "input_bytes": input_bytes,
                "cpu_seconds": max(
                    child_cpu_seconds_since(cpu_started),
                    aggregate_process_usage()["cpu_seconds"],
                ),
                "peak_memory_bytes": aggregate_process_usage()[
                    "peak_memory_bytes"
                ],
                "memory_duration_ms": aggregate_process_usage()[
                    "memory_duration_ms"
                ],
            },
            pricing=configured_pricing(settings),
            lease_token=lease_token,
        )
        if retryable:
            raise self.retry(
                exc=exc,
                max_retries=settings.TASK_RETRY_FFMPEG_COMMAND_MAX,
            )
        raise

    return dict(
        file_path=output_path,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        input_bytes=input_bytes,
        cpu_seconds=max(
            child_cpu_seconds_since(cpu_started),
            aggregate_process_usage()["cpu_seconds"],
        ),
        peak_memory_bytes=aggregate_process_usage()["peak_memory_bytes"],
        memory_duration_ms=aggregate_process_usage()["memory_duration_ms"],
        validation_report=validation_report,
        artifact_type="image_thumbnail",
        media_type="image/{}".format(
            Path(output_path).suffix.lower().lstrip(".") or "jpeg"
        ),
    )
