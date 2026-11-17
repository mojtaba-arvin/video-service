from abc import ABC
import os
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.ffmpeg.utils import FfmpegCallback
from video_streaming.ffmpeg.package_validation import validate_streaming_package
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from video_streaming.workflows.service import get_workflow_repository
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

    attempt_id, attempt_number = get_workflow_repository().start_attempt(
        job_id=request_id,
        stage_key=output_id,
        execution_backend="celery",
        execution_ref=self.request.id.__str__(),
    )
    input_bytes = os.path.getsize(video_path)

    # save primary status using request_id
    self.save_primary_status(
        self.primary_status.OUTPUTS_PROGRESSING,
        request_id)

    # save output status using output_id and request_id
    self.save_output_status(
        self.output_status.PREPARATION_PROCESSING,
        output_id,
        request_id)

    # get output directory and set output_path if is None
    output_path, directory = self.ensure_set_output_location(
       request_id,
       output_id,
       output_path=output_path,
       s3_output_key=s3_output_key)

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
        quality_names=quality_names)

    try:
        # self.output_path includes the file name
        playlist.output(
            output_path,
            monitor=FfmpegCallback(
                task=self,
                task_id=self.request.id.__str__(),
                output_id=output_id,
                request_id=request_id
            ).progress,
            ffmpeg_bin=settings.FFMPEG_BIN_PATH,
            async_run=async_run)
        validation_report = validate_streaming_package(
            directory=directory,
            manifest_path=output_path,
            protocol="hls" if is_hls else "dash",
        )
        get_workflow_repository().mark_validated(
            job_id=request_id,
            stage_key=output_id,
            attempt_id=attempt_id,
            validation_report=validation_report,
        )
    except Exception as e:

        if self.is_forced_to_stop(request_id):
            raise self.raise_revoke(request_id)
        if self.is_output_forced_to_stop(request_id, output_id):
            raise self.raise_revoke_output(request_id, output_id)

        # The attempt is durable and retryable before Celery schedules its
        # next execution. Redis TTL is not involved in recovery semantics.
        get_workflow_repository().fail_attempt(
            job_id=request_id,
            stage_key=output_id,
            attempt_id=attempt_id,
            retryable=True,
            error_code="PACKAGE_CREATION_OR_VALIDATION_FAILED",
            error_message=str(e),
            metrics={"input_bytes": input_bytes},
        )
        raise self.retry(
            exc=e,
            max_retries=settings.TASK_RETRY_FFMPEG_COMMAND_MAX)

    # TODO check ffmpeg is really finished successfully,
    #  Sometimes FfmpegCallback has an error but the Ffmpeg stops
    #  without error and returns 'ffmpeg executed command successfully'

    # it's possible process killed in FfmpegCallback
    # so, checking the force stop before continuing
    if self.is_forced_to_stop(request_id):
        raise self.raise_revoke(request_id)
    if self.is_output_forced_to_stop(request_id, output_id):
        raise self.raise_revoke_output(request_id, output_id)

    # Processing is not complete until the publication stage verifies storage.
    return dict(
        directory=directory,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        input_bytes=input_bytes,
        validation_report=validation_report,
    )
