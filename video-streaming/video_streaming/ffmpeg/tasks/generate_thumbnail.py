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

    repository = get_workflow_repository()
    attempt_id, attempt_number = repository.start_attempt(
        job_id=request_id,
        stage_key=output_id,
        execution_backend="celery",
        execution_ref=self.request.id.__str__(),
    )
    input_bytes = os.path.getsize(video_path)
    cpu_started = capture_child_cpu_seconds()

    # save primary status using request_id
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
        output_path=output_path,
        s3_output_key=s3_output_key,
    )
    Path(directory).mkdir(parents=True, exist_ok=True)

    usage_metrics = {}

    def capture_usage(metrics):
        usage_metrics.update(metrics)

    try:
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
        )
        validation_report = {
            "valid": True,
            "artifact_type": "image_thumbnail",
            "width": dimensions["width"],
            "height": dimensions["height"],
            "size_bytes": os.path.getsize(output_path),
            "errors": [],
        }
        repository.mark_validated(
            job_id=request_id,
            stage_key=output_id,
            attempt_id=attempt_id,
            validation_report=validation_report,
        )
    except Exception as exc:
        if self.is_forced_to_stop(request_id) or self.is_output_forced_to_stop(
                request_id, output_id):
            repository.cancel_attempt(
                request_id, output_id, attempt_id,
                reason="CANCELLED_DURING_THUMBNAIL_GENERATION",
                metrics={
                    "input_bytes": input_bytes,
                    "cpu_seconds": usage_metrics.get(
                        "cpu_seconds", child_cpu_seconds_since(cpu_started)
                    ),
                    "peak_memory_bytes": usage_metrics.get("peak_memory_bytes"),
                    "memory_duration_ms": usage_metrics.get("memory_duration_ms"),
                },
                pricing=configured_pricing(settings),
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
                "cpu_seconds": usage_metrics.get(
                    "cpu_seconds", child_cpu_seconds_since(cpu_started)
                ),
                "peak_memory_bytes": usage_metrics.get("peak_memory_bytes"),
            },
            pricing=configured_pricing(settings),
        )
        if retryable:
            raise self.retry(
                exc=exc,
                max_retries=settings.TASK_RETRY_FFMPEG_COMMAND_MAX,
            )
        raise

    if self.is_forced_to_stop(request_id) or self.is_output_forced_to_stop(
            request_id, output_id):
        repository.cancel_attempt(
            request_id, output_id, attempt_id,
            reason="CANCELLED_AFTER_THUMBNAIL_VALIDATION",
            metrics={
                "input_bytes": input_bytes,
                "cpu_seconds": usage_metrics.get(
                    "cpu_seconds", child_cpu_seconds_since(cpu_started)
                ),
                "peak_memory_bytes": usage_metrics.get("peak_memory_bytes"),
                "memory_duration_ms": usage_metrics.get("memory_duration_ms"),
            },
            pricing=configured_pricing(settings),
        )
        if self.is_forced_to_stop(request_id):
            raise self.raise_revoke(request_id)
        raise self.raise_revoke_output(request_id, output_id)

    return dict(
        file_path=output_path,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        input_bytes=input_bytes,
        cpu_seconds=usage_metrics.get(
            "cpu_seconds", child_cpu_seconds_since(cpu_started)
        ),
        peak_memory_bytes=usage_metrics.get("peak_memory_bytes"),
        memory_duration_ms=usage_metrics.get("memory_duration_ms"),
        validation_report=validation_report,
        artifact_type="image_thumbnail",
        media_type="image/{}".format(
            Path(output_path).suffix.lower().lstrip(".") or "jpeg"
        ),
    )