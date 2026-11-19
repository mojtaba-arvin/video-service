import os
import time
import ffmpeg
from abc import ABC
from pathlib import Path
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.core.constants import CacheKeysTemplates
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.ffmpeg.utils import FfmpegCallback, get_time, \
    time_left
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from video_streaming.workflows.service import get_workflow_repository
from video_streaming.workflows.metrics import (
    capture_child_cpu_seconds,
    child_cpu_seconds_since,
    configured_pricing,
)
from video_streaming.execution.process_control import run_streaming_process
from video_streaming.execution.media_probe import probe_media
from .base import BaseStreamingTask
from .mixins import AddWatermarkMixin


class AddWatermarkTask(
        ChainCallbackMixin,
        AddWatermarkMixin,
        BaseStreamingTask,
        ABC
        ):

    # rewrite BaseOutputMixin.save_failed
    def save_failed(self, request_id, output_id):
        #
        super().save_failed(request_id, output_id)
        # set failed status for all watermarked outputs tasks

        job_details: dict = self.cache.get(
            CacheKeysTemplates.JOB_DETAILS.format(
                request_id=request_id))
        if job_details:
            for output_id in set(job_details['watermarked_outputs_ids']):
                super().save_failed(request_id, output_id)

        # stop reason will only be set if there is no reason before.
        # set common reason for the task after many retries or etc.
        self.save_job_stop_reason(
            self.stop_reason.FAILED_ADD_WATERMARK,
            request_id
        )


@celery_app.task(name="add_watermark",
                 base=AddWatermarkTask,
                 **TASK_DECORATOR_KWARGS)
def add_watermark(
        self,
        *args,
        video_path: str = None,
        watermark_path: str = None,
        output_path: str = None,
        s3_output_key: str = None,
        request_id: str = None,
        output_id: str = None
        ) -> dict:

    # TODO add overlay params

    self.check_add_watermark_requirements(
        request_id=request_id,
        output_id=output_id,
        video_path=video_path,
        watermark_path=watermark_path,
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
    input_bytes = (
        os.path.getsize(video_path) + os.path.getsize(watermark_path)
    )
    cpu_started = capture_child_cpu_seconds()
    process_usage = {}

    def measured_cpu_seconds():
        return max(
            child_cpu_seconds_since(cpu_started),
            float(process_usage.get("cpu_seconds") or 0.0),
        )

    def process_resource_metrics():
        cached_peak_memory = self.cache.get(
            CacheKeysTemplates.OUTPUT_END_MEMORY_RSS.format(
                request_id=request_id, output_id=output_id
            )
        )
        measured_peak_memory = process_usage.get("peak_memory_bytes")
        peak_memory_bytes = max(
            int(value) for value in (
                cached_peak_memory, measured_peak_memory
            ) if value
        ) if cached_peak_memory or measured_peak_memory else None
        started_at = self.cache.get(
            CacheKeysTemplates.OUTPUT_START_PROCESSING_TIME.format(
                request_id=request_id, output_id=output_id
            )
        )
        ended_at = self.cache.get(
            CacheKeysTemplates.OUTPUT_END_PROCESSING_TIME.format(
                request_id=request_id, output_id=output_id
            )
        )
        memory_duration_ms = process_usage.get("memory_duration_ms")
        if memory_duration_ms is None and started_at is not None and ended_at is not None:
            try:
                memory_duration_ms = max(
                    0, int((float(ended_at) - float(started_at)) * 1000)
                )
            except (TypeError, ValueError):
                memory_duration_ms = None
        return {
            "peak_memory_bytes": (
                int(peak_memory_bytes) if peak_memory_bytes else None
            ),
            "memory_duration_ms": memory_duration_ms,
        }

    self.save_output_status(
        self.output_status.PREPARATION_PROCESSING,
        output_id,
        request_id)

    output_path, directory = self.ensure_set_output_location(
        request_id,
        output_id,
        output_path=output_path,
        s3_output_key=s3_output_key)
    Path(directory).mkdir(parents=True, exist_ok=True)

    try:
        main = ffmpeg.input(video_path)
        watermark = ffmpeg.input(watermark_path)
        callback = FfmpegCallback(
            task=self,
            task_id=self.request.id.__str__(),
            output_id=output_id,
            request_id=request_id,
        ).progress

        stream = ffmpeg.filter(
            [main, watermark], "overlay", 0, 0
        ).output(output_path)

        duration = 1
        current_time = 0
        start_time = time.time()

        def report_progress(line, process):
            nonlocal duration, current_time
            duration = get_time("Duration: ", line, duration)
            current_time = get_time("time=", line, current_time)
            callback(
                line,
                duration,
                current_time,
                time_left(start_time, current_time, duration),
                process,
            )

        run_streaming_process(
            stream.compile(cmd=settings.FFMPEG_BIN_PATH),
            timeout_seconds=settings.FFMPEG_WATERMARK_TIMEOUT_SECONDS,
            on_line=report_progress,
            should_cancel=lambda: (
                self.is_forced_to_stop(request_id)
                or self.is_output_forced_to_stop(request_id, output_id)
            ),
            on_usage=process_usage.update,
        )

        if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
            raise RuntimeError("Watermarked output is missing or empty")

        probe = probe_media(
            output_path,
            ffprobe_bin_path=settings.FFPROBE_BIN_PATH,
            timeout_seconds=settings.FFPROBE_TIMEOUT_SECONDS,
            should_cancel=lambda: (
                self.is_forced_to_stop(request_id)
                or self.is_output_forced_to_stop(request_id, output_id)
            ),
        )
        video_stream = next(
            (item for item in probe.get("streams", [])
             if item.get("codec_type") == "video"),
            None,
        )
        if video_stream is None:
            raise RuntimeError("Watermarked output contains no video stream")

        validation_report = {
            "valid": True,
            "artifact_type": "video_file",
            "width": int(video_stream.get("width") or 0),
            "height": int(video_stream.get("height") or 0),
            "size_bytes": os.path.getsize(output_path),
            "video_codec": video_stream.get("codec_name"),
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
                reason="CANCELLED_DURING_WATERMARK_PROCESSING",
                metrics={
                    "input_bytes": input_bytes,
                    "cpu_seconds": child_cpu_seconds_since(cpu_started),
                    **process_resource_metrics(),
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
            error_code="WATERMARK_PROCESSING_OR_VALIDATION_FAILED",
            error_message=str(exc),
            metrics={
                "input_bytes": input_bytes,
                "cpu_seconds": child_cpu_seconds_since(cpu_started),
                **process_resource_metrics(),
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
            reason="CANCELLED_AFTER_WATERMARK_VALIDATION",
            metrics={
                "input_bytes": input_bytes,
                "cpu_seconds": child_cpu_seconds_since(cpu_started),
            },
            pricing=configured_pricing(settings),
        )
        if self.is_forced_to_stop(request_id):
            raise self.raise_revoke(request_id)
        raise self.raise_revoke_output(request_id, output_id)


    return dict(
        video_path=output_path,
        file_path=output_path,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        input_bytes=input_bytes,
        cpu_seconds=child_cpu_seconds_since(cpu_started),
        **process_resource_metrics(),
        validation_report=validation_report,
        artifact_type="video_file",
        media_type="video/mp4",
    )
