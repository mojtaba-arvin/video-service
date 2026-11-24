import os
import time
import ffmpeg
from abc import ABC
from pathlib import Path
from video_streaming import settings
from video_streaming.execution.attempt_scope import attempt_cgroup_scope
from video_streaming.celery import celery_app
from video_streaming.core.constants import CacheKeysTemplates
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.ffmpeg.utils import FfmpegCallback, get_time, \
    time_left
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from video_streaming.workflows.service import get_workflow_repository
from video_streaming.workflows.domain import AttemptLeaseLost
from video_streaming.workflows.lease import AttemptLeaseHeartbeat
from video_streaming.workflows.metrics import (
    capture_child_cpu_seconds,
    child_cpu_seconds_since,
    configured_pricing,
)
from video_streaming.execution.process_control import (
    ProcessUsageAccumulator,
    run_streaming_process,
)
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

    input_bytes = (
        os.path.getsize(video_path) + os.path.getsize(watermark_path)
    )
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
    process_usage = ProcessUsageAccumulator()

    def aggregate_process_usage():
        return process_usage.aggregate()

    def measured_cpu_seconds():
        sampled_cpu_seconds = aggregate_process_usage()["cpu_seconds"]
        return max(
            child_cpu_seconds_since(cpu_started),
            sampled_cpu_seconds if sampled_cpu_seconds is not None else 0.0,
        )

    def process_resource_metrics():
        aggregate = aggregate_process_usage()
        return {
            "peak_memory_bytes": aggregate["peak_memory_bytes"],
            "memory_duration_ms": aggregate["memory_duration_ms"],
        }

    output_path = None

    def capture_process_usage(metrics):
        process_usage.record(metrics)
        lease_heartbeat.on_usage(metrics)

    try:
        with attempt_cgroup_scope(attempt_id):
            lease_heartbeat.assert_owned()
            self.save_output_status(
                self.output_status.PREPARATION_PROCESSING,
                output_id,
                request_id)

            output_path, directory = self.ensure_set_output_location(
                request_id,
                output_id,
                output_path=output_path,
                s3_output_key=s3_output_key)
            directory = os.path.join(directory, ".attempts", attempt_id)
            output_path = os.path.join(directory, os.path.basename(output_path))
            Path(directory).mkdir(parents=True, exist_ok=True)
            lease_heartbeat.assert_owned()

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
                on_usage=capture_process_usage,
            )
            lease_heartbeat.assert_owned()

            if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
                raise RuntimeError("Watermarked output is missing or empty")

            lease_heartbeat.assert_owned()
            probe = probe_media(
                output_path,
                ffprobe_bin_path=settings.FFPROBE_BIN_PATH,
                timeout_seconds=settings.FFPROBE_TIMEOUT_SECONDS,
                should_cancel=lambda: (
                    self.is_forced_to_stop(request_id)
                    or self.is_output_forced_to_stop(request_id, output_id)
                ),
                on_usage=capture_process_usage,
            )
            video_stream = next(
                (item for item in probe.get("streams", [])
                 if item.get("codec_type") == "video"),
                None,
            )
            if video_stream is None:
                raise RuntimeError("Watermarked output contains no video stream")
            lease_heartbeat.assert_owned()

            validation_report = {
                "valid": True,
                "artifact_type": "video_file",
                "width": int(video_stream.get("width") or 0),
                "height": int(video_stream.get("height") or 0),
                "size_bytes": os.path.getsize(output_path),
                "video_codec": video_stream.get("codec_name"),
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
                "cpu_seconds": measured_cpu_seconds(),
                **process_resource_metrics(),
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
                reason="CANCELLED_DURING_WATERMARK_PROCESSING",
                metrics={
                    "input_bytes": input_bytes,
                    "cpu_seconds": measured_cpu_seconds(),
                    **process_resource_metrics(),
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
            error_code="WATERMARK_PROCESSING_OR_VALIDATION_FAILED",
            error_message=str(exc),
            metrics={
                "input_bytes": input_bytes,
                "cpu_seconds": measured_cpu_seconds(),
                **process_resource_metrics(),
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
        video_path=output_path,
        file_path=output_path,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        input_bytes=input_bytes,
        cpu_seconds=measured_cpu_seconds(),
        **process_resource_metrics(),
        validation_report=validation_report,
        artifact_type="video_file",
        media_type="video/mp4",
    )
