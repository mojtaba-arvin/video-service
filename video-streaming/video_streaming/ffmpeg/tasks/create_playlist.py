from abc import ABC
import os
import re
import shlex
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.core.constants import CacheKeysTemplates
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.ffmpeg.utils import FfmpegCallback
from ffmpeg_streaming._command_builder import command_builder
from video_streaming.execution.process_control import (
    ProcessUsageAccumulator,
    run_streaming_process,
)
from video_streaming.ffmpeg.package_validation import validate_package
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from video_streaming.workflows.service import get_workflow_repository
from video_streaming.workflows.domain import AttemptLeaseLost
from video_streaming.workflows.lease import AttemptLeaseHeartbeat
from video_streaming.workflows.artifacts import attempt_scoped_local_output_path
from video_streaming.workflows.metrics import (
    capture_child_cpu_seconds,
    child_cpu_seconds_since,
    configured_pricing,
)
from .base import BaseStreamingTask
from .mixins import CreatePlaylistMixin


class CreatePlaylistTask(
        ChainCallbackMixin,
        CreatePlaylistMixin,
        BaseStreamingTask,
        ABC
        ):

    # rewrite BaseOutputMixin.save_failed
    def save_failed(self, request_id, output_id):
        super().save_failed(request_id, output_id)
        # stop reason will only be set if there is no reason before.
        # set common reason for the task after many retries or etc.
        self.save_job_stop_reason(
            self.stop_reason.FAILED_CREATE_PLAYLIST,
            request_id
        )


@celery_app.task(name="create_playlist",
                 base=CreatePlaylistTask,
                 **TASK_DECORATOR_KWARGS)
