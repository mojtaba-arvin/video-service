from abc import ABC
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.ffmpeg.utils import FfmpegCallback
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from .base import BaseStreamingTask
from .mixins import CreatePlaylistMixin


class CreatePlaylistTask(
        ChainCallbackMixin,
        CreatePlaylistMixin,
        BaseStreamingTask,
        ABC
        ):

    # rewrite BaseOutputMixin.save_failed
    def save_failed(self, request_id, output_number):
        super().save_failed(request_id, output_number)
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
        input_path: str = None,
        output_path: str = None,
        s3_output_key: str = None,
        fragmented: bool = settings.DEFAULT_SEGMENT_TYPE_IS_FMP4,
        encode_format: str = settings.DEFAULT_ENCODE_FORMAT,
        video_codec: str = None,
        audio_codec: str = None,  # The audio codec format, e.g "aac"
        quality_names: list[str] = None,
        custom_qualities: list[dict] = None,
        async_run: bool = False,
        request_id: str = None,
        output_number: int = None,
        is_hls: bool = settings.DEFAULT_PLAYLIST_IS_HLS
        ) -> dict:
    """create an playlist ( HLS or DASH )

       Kwargs:

         input_path:
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
         output_number:
           output_number is using in redis key, to save progress of
           every output, also it's using to create different path
           for outputs
        

       required parameters:
         - request_id
         - output_number
         - input_path
         - output_path or s3_output_key
    """

    self.check_create_playlist_requirements(
        request_id=request_id,
        output_number=output_number,
        input_path=input_path,
        output_path=output_path,
        s3_output_key=s3_output_key)

    if self.is_forced_to_stop(request_id):
        raise self.raise_revoke(request_id)
    if self.is_output_forced_to_stop(request_id, output_number):
        raise self.raise_revoke_output(request_id, output_number)

    # save primary status using request_id
    self.save_primary_status(
        self.primary_status.OUTPUTS_PROGRESSING,
        request_id)

    # save output status using output_number and request_id
    self.save_output_status(
        self.output_status.PREPARATION_PROCESSING,
        output_number,
        request_id)

    # get output directory and set output_path if is None
    output_path, directory = self.ensure_set_output_location(
       request_id,
       output_number,
       output_path=output_path,
       s3_output_key=s3_output_key)

    playlist = self.initial_protocol(
        input_path,
        output_number,
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
                output_number=output_number,
                request_id=request_id
            ).progress,
            ffmpeg_bin=settings.FFMPEG_BIN_PATH,
            async_run=async_run)
    except Exception as e:

        if self.is_forced_to_stop(request_id):
            raise self.raise_revoke(request_id)
        if self.is_output_forced_to_stop(request_id, output_number):
            raise self.raise_revoke_output(request_id, output_number)

        # TODO handle possible Runtime Errors
        # notice : video processing has cost to retry
        raise self.retry(
            exc=e,
            max_retries=settings.TASK_RETRY_FFMPEG_COMMAND_MAX)

    # it's possible process killed in FfmpegCallback
    # so, checking the force stop before continuing
    if self.is_forced_to_stop(request_id):
        raise self.raise_revoke(request_id)
    if self.is_output_forced_to_stop(request_id, output_number):
        raise self.raise_revoke_output(request_id, output_number)

    self.save_output_status(
        self.output_status.PROCESSING_FINISHED,
        output_number,
        request_id)

    return dict(directory=directory)
