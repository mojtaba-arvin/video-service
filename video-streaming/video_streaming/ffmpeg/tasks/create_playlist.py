from abc import ABC
import os
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.core.constants import CacheKeysTemplates
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.ffmpeg.utils import FfmpegCallback
from video_streaming.ffmpeg.package_validation import (
    reset_package_directory,
    validate_package,
)
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from video_streaming.workflows.service import get_workflow_repository
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
    attempt_id, attempt_number = workflow_repository.start_attempt(
        job_id=request_id,
        stage_key=output_id,
        execution_backend="celery",
        execution_ref=self.request.id.__str__(),
    )
    input_bytes = 0
    cpu_started = capture_child_cpu_seconds()
    expected_renditions = []
    validation_report = None

    def process_resource_metrics():
        peak_memory_bytes = self.cache.get(
            CacheKeysTemplates.OUTPUT_END_MEMORY_RSS.format(
                request_id=request_id, output_id=output_id
            )
        )
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
        memory_duration_ms = None
        if started_at is not None and ended_at is not None:
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

    try:
        input_bytes = os.path.getsize(video_path)

        self.save_primary_status(
            self.primary_status.OUTPUTS_PROGRESSING,
            request_id)
        self.save_output_status(
            self.output_status.PREPARATION_PROCESSING,
            output_id,
            request_id)

        managed_output_directory = output_path is None
        output_path, directory = self.ensure_set_output_location(
            request_id,
            output_id,
            output_path=output_path,
            s3_output_key=s3_output_key,
        )

        if managed_output_directory:
            directory = reset_package_directory(
                directory,
                self.get_outputs_root_directory(request_id),
            )

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

        playlist.output(
            output_path,
            monitor=FfmpegCallback(
                task=self,
                task_id=self.request.id.__str__(),
                output_id=output_id,
                request_id=request_id,
            ).progress,
            ffmpeg_bin=settings.FFMPEG_BIN_PATH,
            async_run=async_run,
        )

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
        )
        validation_report.require_valid()
        workflow_repository.mark_validated(
            job_id=request_id,
            stage_key=output_id,
            attempt_id=attempt_id,
            validation_report=validation_report.as_dict(),
        )
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
        workflow_repository.fail_attempt(
            job_id=request_id,
            stage_key=output_id,
            attempt_id=attempt_id,
            retryable=retryable,
            error_code="PACKAGE_CREATION_OR_VALIDATION_FAILED",
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
        workflow_repository.cancel_attempt(
            job_id=request_id,
            stage_key=output_id,
            attempt_id=attempt_id,
            reason="CANCELLED_AFTER_PACKAGE_VALIDATION",
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

    # Processing completion is deferred until publication has been verified.
    return dict(
        directory=directory,
        expected_renditions=expected_renditions,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        input_bytes=input_bytes,
        cpu_seconds=child_cpu_seconds_since(cpu_started),
        **process_resource_metrics(),
        validation_report=validation_report.as_dict(),
    )