def create_playlist(
        self,
        *args,
        video_path: str = None,
        output_path: str = None,
        s3_output_key: str = None,
        fragmented: bool = settings.DEFAULT_SEGMENT_TYPE_IS_FMP4,
        encode_format: str = settings.DEFAULT_ENCODE_FORMAT,
        video_codec: str = None,
        audio_codec: str = None,
        quality_names: list[str] = None,
        custom_qualities: list[dict] = None,
        async_run: bool = False,
        request_id: str = None,
        output_id: str = None,
        is_hls: bool = settings.DEFAULT_PLAYLIST_IS_HLS,
        **kwargs
        ) -> dict:
    """create an playlist ( HLS or DASH )

    Args:
        self:
        *args:
        s3_output_key:
        fragmented:
        encode_format:
        request_id:
        is_hls:
            type of playlist, True is HLS, False is MPEG-DASH
        video_path:
            The local input path
        output_path:
            The local output path
        video_codec:
            The video codec format, e.g "libx264", "libx265"
            or "libvpx-vp9"
        audio_codec:
            The audio codec format, e.g "aac"
        quality_names:
            List of quality names to generate. e.g. ["360p","720p"]
            or [Resolutions.R_360P, Resolutions.R_720P]
        custom_qualities:
            a list of dict includes size and bitrate
            e.g. [dict(size=[256, 144], bitrate=[97280, 65536])]
        async_run:
            default of async_run is False to don't call async method
            inside the task, it can raise RuntimeError: asyncio.run()
            cannot be called from a running event loop
        output_id:
            output_id is using in redis key, to save progress of
            every output, also it's using to create different path
            for outputs
        **kwargs:
            some unused parameters from previous tasks that set by __call__

    Required parameters:
        - request_id
        - output_id
        - input_path
        - output_path or s3_output_key

    Returns:
        a dict includes directory

    """

    self.check_create_playlist_requirements(
        request_id=request_id,
        output_id=output_id,
        video_path=video_path,
        output_path=output_path,
        s3_output_key=s3_output_key)

    if self.is_forced_to_stop(request_id):
        raise self.raise_revoke(request_id)
    if self.is_output_forced_to_stop(request_id, output_id):
        raise self.raise_revoke_output(request_id, output_id)

    workflow_repository = get_workflow_repository()
    existing_stage = workflow_repository.get_stage_snapshot(
        request_id, output_id
    )
    if existing_stage and existing_stage.get("status") in (
            "VALIDATED", "RETRY_WAIT", "FINISHED"):
        validation_artifact = next((
            item for item in existing_stage.get("artifacts", [])
            if item.get("type") == "validation_report"
            and item.get("status") == "AVAILABLE"
        ), None)
        if validation_artifact is not None:
            metadata = validation_artifact.get("metadata") or {}
            processing_attempt = next((
                item for item in existing_stage.get("attempts", [])
                if item.get("id") == validation_artifact.get("attempt_id")
                and item.get("status") == "SUCCEEDED"
            ), None)
            if processing_attempt is not None:
                local_package = workflow_repository.get_attempt_local_package(
                    processing_attempt["id"]
                ) or {}
                local_directory = local_package.get("directory")
                local_manifest = local_package.get("manifest_path")
                expected = local_package.get("expected_renditions") or []
                local_package_available = (
                    isinstance(local_directory, str)
                    and os.path.isdir(local_directory)
                    and isinstance(local_manifest, str)
                    and os.path.isfile(local_manifest)
                    and isinstance(expected, list)
                )
                if (
                        existing_stage.get("status") == "FINISHED"
                        or local_package_available):
                    return dict(
                        directory=(
                            local_directory if local_package_available else None
                        ),
                        expected_renditions=expected,
                        attempt_id=processing_attempt["id"],
                        attempt_number=processing_attempt["attempt_number"],
                        input_bytes=processing_attempt.get("input_bytes"),
                        cpu_seconds=processing_attempt.get("cpu_seconds"),
                        peak_memory_bytes=processing_attempt.get(
                            "peak_memory_bytes"
                        ),
                        memory_duration_ms=processing_attempt.get(
                            "memory_duration_ms"
                        ),
                        validation_report=metadata,
                    )
                workflow_repository.requeue_processing_after_local_package_loss(
                    request_id,
                    output_id,
                    processing_attempt["id"],
                    reason="VALIDATED_LOCAL_PACKAGE_MISSING_ON_TASK_REPLAY",
                )

    attempt_id, attempt_number, lease_token, _ = workflow_repository.claim_attempt(
        job_id=request_id,
        stage_key=output_id,
        lease_ttl_seconds=settings.WORKFLOW_ATTEMPT_LEASE_TTL_SECONDS,
        lease_owner=("celery:{}".format(
            getattr(self.request, "hostname", None) or "worker"
        ))[:255],
        execution_backend="celery",
        execution_ref=str(self.request.id),
        pricing=configured_pricing(settings),
    )
    lease_heartbeat = AttemptLeaseHeartbeat(
        workflow_repository,
        request_id,
        output_id,
        attempt_id,
        lease_token,
        lease_ttl_seconds=settings.WORKFLOW_ATTEMPT_LEASE_TTL_SECONDS,
    )
    input_bytes = 0
    cpu_started = capture_child_cpu_seconds()
    expected_renditions = []
    validation_report = None
    validation_payload = None
    process_usage = ProcessUsageAccumulator()

    def record_process_usage(metrics):
        process_usage.record(metrics)
        lease_heartbeat.on_usage(metrics)

    def aggregate_process_usage():
        return process_usage.aggregate()

    def process_resource_metrics():
        aggregate = aggregate_process_usage()
        return {
            "peak_memory_bytes": aggregate["peak_memory_bytes"],
            "memory_duration_ms": aggregate["memory_duration_ms"],
        }

    def measured_cpu_seconds():
        sampled_cpu_seconds = aggregate_process_usage()["cpu_seconds"]
        return max(
            child_cpu_seconds_since(cpu_started),
            sampled_cpu_seconds if sampled_cpu_seconds is not None else 0.0,
        )


    try:
        input_bytes = os.path.getsize(video_path)

        self.save_primary_status(
            self.primary_status.OUTPUTS_PROGRESSING,
            request_id)
        self.save_output_status(
            self.output_status.PREPARATION_PROCESSING,
            output_id,
            request_id)

        base_output_path = output_path
        if base_output_path is None:
            output_filename = s3_output_key.rpartition("/")[-1]
            base_output_path = os.path.join(
                self.get_outputs_root_directory(request_id),
                str(output_id),
                output_filename,
            )
        output_path, directory = attempt_scoped_local_output_path(
            base_output_path, attempt_id
        )
        os.makedirs(directory, exist_ok=True)

        lease_heartbeat.assert_owned()
        playlist = self.initial_protocol(
            video_path,
            output_id,
            request_id,
            encode_format,
            video_codec=video_codec,
            audio_codec=audio_codec,
            is_hls=is_hls,
            fragmented=fragmented,
            custom_qualities=custom_qualities,
            quality_names=quality_names,
        )

        lease_heartbeat.assert_owned()
        representations = list(playlist.reps)
        if is_hls:
            manifest_stem = os.path.splitext(
                os.path.basename(output_path)
            )[0]
            expected_renditions = [
                "{}_{}p.m3u8".format(manifest_stem, rep.size.height)
                for rep in representations
            ]
        else:
            expected_renditions = [
                str(index) for index, _ in enumerate(representations)
            ]

        # Let the library prepare HLS/DASH output paths and master
        # playlists, but do not delegate process ownership to its internal
        # monitor thread. The shared process supervisor owns timeout,
        # cancellation, process-tree termination and callback failures.
        playlist.output(output_path, run_command=False)
        ffmpeg_callback = FfmpegCallback(
            task=self,
            task_id=self.request.id.__str__(),
            output_id=output_id,
            request_id=request_id,
            on_progress=lease_heartbeat.on_progress,
        )
        duration_seconds = 0.0
        current_seconds = 0.0

        def report_progress(line, process):
            nonlocal duration_seconds, current_seconds
            duration_match = re.search(
                r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
                line,
            )
            current_match = re.search(
                r"\btime=(\d+):(\d+):(\d+(?:\.\d+)?)",
                line,
            )
            if duration_match:
                hours, minutes, seconds = duration_match.groups()
                duration_seconds = (
                    int(hours) * 3600 + int(minutes) * 60 + float(seconds)
                )
            if current_match:
                hours, minutes, seconds = current_match.groups()
                current_seconds = (
                    int(hours) * 3600 + int(minutes) * 60 + float(seconds)
                )
            ffmpeg_callback.progress(
                line,
                duration_seconds,
                current_seconds,
                max(0.0, duration_seconds - current_seconds),
                process,
            )

        command = shlex.split(
            command_builder(settings.FFMPEG_BIN_PATH, playlist)
        )
        run_streaming_process(
            command,
            timeout_seconds=settings.FFMPEG_PLAYLIST_TIMEOUT_SECONDS,
            on_line=report_progress,
            should_cancel=lambda: (
                self.is_forced_to_stop(request_id)
                or self.is_output_forced_to_stop(request_id, output_id)
            ),
            on_usage=record_process_usage,
        )

        lease_heartbeat.assert_owned()
        validation_report = validate_package(
            directory=directory,
            manifest_path=output_path,
            package_type="hls" if is_hls else "dash",
            expected_renditions=expected_renditions,
            ffprobe_bin_path=settings.FFPROBE_BIN_PATH,
            metadata_probe_timeout_seconds=(
                settings.PACKAGE_METADATA_PROBE_TIMEOUT_SECONDS
            ),
            should_cancel=lambda: (
                self.is_forced_to_stop(request_id)
                or self.is_output_forced_to_stop(request_id, output_id)
            ),
            on_usage=record_process_usage,
        )
        validation_report.require_valid()
        validation_payload = validation_report.as_dict()
        lease_heartbeat.assert_owned()
        workflow_repository.complete_processing_attempt(
            job_id=request_id,
            stage_key=output_id,
            attempt_id=attempt_id,
            validation_report=validation_payload,
            lease_token=lease_token,
            metrics={
                "input_bytes": input_bytes,
                # Storage is charged to the separate publication Attempt.
                "output_bytes": 0,
                "cpu_seconds": measured_cpu_seconds(),
                **process_resource_metrics(),
                "local_package": {
                    "directory": directory,
                    "manifest_path": output_path,
                    "expected_renditions": list(expected_renditions),
                    "package_type": "hls" if is_hls else "dash",
                },
            },
            pricing=configured_pricing(settings),
        )
    except AttemptLeaseLost:
        # The callback has already terminated the FFmpeg process tree. A stale
        # worker must not mutate durable state after losing its lease.
        raise
    except Exception as exc:
        if self.is_forced_to_stop(request_id) or self.is_output_forced_to_stop(
                request_id, output_id):
            workflow_repository.cancel_attempt(
                job_id=request_id,
                stage_key=output_id,
                attempt_id=attempt_id,
                reason="CANCELLED_DURING_PACKAGE_CREATION_OR_VALIDATION",
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
        workflow_repository.fail_attempt(
            job_id=request_id,
            stage_key=output_id,
            attempt_id=attempt_id,
            retryable=retryable,
            error_code="PACKAGE_CREATION_OR_VALIDATION_FAILED",
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

    if self.is_forced_to_stop(request_id) or self.is_output_forced_to_stop(
            request_id, output_id):
        # Processing is already terminal. Record cancellation on a distinct
        # publication Attempt rather than mutating the completed processing
        # Attempt whose lease tuple has been cleared.
        publication_id, _, publication_token, _ = (
            workflow_repository.claim_publication_attempt(
                job_id=request_id,
                stage_key=output_id,
                lease_ttl_seconds=settings.WORKFLOW_ATTEMPT_LEASE_TTL_SECONDS,
                lease_owner=("celery:{}".format(
                    getattr(self.request, "hostname", None) or "worker"
                ))[:255],
                execution_backend="celery",
                execution_ref=str(self.request.id),
                processing_attempt_id=attempt_id,
                pricing=configured_pricing(settings),
            )
        )
        workflow_repository.cancel_attempt(
            job_id=request_id,
            stage_key=output_id,
            attempt_id=publication_id,
            reason="CANCELLED_AFTER_PACKAGE_VALIDATION",
            metrics={
                "input_bytes": input_bytes,
                "cpu_seconds": measured_cpu_seconds(),
                **process_resource_metrics(),
            },
            pricing=configured_pricing(settings),
            lease_token=publication_token,
        )
        if self.is_forced_to_stop(request_id):
            raise self.raise_revoke(request_id)
        raise self.raise_revoke_output(request_id, output_id)

    # Processing is terminal here; the chained publisher owns a separate lease.
    return dict(
        directory=directory,
        expected_renditions=expected_renditions,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        input_bytes=input_bytes,
        cpu_seconds=measured_cpu_seconds(),
        **process_resource_metrics(),
        validation_report=validation_payload,
    )
